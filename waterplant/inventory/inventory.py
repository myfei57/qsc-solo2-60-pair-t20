"""Chemical inventory ledger.

Stock arrives in lots and is drawn down first in, first out so a partial
consumption always leaves the older lot in front. Every lot remembers how
much was received and how much has since been consumed, which keeps the
reorder calculation independent of the dosing audit trail.

Every receipt and consumption is additionally appended to an immutable
movement log (``inventory:movements``) so settlement can total stock drawn
during a crew shift without reconstructing it from current lot balances.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from waterplant.store.commands import append_command, load_commands
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .report import InventoryState

LOTS_KEY = "inventory:lots"
MOVEMENTS_KEY = "inventory:movements"
REORDER_KEY = "inventory:reorder-level"
DEFAULT_REORDER_LEVEL = 25.0
EPSILON = 1e-9
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _epoch_of(stamp: str) -> int:
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


@dataclass(frozen=True)
class Movement:
    """One immutable inventory receipt or consumption."""

    id: str
    chemical: str
    kind: str
    amount: float
    at: str
    ref: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "chemical": self.chemical,
            "kind": self.kind,
            "amount": self.amount,
            "at": self.at,
            "ref": self.ref,
        }


@dataclass
class Lot:
    """One received batch of a chemical."""

    chemical: str
    lot_id: str
    received: float
    consumed: float = 0.0

    def remaining(self) -> float:
        return self.received - self.consumed

    def as_dict(self) -> dict[str, object]:
        return {
            "chemical": self.chemical,
            "lot_id": self.lot_id,
            "received": self.received,
            "consumed": self.consumed,
            "remaining": self.remaining(),
        }


class Inventory:
    """Receives and draws down chemical stock."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _load(self) -> list[Lot]:
        raw, present = self._store.get(LOTS_KEY)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        lots: list[Lot] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            lots.append(
                Lot(
                    chemical=str(item.get("chemical", "")),
                    lot_id=str(item.get("lot_id", "")),
                    received=float(item.get("received", 0.0)),
                    consumed=float(item.get("consumed", 0.0)),
                )
            )
        return lots

    def _save(self, lots: list[Lot]) -> None:
        self._store.put(LOTS_KEY, json.dumps([lot.as_dict() for lot in lots], ensure_ascii=False))

    def _append_movement(
        self, kind: str, chemical: str, amount: float, at: str | None, ref: str
    ) -> None:
        movement = Movement(
            id=uuid.uuid4().hex,
            chemical=chemical,
            kind=kind,
            amount=float(amount),
            at=at if at is not None else time.strftime(STAMP_FORMAT, time.gmtime()),
            ref=ref,
        )
        append_command(self._store, MOVEMENTS_KEY, json.dumps(movement.as_dict(), ensure_ascii=False))

    def movements(self, chemical: str | None = None, kind: str | None = None) -> list[Movement]:
        """Return the immutable movement log, oldest first."""

        movements: list[Movement] = []
        for item in load_commands(self._store, MOVEMENTS_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("chemical", ""))
            entry_kind = str(payload.get("kind", ""))
            if chemical is not None and name != chemical:
                continue
            if kind is not None and entry_kind != kind:
                continue
            try:
                amount = float(payload.get("amount", ""))
            except ValueError:
                continue
            movements.append(
                Movement(
                    id=str(payload.get("id", "")),
                    chemical=name,
                    kind=entry_kind,
                    amount=amount,
                    at=str(payload.get("at", "")),
                    ref=str(payload.get("ref", "")),
                )
            )
        return sorted(movements, key=lambda movement: _epoch_of(movement.at))

    def consumed_by_shift(
        self,
        chemical: str,
        shift_windows: list[tuple[str, int, int]],
        reattribute: dict[str, str] | None = None,
    ) -> dict[str, dict[str, object]]:
        """Total consumed stock per supplied shift window (start < at <= end).

        ``reattribute`` maps a movement reference (``makeup:<audit id>``) to a
        different shift key, so the stock drawn by a correction during a later
        crew is collected into the shift it corrected.
        """

        reattribute = reattribute or {}
        rows: dict[str, dict[str, object]] = {}
        for movement in self.movements(chemical, "consume"):
            when = _epoch_of(movement.at)
            owner = reattribute.get(movement.ref)
            if owner is None:
                for shift_key, start, end in shift_windows:
                    if start < when <= end:
                        owner = shift_key
                        break
            if owner is None:
                continue
            row = rows.setdefault(owner, {"stock": 0.0, "movement_ids": []})
            row["stock"] = float(row["stock"]) + movement.amount
            row["movement_ids"].append(movement.id)
        return rows

    def receive(
        self, chemical: str, lot_id: str, amount: float, at: str | None = None
    ) -> Lot:
        """Book a new lot into stock."""

        lots = self._load()
        lot = Lot(chemical=chemical, lot_id=lot_id, received=amount)
        lots.append(lot)
        self._save(lots)
        self._append_movement("receive", chemical, amount, at, lot_id)
        return lot

    def consume(
        self, chemical: str, amount: float, at: str | None = None, ref: str = ""
    ) -> float:
        """Draw ``amount`` of a chemical down, oldest lot first."""

        lots = self._load()
        outstanding = amount
        for lot in lots:
            if lot.chemical != chemical:
                continue
            available = lot.remaining()
            if available <= EPSILON:
                continue
            taken = available if available < outstanding else outstanding
            lot.consumed += taken
            outstanding -= taken
            if outstanding <= EPSILON:
                break
        if outstanding > EPSILON:
            raise ValueError(f"insufficient {chemical} inventory")
        self._save(lots)
        self._append_movement("consume", chemical, amount, at, ref)
        return amount

    def lots(self, chemical: str | None = None) -> list[Lot]:
        lots = self._load()
        if chemical is None:
            return lots
        return [lot for lot in lots if lot.chemical == chemical]

    def chemicals(self) -> list[str]:
        names: list[str] = []
        for lot in self._load():
            if lot.chemical not in names:
                names.append(lot.chemical)
        return names

    def balance(self, chemical: str | None = None) -> float:
        return sum(lot.remaining() for lot in self.lots(chemical))

    def reorder_level(self) -> float:
        value, present = load_float(self._store, REORDER_KEY)
        return value if present else DEFAULT_REORDER_LEVEL

    def set_reorder_level(self, value: float) -> float:
        save_float(self._store, REORDER_KEY, value)
        return value

    def needs_reorder(self) -> list[str]:
        return [name for name in self.chemicals() if self.balance(name) < self.reorder_level()]

    def state(self) -> InventoryState:
        return InventoryState(
            reorder_level=self.reorder_level(),
            balance=self.balance(),
            needs_reorder=self.needs_reorder(),
            lots=[lot.as_dict() for lot in self.lots()],
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"inventory balance={state.balance:.4f} reorder={state.reorder_level:.4f} "
            f"alerts={len(state.needs_reorder)}"
        )
