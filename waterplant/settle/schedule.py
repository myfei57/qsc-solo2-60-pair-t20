"""Crew shift arrangements used to divide dosing into settlement periods.

Boundary policy (fixed by crew arrangement, not by wall clock habit):

* ``HANDOVER`` (default): a shift starts at the handover minute its crew is
  rostered for. Boundaries may therefore sit at non whole-hour times such as
  07:30/15:30/23:30.
* ``ON_HOUR``: handover minutes are forced to zero. Shift lengths still come
  from the rostered crew arrangement; only the boundary offset is reset.

The roster is a daily repeating list of segments ordered from the earliest
handover of a UTC day. Every shift key is the UTC stamp of its start minute
plus the owning crew, so shifts are unique even when two crews swap order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.store.store import Store

from .timeutil import epoch_of, parse_stamp, stamp_epoch, utc_now

SCHEDULE_KEY = "settle:shift-schedule"

HANDOVER = "handover"
ON_HOUR = "on_hour"
BOUNDARY_POLICIES = (HANDOVER, ON_HOUR)

MINUTES_PER_DAY = 24 * 60
DEFAULT_ROSTER = (("A", 0), ("B", 8 * 60), ("C", 16 * 60))


DAY_ANCHOR = int(parse_stamp("1970-01-01T00:00:00Z").timestamp())
SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class Shift:
    """One rostered interval belonging to a crew."""

    key: str
    crew: str
    start_epoch: int
    end_epoch: int

    @property
    def start(self) -> str:
        return stamp_epoch(self.start_epoch)

    @property
    def end(self) -> str:
        return stamp_epoch(self.end_epoch)

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "crew": self.crew,
            "start": self.start,
            "end": self.end,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
        }


@dataclass(frozen=True)
class RosterSegment:
    """A crew and the minute past midnight (UTC) its handover happens."""

    crew: str
    start_minute: int

    def as_dict(self) -> dict[str, object]:
        return {"crew": self.crew, "start_minute": self.start_minute}


def validate_roster(segments: list[tuple[str, int]]) -> None:
    if not segments:
        raise ValueError("roster must contain at least one crew segment")
    seen_crews: set[str] = set()
    seen_minutes: set[int] = set()
    previous = -1
    for crew, minute in segments:
        if not crew:
            raise ValueError("crew name is required")
        if crew in seen_crews:
            raise ValueError(f"crew {crew} is rostered more than once per day")
        seen_crews.add(crew)
        if minute < 0 or minute >= MINUTES_PER_DAY:
            raise ValueError("handover minute must fall inside one day")
        if minute in seen_minutes:
            raise ValueError("two crews cannot share a handover minute")
        seen_minutes.add(minute)
        if minute < previous:
            raise ValueError("roster segments must be ordered by handover minute")
        previous = minute


class ShiftSchedule:
    """Persists the crew arrangement and maps dosing times onto shifts."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _document(self) -> dict[str, object]:
        raw, present = self._store.get(SCHEDULE_KEY)
        if not present:
            return {"boundary_policy": HANDOVER, "roster": [
                {"crew": crew, "start_minute": minute} for crew, minute in DEFAULT_ROSTER
            ]}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"boundary_policy": HANDOVER, "roster": [
                {"crew": crew, "start_minute": minute} for crew, minute in DEFAULT_ROSTER
            ]}
        if not isinstance(parsed, dict):
            return {"boundary_policy": HANDOVER, "roster": []}
        return parsed

    def boundary_policy(self) -> str:
        policy = str(self._document().get("boundary_policy", HANDOVER))
        return policy if policy in BOUNDARY_POLICIES else HANDOVER

    def roster(self) -> list[RosterSegment]:
        segments: list[RosterSegment] = []
        roster = self._document().get("roster", [])
        if not isinstance(roster, list):
            return segments
        for item in roster:
            if not isinstance(item, dict):
                continue
            segments.append(
                RosterSegment(
                    crew=str(item.get("crew", "")),
                    start_minute=int(item.get("start_minute", 0)),
                )
            )
        return segments

    def arrange(
        self, roster: list[tuple[str, int]], boundary_policy: str = HANDOVER
    ) -> list[RosterSegment]:
        """Replace the crew arrangement, pinning down the boundary policy."""

        if boundary_policy not in BOUNDARY_POLICIES:
            raise ValueError(f"boundary policy must be one of {BOUNDARY_POLICIES}")
        validate_roster(roster)
        if boundary_policy == ON_HOUR:
            # The arrangement still defines crew order and spacing; whole hour
            # boundaries round every handover down to the hour.
            roster = [(crew, (minute // 60) * 60) for crew, minute in roster]
            validate_roster(roster)
        document = {
            "boundary_policy": boundary_policy,
            "roster": [{"crew": crew, "start_minute": minute} for crew, minute in roster],
        }
        self._store.put(SCHEDULE_KEY, json.dumps(document, ensure_ascii=False))
        return [RosterSegment(crew, minute) for crew, minute in roster]

    def use_defaults(self) -> None:
        if self._store.get(SCHEDULE_KEY)[1]:
            return
        self.arrange(list(DEFAULT_ROSTER), HANDOVER)

    def _segments_for(self, day_index: int) -> list[tuple[int, str, int]]:
        """Return ``(absolute_start_epoch, crew, length_seconds)`` for a day."""

        roster = self.roster()
        if not roster:
            roster = [RosterSegment(crew, minute) for crew, minute in DEFAULT_ROSTER]
        day_origin = DAY_ANCHOR + day_index * SECONDS_PER_DAY
        result: list[tuple[int, str, int]] = []
        for index, segment in enumerate(roster):
            if index + 1 < len(roster):
                next_minute = roster[index + 1].start_minute
            else:
                next_minute = MINUTES_PER_DAY + roster[0].start_minute
            start = day_origin + segment.start_minute * 60
            length = (next_minute - segment.start_minute) * 60
            result.append((start, segment.crew, length))
        return result

    def shift_for(self, when: str) -> Shift:
        """Find the shift in effect at ``when`` under the current arrangement."""

        target = epoch_of(when)
        day_index = (target - DAY_ANCHOR) // SECONDS_PER_DAY
        for candidate_day in (day_index - 1, day_index, day_index + 1):
            for start, crew, length in self._segments_for(candidate_day):
                if start <= target < start + length:
                    return Shift(
                        key=f"{stamp_epoch(start)}|{crew}",
                        crew=crew,
                        start_epoch=start,
                        end_epoch=start + length,
                    )
        raise ValueError(f"no rostered shift covers {when}")

    def shifts_between(self, start_when: str, end_when: str) -> list[Shift]:
        """Every shift overlapping the inclusive window, in chronological order."""

        start_epoch = epoch_of(start_when)
        end_epoch = epoch_of(end_when)
        if end_epoch < start_epoch:
            raise ValueError("window end precedes its start")
        first_day = (start_epoch - DAY_ANCHOR) // SECONDS_PER_DAY - 1
        last_day = (end_epoch - DAY_ANCHOR) // SECONDS_PER_DAY + 1
        shifts: list[Shift] = []
        for day_index in range(first_day, last_day + 1):
            for start, crew, length in self._segments_for(day_index):
                if start + length <= start_epoch or start > end_epoch:
                    continue
                shifts.append(
                    Shift(
                        key=f"{stamp_epoch(start)}|{crew}",
                        crew=crew,
                        start_epoch=start,
                        end_epoch=start + length,
                    )
                )
        return sorted(shifts, key=lambda shift: shift.start_epoch)

    def state(self) -> dict[str, object]:
        return {
            "boundary_policy": self.boundary_policy(),
            "roster": [segment.as_dict() for segment in self.roster()],
        }

    def describe(self) -> str:
        roster = self.roster()
        return (
            f"shift policy={self.boundary_policy()} crews={len(roster)} "
            f"arrangement={[segment.crew for segment in roster]}"
        )

    def current(self, now: str | None = None) -> Shift:
        return self.shift_for(now or stamp_epoch(int(utc_now().timestamp())))
