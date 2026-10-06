"""Chlorine consumption accumulator with a periodic wrap."""

from __future__ import annotations

import json

from waterplant.dosing.cumulative import CUMULATIVE_KEY
from waterplant.dosing.timeutil import now_string
from waterplant.store.commands import load_commands
from waterplant.store.epoch import load_float
from waterplant.store.store import Store

from .report import AccumState

ACCUM_KEY = "quota:chlorine-accumulator"
EPOCH_CAP = 100.0


class Accumulator:
    """Runs a wrapping total so the meter never overflows."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def add(self, amount: float, at: str | None = None) -> float:
        current, _ = load_float(self._store, ACCUM_KEY)
        next_value = current + amount
        if EPOCH_CAP > 0 and next_value >= EPOCH_CAP:
            next_value -= EPOCH_CAP
        self._save_with_observation(next_value, at)
        return next_value

    def value(self) -> float:
        value, _ = load_float(self._store, ACCUM_KEY)
        return value

    def remaining(self, limit: float) -> float:
        remaining = limit - self.value()
        return remaining if remaining > 0 else 0.0

    def reset(self, at: str | None = None) -> None:
        self._save_with_observation(0.0, at)

    def state(self) -> AccumState:
        return AccumState(value=self.value())

    def describe(self) -> str:
        return f"quota accumulator value={self.value():.4f}"

    def _save_with_observation(self, value: float, at: str | None) -> None:
        items = load_commands(self._store, CUMULATIVE_KEY)
        items.append(
            json.dumps(
                {
                    "chemical": "chlorine",
                    "time": at or now_string(),
                    "value": value,
                    "rollover": EPOCH_CAP,
                },
                ensure_ascii=False,
            )
        )
        self._store.put_many(
            {
                ACCUM_KEY: f"{value:.4f}",
                CUMULATIVE_KEY: json.dumps(items, ensure_ascii=False),
            }
        )
