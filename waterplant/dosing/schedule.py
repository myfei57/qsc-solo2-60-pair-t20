"""Shift boundaries defined by crew handovers rather than fixed whole hours."""

from __future__ import annotations

import json

from waterplant.store.store import Store

from .report import Shift, ShiftScheduleState
from .timeutil import format_time, parse_time

SCHEDULE_KEY = "dosing:shift-schedule"
BOUNDARY_POLICY = "handover"
DEFAULT_HANDOVER_MINUTES = [0, 480, 960]
DEFAULT_TEAMS = ["A", "B", "C"]


class ShiftSchedule:
    """Maps an event time to the shift defined by the crew roster."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _load(self) -> tuple[list[int], list[str]]:
        raw, present = self._store.get(SCHEDULE_KEY)
        if not present:
            return list(DEFAULT_HANDOVER_MINUTES), list(DEFAULT_TEAMS)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ValueError("shift schedule is corrupt") from exc
        if not isinstance(payload, dict):
            raise ValueError("shift schedule must be an object")
        minutes = payload.get("handover_minutes", DEFAULT_HANDOVER_MINUTES)
        teams = payload.get("teams", DEFAULT_TEAMS)
        if not isinstance(minutes, list) or not isinstance(teams, list):
            raise ValueError("shift schedule lists have the wrong type")
        pairs = sorted(zip(minutes, teams), key=lambda item: int(item[0]))
        return [int(minute) for minute, _ in pairs], [str(team) for _, team in pairs]

    def set(self, handover_minutes: list[int], teams: list[str] | None = None) -> ShiftScheduleState:
        validate_schedule(handover_minutes, teams or [])
        stored_teams = list(teams) if teams else default_teams(len(handover_minutes))
        pairs = sorted(zip(handover_minutes, stored_teams), key=lambda item: item[0])
        state = ShiftScheduleState(
            handover_minutes=[minute for minute, _ in pairs],
            teams=[team for _, team in pairs],
            boundary_policy=BOUNDARY_POLICY,
        )
        self._store.put(SCHEDULE_KEY, json.dumps(state.as_dict(), ensure_ascii=False))
        return state

    def state(self) -> ShiftScheduleState:
        minutes, teams = self._load()
        return ShiftScheduleState(
            handover_minutes=minutes,
            teams=teams,
            boundary_policy=BOUNDARY_POLICY,
        )

    def locate(self, at: str | int) -> Shift:
        """Return the shift that contains ``at`` under the current roster."""

        event = parse_time(at) if isinstance(at, str) else int(at)
        minutes, teams = self._load()
        pairs = sorted(zip(minutes, teams or default_teams(len(minutes))), key=lambda item: item[0])
        day_start = event - (event % 86400)
        selected_position = len(pairs) - 1
        for position, (minute, _team) in enumerate(pairs):
            if event >= day_start + minute * 60:
                selected_position = position
            else:
                break
        next_position = (selected_position + 1) % len(pairs)
        next_day_offset = 1 if next_position == 0 else 0
        selected_minute, team = pairs[selected_position]
        next_minute = pairs[next_position][0]
        start = day_start + selected_minute * 60
        end = day_start + next_day_offset * 86400 + next_minute * 60
        start_date = format_time(start)[:10]
        key = f"{start_date}-{team}"
        return Shift(
            key=key,
            team=team,
            start=format_time(start),
            end=format_time(end),
            start_epoch=start,
            end_epoch=end,
        )

    def shifts_between(self, start_epoch: int, end_epoch: int) -> list[Shift]:
        """Return every shift overlapping a time interval."""

        shifts: list[Shift] = []
        cursor = self.locate(start_epoch)
        while cursor.start_epoch < end_epoch:
            shifts.append(cursor)
            cursor = self.locate(cursor.end_epoch)
        return shifts

    def describe(self) -> str:
        state = self.state()
        return (
            f"shift schedule boundary={state.boundary_policy} "
            f"handovers={state.handover_minutes} teams={state.teams}"
        )


def validate_schedule(handover_minutes: list[int], teams: list[str]) -> None:
    """Reject schedules that cannot unambiguously define a shift."""

    if not 1 <= len(handover_minutes) <= 4:
        raise ValueError("schedule must contain one to four handovers")
    if any(not isinstance(item, int) for item in handover_minutes):
        raise ValueError("handover minutes must be integers")
    if any(item < 0 or item >= 1440 for item in handover_minutes):
        raise ValueError("handover minutes must be within a day")
    if len(set(handover_minutes)) != len(handover_minutes):
        raise ValueError("handover minutes must be unique")
    if teams and len(teams) != len(handover_minutes):
        raise ValueError("teams must align one-for-one with handovers")
    if teams and any(not team.strip() for team in teams):
        raise ValueError("team names must not be empty")
    if teams and len(set(teams)) != len(teams):
        raise ValueError("team names must be unique")


def default_teams(count: int) -> list[str]:
    """Build names for an unnamed schedule while reusing the normal A/B/C roster."""

    defaults = list(DEFAULT_TEAMS)
    if count <= len(defaults):
        return defaults[:count]
    return defaults + [f"shift-{index + 1}" for index in range(len(defaults), count)]
