"""Consumer tests against a real Postgres: ordering, duplicates, partial updates."""

import json

import pytest

from consumer.consume import apply_event
from consumer.parser import parse_event
from tests.conftest import FakeEmbedder

REG = "SYN-000001"


@pytest.fixture
def embedder():
    return FakeEmbedder()


def body(updated_at: str, event_type: str = "company-upserted", **data) -> bytes:
    return json.dumps(
        {
            "type": event_type,
            "registrationNumber": REG,
            "updatedAt": updated_at,
            "data": data or None,
        }
    ).encode()


async def deliver(db, embedder, raw: bytes, message_id: str) -> None:
    """What the consumer loop does for one message."""
    await db.execute(
        "INSERT INTO raw_events (message_id, body) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (message_id, raw.decode()),
    )
    await apply_event(db, parse_event(raw), message_id, embedder)
    await db.commit()


async def company(db) -> dict:
    cur = await db.execute(
        """
        SELECT company_name, nature_of_business, industry, state, status,
               embedding IS NOT NULL AS has_embedding, embedded_text, last_message_id
        FROM companies WHERE registration_number = %s
        """,
        (REG,),
    )
    row = await cur.fetchone()
    assert row is not None, "company was not stored"
    return dict(zip([c.name for c in cur.description], row))


async def test_new_company_is_stored_with_embedding(db, embedder):
    await deliver(
        db,
        embedder,
        body("2026-08-14T09:00:00+08:00", companyName="Acme", natureOfBusiness="Toy retail", industry="Retail", state="Johor"),
        "m1",
    )

    row = await company(db)
    assert row["company_name"] == "Acme"
    assert row["status"] == "active"
    assert row["has_embedding"]
    assert row["embedded_text"] == "Toy retail"
    assert row["last_message_id"] == "m1"
    assert embedder.calls == ["Toy retail"]


async def test_older_event_arriving_late_is_ignored(db, embedder):
    await deliver(db, embedder, body("2026-08-14T10:00:00+08:00", natureOfBusiness="New business"), "new")
    await deliver(db, embedder, body("2026-08-14T09:00:00+08:00", natureOfBusiness="Old business"), "old")

    row = await company(db)
    assert row["nature_of_business"] == "New business"
    assert row["embedded_text"] == "New business"
    assert row["last_message_id"] == "new"


async def test_out_of_order_across_timestamp_formats(db, embedder):
    # 04:41:32Z is 12:41:32+08:00, so it is newer than 12:40:32+08:00 despite looking smaller.
    await deliver(db, embedder, body("2026-08-14T04:41:32Z", natureOfBusiness="Newer"), "a")
    await deliver(db, embedder, body("2026-08-14T12:40:32+08:00", natureOfBusiness="Older"), "b")

    assert (await company(db))["nature_of_business"] == "Newer"


async def test_redelivered_message_is_idempotent(db, embedder):
    raw = body("2026-08-14T09:00:00+08:00", companyName="Acme", natureOfBusiness="Toy retail")
    await deliver(db, embedder, raw, "m1")
    await deliver(db, embedder, raw, "m1")

    assert (await company(db))["company_name"] == "Acme"
    assert embedder.calls == ["Toy retail"]  # not embedded twice


async def test_partial_update_keeps_other_fields(db, embedder):
    await deliver(
        db,
        embedder,
        body("2026-08-14T09:00:00+08:00", companyName="Acme", natureOfBusiness="Toy retail", industry="Retail", state="Johor"),
        "m1",
    )
    await deliver(db, embedder, body("2026-08-14T10:00:00+08:00", state="Perak", natureOfBusiness=None), "m2")

    row = await company(db)
    assert row["state"] == "Perak"
    assert row["company_name"] == "Acme"
    assert row["nature_of_business"] == "Toy retail"


async def test_unchanged_description_is_not_re_embedded(db, embedder):
    await deliver(db, embedder, body("2026-08-14T09:00:00+08:00", natureOfBusiness="Toy retail", state="Johor"), "m1")
    await deliver(db, embedder, body("2026-08-14T10:00:00+08:00", natureOfBusiness="Toy retail", state="Perak"), "m2")

    assert embedder.calls == ["Toy retail"]


async def test_changed_description_is_re_embedded(db, embedder):
    await deliver(db, embedder, body("2026-08-14T09:00:00+08:00", natureOfBusiness="Toy retail"), "m1")
    await deliver(db, embedder, body("2026-08-14T10:00:00+08:00", natureOfBusiness="Bakery"), "m2")

    assert embedder.calls == ["Toy retail", "Bakery"]
    assert (await company(db))["embedded_text"] == "Bakery"


async def test_company_without_description_is_stored_without_embedding(db, embedder):
    await deliver(db, embedder, body("2026-08-14T09:00:00+08:00", companyName="Acme", natureOfBusiness=""), "m1")

    row = await company(db)
    assert row["company_name"] == "Acme"
    assert not row["has_embedding"]
    assert embedder.calls == []


async def test_struck_off_after_upsert(db, embedder):
    await deliver(db, embedder, body("2026-08-14T09:00:00+08:00", companyName="Acme", natureOfBusiness="Toy retail"), "m1")
    await deliver(db, embedder, body("2026-08-14T16:00:00+08:00", event_type="company-struck-off"), "m2")

    row = await company(db)
    assert row["status"] == "struck_off"
    assert row["company_name"] == "Acme"  # details kept


async def test_struck_off_for_unknown_company_is_stored(db, embedder):
    await deliver(db, embedder, body("2026-08-14T16:00:00+08:00", event_type="company-struck-off"), "m1")

    assert (await company(db))["status"] == "struck_off"
