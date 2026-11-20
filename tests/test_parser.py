import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from consumer.parser import (
    normalize_fields,
    normalize_registration_number,
    normalize_timestamp,
    parse_event,
)

FEED = Path(__file__).parents[1] / "data" / "feed.jsonl"


def event(**overrides) -> bytes:
    payload = {
        "type": "company-upserted",
        "registrationNumber": "SYN-000001",
        "updatedAt": "2026-08-14T09:00:00+08:00",
        "data": {
            "companyName": "Acme Sdn. Bhd.",
            "natureOfBusiness": "Online retail of toys",
            "industry": "Retail",
            "state": "Selangor",
        },
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


# --- registration numbers ---------------------------------------------------


@pytest.mark.parametrize("raw", ["SYN-000001", " syn-000001 ", "Syn-000001\n"])
def test_registration_number_is_normalised(raw):
    assert normalize_registration_number(raw) == "SYN-000001"


@pytest.mark.parametrize("raw", [None, 119, "SYN-1", "ABC-000001", "SYN-0000011", ""])
def test_invalid_registration_number_is_rejected(raw):
    with pytest.raises(ValueError):
        normalize_registration_number(raw)


# --- timestamps -------------------------------------------------------------


def test_timestamp_with_offset_is_kept():
    ts = normalize_timestamp("2026-08-14T09:00:00+08:00")
    assert ts == datetime(2026, 8, 14, 1, 0, tzinfo=UTC)


def test_naive_timestamp_is_treated_as_malaysia_time():
    ts = normalize_timestamp("2026-08-14T09:00:00")
    assert ts.utcoffset() == timedelta(hours=8)


def test_epoch_milliseconds():
    ts = normalize_timestamp(1786671038000)
    assert ts == datetime.fromtimestamp(1786671038, tz=UTC)


def test_same_instant_in_different_formats_compares_equal():
    # The feed mixes +08:00, Z and epoch-ms for the same company; ordering must still work.
    a = normalize_timestamp("2026-08-14T12:40:32+08:00")
    b = normalize_timestamp("2026-08-14T04:40:32Z")
    c = normalize_timestamp(int(a.timestamp() * 1000))
    assert a == b == c


@pytest.mark.parametrize("raw", [None, True, "yesterday", 1.5, {}])
def test_invalid_timestamp_is_rejected(raw):
    with pytest.raises(ValueError):
        normalize_timestamp(raw)


# --- fields -----------------------------------------------------------------


def test_fields_are_mapped_and_cleaned():
    fields = normalize_fields(
        {
            "companyName": "  Acme   Sdn.  Bhd. ",
            "natureOfBusiness": "Online retail",
            "state": "KUALA LUMPUR",
            "industry": "Retail",
        }
    )
    assert fields == {
        "company_name": "Acme Sdn. Bhd.",
        "nature_of_business": "Online retail",
        "state": "Kuala Lumpur",
        "industry": "Retail",
    }


def test_null_and_empty_fields_are_dropped():
    # Dropped (not set to None) so the upsert keeps the previous value.
    fields = normalize_fields({"companyName": "Acme", "natureOfBusiness": "", "industry": None})
    assert fields == {"company_name": "Acme"}


def test_missing_data_means_no_field_changes():
    assert normalize_fields(None) == {}


def test_non_dict_data_is_rejected():
    with pytest.raises(ValueError):
        normalize_fields("companyName=Foo; state=Perak")


# --- whole events -----------------------------------------------------------


def test_parse_valid_event():
    parsed = parse_event(event())
    assert parsed["type"] == "company-upserted"
    assert parsed["registration_number"] == "SYN-000001"
    assert parsed["fields"]["nature_of_business"] == "Online retail of toys"


def test_struck_off_event():
    assert parse_event(event(type="company-struck-off", data=None))["type"] == "company-struck-off"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"null",
        b"[1, 2]",
        event(type="company-deleted"),
        event(type=None),
        event(registrationNumber=None),
        event(updatedAt=None),
    ],
)
def test_bad_events_raise_value_error(body):
    with pytest.raises(ValueError):
        parse_event(body)


def test_whole_feed_parses_or_is_rejected_cleanly():
    """Every line in the real feed either parses or raises ValueError, never anything else."""
    outcomes = Counter()
    registrations = set()
    for line in FEED.read_bytes().splitlines():
        try:
            registrations.add(parse_event(line)["registration_number"])
            outcomes["ok"] += 1
        except ValueError:
            outcomes["rejected"] += 1

    assert outcomes == {"ok": 2486, "rejected": 32}
    # No company is only reachable through a rejected event.
    assert len(registrations) == 2000
