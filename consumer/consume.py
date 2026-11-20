"""Consume company events from RabbitMQ into Postgres + pgvector."""

import argparse
import asyncio
import os
from pathlib import Path
from typing import Final, Protocol

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

import psycopg
from pgvector.psycopg import register_vector_async

from consumer.parser import parse_event
from consumer.embedding import Embedder

QUEUE: Final = "company-events"
PREFETCH: Final = 50

SCHEMA_PATH: Final = Path(__file__).with_name("schema.sql")

# Backoff between attempts to reach Postgres after the connection drops.
RECONNECT_DELAYS: Final = (1, 2, 5, 10, 30)


class TextEmbedder(Protocol):
    def embed(self, texts: list[str]) -> list: ...

UPSERT_SQL: Final = """
INSERT INTO companies (registration_number, company_name, nature_of_business, industry, state,
                       updated_at, last_message_id)
VALUES (%(reg)s, %(company_name)s, %(nature_of_business)s, %(industry)s, %(state)s,
        %(updated_at)s, %(message_id)s)
ON CONFLICT (registration_number) DO UPDATE SET
    company_name       = COALESCE(EXCLUDED.company_name, companies.company_name),
    nature_of_business = COALESCE(EXCLUDED.nature_of_business, companies.nature_of_business),
    industry           = COALESCE(EXCLUDED.industry, companies.industry),
    state              = COALESCE(EXCLUDED.state, companies.state),
    updated_at         = EXCLUDED.updated_at,
    last_message_id    = EXCLUDED.last_message_id,
    ingested_at        = now()
WHERE companies.updated_at IS NULL OR companies.updated_at < EXCLUDED.updated_at
RETURNING nature_of_business, embedded_text
"""

STRUCK_OFF_SQL: Final = """
INSERT INTO companies (registration_number, status, struck_off_at, updated_at, last_message_id)
VALUES (%(reg)s, 'struck_off', %(updated_at)s, %(updated_at)s, %(message_id)s)
ON CONFLICT (registration_number) DO UPDATE SET
    status          = 'struck_off',
    struck_off_at   = EXCLUDED.struck_off_at,
    last_message_id = EXCLUDED.last_message_id,
    ingested_at     = now()
WHERE companies.struck_off_at IS NULL OR companies.struck_off_at < EXCLUDED.struck_off_at
RETURNING registration_number
"""

async def apply_event(
    conn: psycopg.AsyncConnection, event: dict, message_id: str, embedder: TextEmbedder
) -> None:
    if event["type"] == "company-upserted":
        cur = await conn.execute(
            UPSERT_SQL,
            {
                "reg": event["registration_number"],
                "company_name": event["fields"].get("company_name"),
                "nature_of_business": event["fields"].get("nature_of_business"),
                "industry": event["fields"].get("industry"),
                "state": event["fields"].get("state"),
                "updated_at": event["updated_at"],
                "message_id": message_id,
            },
        )
        row = await cur.fetchone()
        if row is None:
            return

        nature, embedded_text = row
        if not nature or nature == embedded_text:
            return

        [vector] = await asyncio.to_thread(embedder.embed, [nature])
        await conn.execute(
            """
            UPDATE companies
            SET embedding = %s, embedded_text = %s
            WHERE registration_number = %s
            """,
            (vector, nature, event["registration_number"]),
        )

    elif event["type"] == "company-struck-off":
        await conn.execute(
            STRUCK_OFF_SQL,
            {
                "reg": event["registration_number"],
                "updated_at": event["updated_at"],
                "message_id": message_id,
            },
        )


async def connect_db(db_url: str) -> psycopg.AsyncConnection:
    conn = await psycopg.AsyncConnection.connect(db_url)
    await conn.execute(SCHEMA_PATH.read_bytes())
    await conn.commit()
    await register_vector_async(conn)
    return conn


class Consumer:
    def __init__(self, db_url: str, embedder: TextEmbedder) -> None:
        self.db_url = db_url
        self.embedder = embedder
        self.db: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        attempt = 0
        while True:
            try:
                self.db = await connect_db(self.db_url)
                return
            except psycopg.OperationalError as e:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                print(f"Postgres unavailable, retrying in {delay}s: {e}")
                attempt += 1
                await asyncio.sleep(delay)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()

    async def store(self, message: AbstractIncomingMessage, message_id: str) -> None:
        assert self.db is not None
        body = message.body.decode(errors="replace")
        await self.db.execute(
            """
            INSERT INTO raw_events (message_id, body) VALUES (%s, %s)
            ON CONFLICT (message_id) DO NOTHING
            """,
            (message_id, body),
        )
        await self.db.commit()

        try:
            event = parse_event(message.body)
        except ValueError as e:
            print(f"Skipping bad message {message_id}: {e}")
            return

        await apply_event(self.db, event, message_id, self.embedder)
        await self.db.commit()

    async def handle(self, message: AbstractIncomingMessage) -> None:
        message_id = message.message_id
        if message_id is None:
            print("Skipping message without message_id")
            await message.ack()
            return

        try:
            await self.store(message, message_id)
        except psycopg.OperationalError as e:
            # The connection itself is gone (Postgres restarted, network blip).
            # Nothing was committed for this message, so put it back and reconnect.
            print(f"Lost Postgres connection on {message_id}, requeueing: {e}")
            await message.nack(requeue=True)
            await self.close_quietly()
            await self.connect()
            return
        except Exception as e:
            await self.rollback_quietly()
            if message.redelivered:
                print(f"Dropping {message_id} after it failed twice: {e!r}")
                await message.nack(requeue=False)
            else:
                print(f"Failed to process {message_id}, requeueing once: {e!r}")
                await message.nack(requeue=True)
            return

        await message.ack()

    async def rollback_quietly(self) -> None:
        try:
            if self.db is not None:
                await self.db.rollback()
        except psycopg.Error:
            await self.close_quietly()
            await self.connect()

    async def close_quietly(self) -> None:
        try:
            await self.close()
        except psycopg.Error:
            pass


async def consume(amqp_url: str, db_url: str) -> None:
    consumer = Consumer(db_url, Embedder())
    await consumer.connect()
    connection = await aio_pika.connect_robust(amqp_url)

    try:
        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=PREFETCH)
            queue = await channel.declare_queue(QUEUE, durable=True)
            print(f"Consuming from {QUEUE}")

            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    await consumer.handle(message)
    finally:
        await consumer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)

    rabbit_port = os.environ.get("RABBITMQ_PORT", "5672")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")

    parser.add_argument(
        "--amqp-url",
        default=os.environ.get(
            "RABBITMQ_URL", f"amqp://guest:guest@localhost:{rabbit_port}/"
        ),
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get(
            "POSTGRES_URL",
            f"postgresql://postgres:postgres@localhost:{pg_port}/companies",
        ),
    )

    args = parser.parse_args()
    amqp_url: str = args.amqp_url
    db_url: str = args.db_url

    asyncio.run(consume(amqp_url, db_url))


if __name__ == "__main__":
    main()
