"""Append only supplementary (makeup) dosing ledger.

A makeup dose is a correction applied after the fact. It is written in two
steps that mirror each other so neither side can silently drift:

1. an ordinary immutable audit entry is appended (the raw dosing record),
2. a makeup marker is appended here naming that audit id and the shift the
   correction settles against.

Settlement groups a makeup marker's audit entry by ``for_shift`` rather than
by its actual dosing time. When the crash window sits between the two writes,
the audit entry exists without a marker (it is simply a normal late dose) or a
marker exists without its audit entry (a dangling marker, excluded and
reported until :meth:`recover` prunes it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.audit import Auditor
from waterplant.inventory import Inventory
from waterplant.store.commands import append_command, load_commands, clear_commands
from waterplant.store.store import Store

from .meters import MeterLog
from .timeutil import stamp_now

MAKEUP_KEY = "settle:makeups"


def makeup_ref(audit_id: str) -> str:
    return f"makeup:{audit_id}"


@dataclass(frozen=True)
class Makeup:
    """One supplementary dose correction tied to an audit entry and a shift."""

    audit_id: str
    chemical: str
    amount: float
    for_shift: str
    reason: str
    recorded_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "audit_id": self.audit_id,
            "chemical": self.chemical,
            "amount": self.amount,
            "for_shift": self.for_shift,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
        }


class MakeupLedger:
    """Records makeup doses without ever editing an existing audit entry."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _load(self) -> list[Makeup]:
        makeups: list[Makeup] = []
        for item in load_commands(self._store, MAKEUP_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            try:
                amount = float(payload.get("amount", ""))
            except ValueError:
                continue
            makeups.append(
                Makeup(
                    audit_id=str(payload.get("audit_id", "")),
                    chemical=str(payload.get("chemical", "")),
                    amount=amount,
                    for_shift=str(payload.get("for_shift", "")),
                    reason=str(payload.get("reason", "")),
                    recorded_at=str(payload.get("recorded_at", "")),
                )
            )
        return makeups

    def add(
        self,
        chemical: str,
        amount: float,
        for_shift: str,
        reason: str = "",
        at: str | None = None,
        draw_stock: bool = True,
        meter_value: float | None = None,
    ) -> Makeup:
        """Write the raw audit entry first, then the linked stock and meter.

        A correction is physically performed later, so the stock draw and the
        cumulative meter reading that accompany it are tagged with
        ``makeup:<audit_id>``. Settlement reattributes those records to
        ``for_shift`` instead of the crew that was on duty when the correction
        happened.
        """

        if not chemical:
            raise ValueError("chemical is required")
        if amount < 0:
            raise ValueError("makeup amount must be non-negative")
        if amount > 1_000_000:
            raise ValueError("makeup amount exceeds the supported range")
        if not for_shift:
            raise ValueError("for_shift is required")
        entry = Auditor(self._store).record(chemical, f"{amount:.4f}", at=at)
        ref = makeup_ref(entry.id)
        if draw_stock and amount > 0:
            Inventory(self._store).consume(chemical, amount, at=at, ref=ref)
        if meter_value is not None:
            MeterLog(self._store).reading(chemical, meter_value, at=at, source=ref)
        makeup = Makeup(
            audit_id=entry.id,
            chemical=chemical,
            amount=amount,
            for_shift=for_shift,
            reason=reason,
            recorded_at=stamp_now(),
        )
        append_command(self._store, MAKEUP_KEY, json.dumps(makeup.as_dict(), ensure_ascii=False))
        return makeup

    def makeups(self, chemical: str | None = None, shift_key: str | None = None) -> list[Makeup]:
        result = self._load()
        if chemical is not None:
            result = [makeup for makeup in result if makeup.chemical == chemical]
        if shift_key is not None:
            result = [makeup for makeup in result if makeup.for_shift == shift_key]
        return result

    def index_by_audit(self) -> dict[str, Makeup]:
        return {makeup.audit_id: makeup for makeup in self._load()}

    def dangling(self) -> list[Makeup]:
        """Markers whose raw audit entry never landed (partial write)."""

        known = {entry.id for entry in Auditor(self._store).entries()}
        return [makeup for makeup in self._load() if makeup.audit_id not in known]

    def recover(self) -> int:
        """Drop dangling markers so a rerun can re-enter the correction.

        Returns how many markers were pruned. The audit side is untouched:
        raw records are never rewritten, only the derived marker list is
        repaired.
        """

        dangling = self.dangling()
        if not dangling:
            return 0
        good = [
            makeup for makeup in self._load() if makeup.audit_id not in {m.audit_id for m in dangling}
        ]
        clear_commands(self._store, MAKEUP_KEY)
        for makeup in good:
            append_command(self._store, MAKEUP_KEY, json.dumps(makeup.as_dict(), ensure_ascii=False))
        return len(dangling)
