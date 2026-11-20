"""How the consumer acks, requeues or drops messages when things go wrong."""

import json

import psycopg
import pytest

from consumer.consume import Consumer
from tests.conftest import FakeEmbedder


class FakeMessage:
    """The parts of an aio_pika incoming message the consumer uses."""

    def __init__(self, body: bytes, message_id: str | None = "m1", redelivered: bool = False):
        self.body = body
        self.message_id = message_id
        self.redelivered = redelivered
        self.outcome: str | None = None

    async def ack(self) -> None:
        self.outcome = "ack"

    async def nack(self, requeue: bool = True) -> None:
        self.outcome = "requeue" if requeue else "drop"


class BrokenEmbedder(FakeEmbedder):
    def embed(self, texts):
        raise RuntimeError("model crashed")


def event(reg: str = "SYN-000001", nature: str = "Toy retail") -> bytes:
    return json.dumps(
        {
            "type": "company-upserted",
            "registrationNumber": reg,
            "updatedAt": "2026-08-14T09:00:00+08:00",
            "data": {"companyName": "Acme", "natureOfBusiness": nature},
        }
    ).encode()


@pytest.fixture
async def consumer(db, test_database):
    c = Consumer(test_database, FakeEmbedder())
    await c.connect()
    yield c
    await c.close_quietly()


async def count(db, table: str) -> int:
    await db.commit()  # see the consumer's committed writes
    return (await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone())[0]


async def test_valid_message_is_stored_and_acked(consumer, db):
    msg = FakeMessage(event())
    await consumer.handle(msg)

    assert msg.outcome == "ack"
    assert await count(db, "companies") == 1


async def test_bad_message_is_acked_and_raw_body_kept(consumer, db):
    msg = FakeMessage(b"{not json")
    await consumer.handle(msg)

    assert msg.outcome == "ack"  # retrying won't fix it
    assert await count(db, "raw_events") == 1
    assert await count(db, "companies") == 0


async def test_message_without_id_is_acked(consumer, db):
    msg = FakeMessage(event(), message_id=None)
    await consumer.handle(msg)

    assert msg.outcome == "ack"
    assert await count(db, "raw_events") == 0


async def test_unexpected_error_requeues_once(consumer, db):
    consumer.embedder = BrokenEmbedder()
    msg = FakeMessage(event())
    await consumer.handle(msg)

    assert msg.outcome == "requeue"
    assert await count(db, "companies") == 0  # rolled back, not half-written


async def test_unexpected_error_on_redelivery_drops(consumer, db):
    consumer.embedder = BrokenEmbedder()
    msg = FakeMessage(event(), redelivered=True)
    await consumer.handle(msg)

    assert msg.outcome == "drop"
    assert await count(db, "raw_events") == 1  # still there for replay


async def test_consumer_keeps_working_after_an_error(consumer, db):
    consumer.embedder = BrokenEmbedder()
    await consumer.handle(FakeMessage(event("SYN-000001"), message_id="m1"))

    consumer.embedder = FakeEmbedder()
    msg = FakeMessage(event("SYN-000002"), message_id="m2")
    await consumer.handle(msg)

    assert msg.outcome == "ack"
    assert await count(db, "companies") == 1


async def test_lost_connection_requeues_and_reconnects(consumer, db, test_database):
    # Kill the consumer's Postgres session, as a Postgres restart would.
    pid = consumer.db.info.backend_pid
    with psycopg.connect(test_database, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))

    msg = FakeMessage(event(), message_id="m1")
    await consumer.handle(msg)
    assert msg.outcome == "requeue"
    assert consumer.db.info.backend_pid != pid  # reconnected

    # RabbitMQ redelivers it; this time it goes through.
    retry = FakeMessage(event(), message_id="m1", redelivered=True)
    await consumer.handle(retry)
    assert retry.outcome == "ack"
    assert await count(db, "companies") == 1
