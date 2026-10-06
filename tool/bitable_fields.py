"""Public helpers for reading Feishu Bitable record payloads.

Formerly private functions inside data/repositories.py. Value coercions and
alias resolution are transport concerns shared by every workflow.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def record_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("fields", {})
    return dict(value) if isinstance(value, Mapping) else {}


def scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return scalar(value[0]) if value else ""
    if isinstance(value, Mapping):
        for key in (
            "record_id",
            "id",
            "open_id",
            "user_id",
            "link",
            "text",
            "texts",
            "mobile",
            "name",
            "value",
        ):
            if key in value:
                return scalar(value[key])
    return str(value)


def display_name(value: Any) -> str:
    if isinstance(value, list):
        return display_name(value[0]) if value else ""
    if isinstance(value, Mapping) and value.get("name"):
        return str(value["name"])
    return scalar(value)


def references(value: Any) -> list[str]:
    """Return every usable identifier exposed by a Feishu relation/person field."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(references(item))
        return list(dict.fromkeys(result))
    if isinstance(value, Mapping):
        result = []
        for key in (
            "record_ids",
            "record_id",
            "id",
            "person_id",
            "open_id",
            "user_id",
            "text",
            "name",
            "value",
        ):
            if key in value:
                result.extend(references(value[key]))
        return list(dict.fromkeys(result))
    return [str(value)]


def alias_index(rows: Iterable[tuple[str, Iterable[str]]]) -> dict[str, str]:
    """Build an alias map while rejecting ambiguous duplicate display names."""
    aliases: dict[str, str | None] = {}
    for canonical_id, candidates in rows:
        if not canonical_id:
            continue
        for candidate in candidates:
            alias = str(candidate).strip()
            if not alias:
                continue
            if alias in aliases and aliases[alias] != canonical_id:
                aliases[alias] = None
            else:
                aliases[alias] = canonical_id
    return {alias: target for alias, target in aliases.items() if target}


def resolve_reference(value: Any, aliases: Mapping[str, str]) -> str:
    for candidate in references(value):
        if candidate in aliases:
            return aliases[candidate]
    return ""


def field_boolean(value: Any, *, default: bool = True) -> bool:
    text = scalar(value).strip().lower()
    if not text:
        return default
    return text in {"true", "1", "yes", "是", "有效", "启用"}


def field_datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = scalar(value)
    if not text:
        raise ValueError("datetime field is empty")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


def record_submission_time(record: Mapping[str, Any], time_field: str = "") -> datetime | None:
    """Use the latest available form/record timestamp when checking a deadline."""
    fields = record_fields(record)
    values = [record.get("created_time"), record.get("last_modified_time")]
    if time_field:
        values.append(fields.get(time_field))
    timestamps = []
    for value in values:
        if value in (None, ""):
            continue
        try:
            timestamps.append(field_datetime(value))
        except (TypeError, ValueError, OverflowError):
            continue
    return max(timestamps) if timestamps else None
