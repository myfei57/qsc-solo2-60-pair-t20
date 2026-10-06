"""Append only audit entries for every chemical dose."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"


@dataclass(frozen=True)
class Entry:
    """One recorded dosing decision."""

    id: str
    time: str
    kind: str
    detail: str
    dose_type: str = "dose"
    raw_amount: float | None = None
    calibration: float | None = None
    concentration: float | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "time": self.time,
            "kind": self.kind,
            "detail": self.detail,
            "dose_type": self.dose_type,
        }
        if self.raw_amount is not None:
            payload["raw_amount"] = self.raw_amount
        if self.calibration is not None:
            payload["calibration"] = self.calibration
        if self.concentration is not None:
            payload["concentration"] = self.concentration
        return payload


class Auditor:
    """Records and queries dosing entries."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(
        self,
        kind: str,
        detail: str,
        at: str | None = None,
        dose_type: str = "dose",
        raw_amount: float | None = None,
        calibration: float | None = None,
        concentration: float | None = None,
    ) -> Entry:
        if dose_type not in {"dose", "supplement"}:
            raise ValueError("dose_type must be dose or supplement")
        entry = Entry(
            id=uuid.uuid4().hex,
            time=at
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            kind=kind,
            detail=detail,
            dose_type=dose_type,
            raw_amount=raw_amount,
            calibration=calibration,
            concentration=concentration,
        )
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def supplement(
        self,
        kind: str,
        amount: float,
        at: str | None = None,
        raw_amount: float | None = None,
        calibration: float | None = None,
        concentration: float | None = None,
    ) -> Entry:
        """Append a late/manual dosing record without changing history."""

        if amount < 0:
            raise ValueError("supplement amount must be non-negative")
        return self.record(
            kind,
            f"{amount:.4f}",
            at=at,
            dose_type="supplement",
            raw_amount=raw_amount,
            calibration=calibration,
            concentration=concentration,
        )

    def entries(self) -> list[Entry]:
        entries: list[Entry] = []
        for item in load_commands(self._store, AUDIT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            entries.append(
                Entry(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    kind=str(payload.get("kind", "")),
                    detail=str(payload.get("detail", "")),
                    dose_type=str(payload.get("dose_type", "dose")),
                    raw_amount=_optional_float(payload.get("raw_amount")),
                    calibration=_optional_float(payload.get("calibration")),
                    concentration=_optional_float(payload.get("concentration")),
                )
            )
        return entries

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric dose recorded for each chemical."""

        totals: dict[str, float] = {}
        for entry in self.entries():
            try:
                amount = float(entry.detail)
            except ValueError:
                continue
            totals[entry.kind] = totals.get(entry.kind, 0.0) + amount
        return totals

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
