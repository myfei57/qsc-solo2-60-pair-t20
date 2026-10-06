"""UTC timestamp helpers for persisted inventory and dosing records."""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone


def now_string() -> str:
    """Return a second precision UTC timestamp."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> int:
    """Parse the service's UTC timestamps, including legacy seconds records."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return timegm(parsed.astimezone(timezone.utc).utctimetuple())


def format_time(epoch: int) -> str:
    """Format a UTC epoch second using the audit timestamp convention."""

    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
