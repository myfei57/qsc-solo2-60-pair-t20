"""Coagulant dosing controller."""

from __future__ import annotations

from waterplant.audit import Auditor
from waterplant.dosing.basis import BasisLedger
from waterplant.flow import Calibration
from waterplant.intake import FlowRepository
from waterplant.store.store import Store

from .report import DoseState

COAGULANT = "coagulant"


class Doser:
    """Turns a flow reading into a coagulant dose at the current ratio."""

    def __init__(self, store: Store) -> None:
        self._flow = FlowRepository(store)
        self._calibration = Calibration(store)
        self._basis = BasisLedger(store)
        self._auditor = Auditor(store)

    @property
    def flow(self) -> FlowRepository:
        return self._flow

    def current_ratio(self) -> float:
        """Ratio the dose is computed against, refreshed from the meter."""

        return self._calibration.current()

    def dose_for_flow(self, value: float) -> float:
        basis = self._basis.latest(COAGULANT)
        return value * self.current_ratio() * basis.concentration

    def dose_for_turbidity(self, turbidity: float) -> float:
        basis = self._basis.latest(COAGULANT)
        return turbidity * self.current_ratio() * basis.concentration

    def dose_from_persisted_flow(self) -> float:
        value, present = self._flow.load_flow()
        if not present:
            return 0.0
        return self.dose_for_flow(value)

    def update_flow_and_dose(self, value: float, at: str | None = None) -> float:
        """Persist the new flow, then dose against the value just written."""

        self._flow.persist_flow(value)
        dose = self.dose_from_persisted_flow()
        basis = self._basis.latest(COAGULANT)
        self._auditor.record(
            COAGULANT,
            f"{dose:.4f}",
            at=at,
            raw_amount=value,
            calibration=self.current_ratio(),
            concentration=basis.concentration,
        )
        return dose

    def dose_plan(self, flow: float, turbidity: float) -> float:
        """Blend flow and turbidity into a planning dose."""

        return (flow * 0.6 + turbidity * 0.4) * self.current_ratio()

    def state(self) -> DoseState:
        flow, present = self._flow.load_flow()
        return DoseState(ratio=self.current_ratio(), persisted_flow=flow, flow_present=present)

    def describe(self) -> str:
        state = self.state()
        return (
            f"coagulant ratio={state.ratio:.4f} persisted_flow={state.persisted_flow:.4f} "
            f"present={state.flow_present}"
        )
