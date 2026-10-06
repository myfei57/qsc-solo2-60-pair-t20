"""Append-only observations for chemical cumulative meters."""

from __future__ import annotations

import json

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .report import CumulativeEntry
from .timeutil import now_string, parse_time

CUMULATIVE_KEY = "dosing:cumulative"
DEFAULT_ROLLOVER = 100.0
EPSILON = 1e-9


class CumulativeLedger:
    """Stores meter readings and computes bounded shift deltas."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(
        self,
        chemical: str,
        value: float,
        at: str | None = None,
        rollover: float | None = DEFAULT_ROLLOVER,
    ) -> CumulativeEntry:
        if value < 0:
            raise ValueError("cumulative value must be non-negative")
        if rollover is not None and rollover <= 0:
            raise ValueError("rollover must be positive")
        entry = CumulativeEntry(
            chemical=chemical,
            time=at or now_string(),
            value=value,
            rollover=rollover,
        )
        append_command(self._store, CUMULATIVE_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def entries(self, chemical: str) -> list[CumulativeEntry]:
        rows: list[CumulativeEntry] = []
        for item in load_commands(self._store, CUMULATIVE_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict) or str(payload.get("chemical", "")) != chemical:
                continue
            raw_rollover = payload.get("rollover", DEFAULT_ROLLOVER)
            rows.append(
                CumulativeEntry(
                    chemical=str(payload.get("chemical", "")),
                    time=str(payload.get("time", "")),
                    value=float(payload.get("value", 0.0)),
                    rollover=None if raw_rollover is None else float(raw_rollover),
                )
            )
        return sorted(rows, key=lambda item: parse_time(item.time))

    def delta(self, chemical: str, start_epoch: int, end_epoch: int) -> tuple[float | None, dict[str, object] | None]:
        """Use the observations bracketing a shift; never interpolate readings."""

        readings = self.entries(chemical)
        before = [entry for entry in readings if parse_time(entry.time) <= start_epoch]
        after = [entry for entry in readings if parse_time(entry.time) >= end_epoch]
        if not before or not after:
            return None, None
        start = before[-1]
        end = after[0]
        delta = end.value - start.value
        if delta < -EPSILON and end.rollover is not None:
            delta += end.rollover
        return delta, {
            "start": start.as_dict(),
            "end": end.as_dict(),
            "shift_start_epoch": start_epoch,
            "shift_end_epoch": end_epoch,
        }
