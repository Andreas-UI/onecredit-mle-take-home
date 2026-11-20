from typing import Final, Any

import re
import json
from datetime import timezone, timedelta, datetime, UTC

REG_NO_PATTERN: Final = re.compile(r"SYN-\d{6}")
MALAYSIA_TIMEZONE: Final = timezone(timedelta(hours=8), name="Asia/Kuala_Lumpur")


def normalize_registration_number(registration_number: object) -> str:
    # Validate input type (None, number, or other types that is not string)
    if not isinstance(registration_number, str):
        raise ValueError(
            f"registration_number must be a string, got {type(registration_number).__name__}"
        )

    # Clean and normalize inconsistent registration numbers (e.g., extra whitespace, lowercase)
    cleaned = registration_number.strip().upper()

    # Validate if registration number is not "SYN-XXXXXX" format
    if not REG_NO_PATTERN.fullmatch(cleaned):
        raise ValueError(f"Invalid registration number: {registration_number}")

    return cleaned


def normalize_timestamp(timestamp: object) -> datetime:
    # Validate timestamp if it is a boolean or None
    if isinstance(timestamp, bool) or timestamp is None:
        raise ValueError(f"Invalid timestamp: {timestamp}")

    # Normalize milisecond timestamp
    if isinstance(timestamp, int):
        return datetime.fromtimestamp(timestamp / 1000, tz=UTC)

    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp.strip())
        except ValueError:
            raise ValueError(f"Invalid timestamp: {timestamp}")

        return parsed if parsed.tzinfo else parsed.replace(tzinfo=MALAYSIA_TIMEZONE)
    raise ValueError(f"Invalid timestamp: {timestamp}")


def normalize_text(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    return " ".join(text.strip().split()) or None


def normalize_fields(data: object) -> dict:
    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ValueError(f"Expected dict, got {type(data).__name__}")

    fields = {}
    for source_key, column in {
        "companyName": "company_name",
        "natureOfBusiness": "nature_of_business",
        "industry": "industry",
        "state": "state",
    }.items():
        value = normalize_text(data.get(source_key))
        if value is not None:
            fields[column] = value

    if "state" in fields:
        fields["state"] = fields["state"].title()

    return fields


def parse_event(body: bytes) -> dict:
    try:
        payload: Any = json.loads(body)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Invalid JSON: {e}") from e

    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got {type(payload).__name__}")

    event_type = payload.get("type")
    if event_type not in {"company-upserted", "company-struck-off"}:
        raise ValueError(f"Invalid event type: {event_type!r}")

    registration_number = normalize_registration_number(
        payload.get("registrationNumber")
    )
    updated_at = normalize_timestamp(payload.get("updatedAt"))

    fields = normalize_fields(payload.get("data"))

    return {
        "type": event_type,
        "registration_number": registration_number,
        "updated_at": updated_at,
        "fields": fields,
    }
