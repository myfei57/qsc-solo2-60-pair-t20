"""Time parsing and formatting shared by every settlement component.

Audit entries carry ``%Y-%m-%dT%H:%M:%SZ`` stamps in UTC. Every settlement
calculation compares times as epoch seconds so clock arithmetic never depends
on string ordering.
"""

from __future__ import annotations

from datetime import datetime, timezone

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp_now() -> str:
    return format_stamp(utc_now())


def format_stamp(moment: datetime) -> str:
    """Render a timezone aware moment as a UTC ``Z`` stamp."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(STAMP_FORMAT)


def parse_stamp(value: str) -> datetime:
    """Parse the ``Z`` stamp format, accepting explicit offsets too."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def epoch_of(value: str) -> int:
    return int(parse_stamp(value).timestamp())


def stamp_epoch(seconds: int) -> str:
    return format_stamp(datetime.fromtimestamp(seconds, tz=timezone.utc))
