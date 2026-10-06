"""Gross meter readings for chemicals, including wrapping cumulative totals.

The chlorine accumulator wraps at 100 so its live value alone cannot answer
"how much passed the meter during this shift". Readings are therefore logged
in reading order and the between-boundaries delta is reconstructed with wrap
detection. A reading is attributed to the shift whose window contains its
timestamp: the delta from the previous reading belongs to that shift.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .timeutil import epoch_of, stamp_now

READINGS_KEY = "settle:meter-readings"
CAPS_KEY = "settle:meter-caps"

DEFAULT_WRAP_CAP = 0.0  # 0 means the meter does not wrap


@dataclass(frozen=True)
class MeterReading:
    """One cumulative meter value observed at a point in time."""

    id: str
    chemical: str
    value: float
    at: str
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "chemical": self.chemical,
            "value": self.value,
            "at": self.at,
            "source": self.source,
        }


class MeterLog:
    """Stores cumulative readings and reconstructs per-shift consumption."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def wrap_cap(self, chemical: str) -> float:
        raw, present = self._store.get(f"{CAPS_KEY}:{chemical}")
        if not present:
            # The chlorine quota accumulator historically wraps at 100.
            return 100.0 if chemical == "chlorine" else DEFAULT_WRAP_CAP
        try:
            return float(raw)
        except ValueError:
            return DEFAULT_WRAP_CAP

    def set_wrap_cap(self, chemical: str, cap: float) -> float:
        if cap < 0:
            raise ValueError("wrap cap must be non-negative")
        self._store.put(f"{CAPS_KEY}:{chemical}", f"{cap:.4f}")
        return cap

    def reading(
        self,
        chemical: str,
        value: float,
        at: str | None = None,
        source: str = "manual",
    ) -> MeterReading:
        if not chemical:
            raise ValueError("chemical is required")
        if value < 0:
            raise ValueError("meter reading must be non-negative")
        stamp = at or stamp_now()
        epoch_of(stamp)
        record = MeterReading(
            id=uuid.uuid4().hex,
            chemical=chemical,
            value=float(value),
            at=stamp,
            source=source,
        )
        append_command(self._store, READINGS_KEY, json.dumps(record.as_dict(), ensure_ascii=False))
        return record

    def readings(self, chemical: str | None = None) -> list[MeterReading]:
        records: list[MeterReading] = []
        for item in load_commands(self._store, READINGS_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("chemical", ""))
            if chemical is not None and name != chemical:
                continue
            try:
                value = float(payload.get("value", ""))
            except ValueError:
                continue
            records.append(
                MeterReading(
                    id=str(payload.get("id", "")),
                    chemical=name,
                    value=value,
                    at=str(payload.get("at", "")),
                    source=str(payload.get("source", "")),
                )
            )
        return sorted(records, key=lambda record: epoch_of(record.at))

    @staticmethod
    def _delta(previous: float, current: float, cap: float) -> float:
        """Gross amount between two cumulative readings.

        With a wrapping meter a smaller current value means one or more wraps
        happened. A backwards jump on a non wrapping meter is a reading fault
        and is surfaced as a negative delta so reconciliation flags the
        segment rather than silently hiding it.
        """

        if current + 1e-9 >= previous:
            return current - previous
        if cap > 0:
            return cap - previous + current
        return current - previous

    def deltas_by_shift(
        self,
        chemical: str,
        shift_windows: list[tuple[str, int, int]],
        reattribute: dict[str, str] | None = None,
    ) -> dict[str, dict[str, object]]:
        """Sum meter deltas attributed to each supplied shift window.

        Normally a delta belongs to the shift containing the newer reading.
        ``reattribute`` maps a reading source (typically
        ``makeup:<audit id>``) to a different shift key, so the delta produced
        by a makeup reading taken during a later crew is collected into the
        shift the correction was made for. The previous reading is always the
        globally preceding one, which counts every physical delta exactly
        once. A first-ever reading counts its full cumulative value.
        """

        reattribute = reattribute or {}
        cap = self.wrap_cap(chemical)
        readings = self.readings(chemical)
        rows: dict[str, dict[str, object]] = {}
        previous: MeterReading | None = None
        seen_any = False
        for record in readings:
            current_epoch = epoch_of(record.at)
            owner = reattribute.get(record.source)
            if owner is None:
                for shift_key, start, end in shift_windows:
                    if start < current_epoch <= end:
                        owner = shift_key
                        break
            if previous is None and not seen_any:
                delta: float | None = record.value
                seen_any = True
            elif previous is not None:
                delta = self._delta(previous.value, record.value, cap)
            else:
                delta = None
            if owner is not None:
                row = rows.setdefault(
                    owner, {"meter": 0.0, "reading_ids": [], "faults": []}
                )
                if delta is not None:
                    row["meter"] = float(row["meter"]) + delta
                    if delta < 0:
                        row["faults"].append(
                            f"reading {record.id} went backwards on a non wrapping meter"
                        )
                row["reading_ids"].append(record.id)
            previous = record
        return rows

    def describe(self) -> str:
        chemicals = sorted({record.chemical for record in self.readings()})
        return f"settle meter readings={len(self.readings())} chemicals={chemicals}"
