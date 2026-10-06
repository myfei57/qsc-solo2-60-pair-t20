"""Append-only inventory receipts and draws used for shift reconciliation."""

from __future__ import annotations

import json

from waterplant.store.commands import load_commands
from waterplant.store.store import Store

from waterplant.dosing_movements import MOVEMENTS_KEY, InventoryMovement, append_movement
from waterplant.dosing_timeutil import parse_time


def save_movements(store: Store, items: list[str]) -> None:
    store.put(MOVEMENTS_KEY, json.dumps(items, ensure_ascii=False))


class MovementLedger:
    """Queries inventory movements by chemical and shift interval."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(
        self,
        chemical: str,
        kind: str,
        amount: float,
        lot_id: str = "",
        at: str | None = None,
    ) -> InventoryMovement:
        movement, items = append_movement(store, chemical, kind, amount, lot_id, at)
        save_movements(self._store, items)
        return movement

    def entries(self, chemical: str | None = None) -> list[InventoryMovement]:
        rows: list[InventoryMovement] = []
        for item in load_commands(self._store, MOVEMENTS_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            entry_chemical = str(payload.get("chemical", ""))
            if chemical is not None and entry_chemical != chemical:
                continue
            rows.append(
                InventoryMovement(
                    time=str(payload.get("time", "")),
                    chemical=entry_chemical,
                    kind=str(payload.get("kind", "")),
                    amount=float(payload.get("amount", 0.0)),
                    lot_id=str(payload.get("lot_id", "")),
                )
            )
        return sorted(rows, key=lambda item: parse_time(item.time))

    def consumed(self, chemical: str, start_epoch: int, end_epoch: int) -> tuple[float, list[dict[str, object]]]:
        movements = [
            entry
            for entry in self.entries(chemical)
            if entry.kind == "consume"
            and start_epoch <= parse_time(entry.time) < end_epoch
        ]
        return sum(entry.amount for entry in movements), [entry.as_dict() for entry in movements]
