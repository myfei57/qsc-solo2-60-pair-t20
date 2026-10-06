"""Append-only calibration and concentration basis history."""

from __future__ import annotations

import json

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .report import BasisEntry
from .timeutil import now_string, parse_time

BASIS_KEY = "dosing:basis"
DEFAULT_CALIBRATION = 1.0
DEFAULT_CONCENTRATION = 1.0


class BasisLedger:
    """Finds the calibration and concentration in force at an event time."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def entries(self, chemical: str | None = None) -> list[BasisEntry]:
        rows: list[BasisEntry] = []
        for item in load_commands(self._store, BASIS_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            entry = BasisEntry(
                chemical=str(payload.get("chemical", "")),
                effective_from=str(payload.get("effective_from", "")),
                calibration=float(payload.get("calibration", DEFAULT_CALIBRATION)),
                concentration=float(payload.get("concentration", DEFAULT_CONCENTRATION)),
            )
            if chemical is None or entry.chemical == chemical:
                rows.append(entry)
        return rows

    def set(
        self,
        chemical: str,
        calibration: float,
        concentration: float,
        effective_from: str | None = None,
    ) -> BasisEntry:
        validate_basis(calibration, concentration)
        entry = BasisEntry(
            chemical=chemical,
            effective_from=effective_from or now_string(),
            calibration=calibration,
            concentration=concentration,
        )
        append_command(self._store, BASIS_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def latest(self, chemical: str) -> BasisEntry:
        """Return the most recently recorded basis, or the service default."""

        entries = self.entries(chemical)
        if not entries:
            return BasisEntry(
                chemical=chemical,
                effective_from=now_string(),
                calibration=DEFAULT_CALIBRATION,
                concentration=DEFAULT_CONCENTRATION,
            )
        return max(entries, key=lambda item: parse_time(item.effective_from))

    def effective(self, chemical: str, at: str) -> BasisEntry:
        """Return the latest basis that had started before ``at``."""

        event = parse_time(at)
        candidates = [
            entry
            for entry in self.entries(chemical)
            if parse_time(entry.effective_from) <= event
            and 0 < entry.calibration <= 100
            and 0 < entry.concentration <= 100
        ]
        if not candidates:
            return BasisEntry(
                chemical=chemical,
                effective_from=at,
                calibration=DEFAULT_CALIBRATION,
                concentration=DEFAULT_CONCENTRATION,
            )
        return max(candidates, key=lambda item: parse_time(item.effective_from))


def validate_basis(calibration: float, concentration: float) -> None:
    """Reject unusable meter calibration or chemical concentration values."""

    if calibration <= 0 or calibration > 100:
        raise ValueError("calibration must be positive and no greater than 100")
    if concentration <= 0 or concentration > 100:
        raise ValueError("concentration must be positive and no greater than 100")
