"""Inventory movement records shared without importing the dosing package."""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.store.commands import load_commands
from waterplant.store.store import Store

from .dosing_timeutil import now_string

MOVEMENTS_KEY = "inventory:movements"


@dataclass(frozen=True)
class InventoryMovement:
    """One append-only receipt or inventory draw."""

    time: str
    chemical: str
    kind: str
    amount: float
    lot_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "time": self.time,
            "chemical": self.chemical,
            "kind": self.kind,
            "amount": self.amount,
            "lot_id": self.lot_id,
        }


def append_movement(
    store: Store,
    chemical: str,
    kind: str,
    amount: float,
    lot_id: str = "",
    at: str | None = None,
) -> tuple[InventoryMovement, list[str]]:
    """Create one inventory movement and return the updated raw command list."""

    if kind not in {"receive", "consume"}:
        raise ValueError("movement kind must be receive or consume")
    if amount < 0:
        raise ValueError("movement amount must be non-negative")
    movement = InventoryMovement(
        time=at or now_string(),
        chemical=chemical,
        kind=kind,
        amount=amount,
        lot_id=lot_id,
    )
    items = load_commands(store, MOVEMENTS_KEY)
    items.append(json.dumps(movement.as_dict(), ensure_ascii=False))
    return movement, items
