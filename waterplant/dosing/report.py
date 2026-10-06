"""Projections shared by dosing aggregation and reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Shift:
    """A chemical-independent operating shift located by handover times."""

    key: str
    team: str
    start: str
    end: str
    start_epoch: int
    end_epoch: int

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "team": self.team,
            "start": self.start,
            "end": self.end,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
        }


@dataclass(frozen=True)
class ShiftScheduleState:
    """Persisted crew handover policy."""

    handover_minutes: list[int]
    teams: list[str]
    boundary_policy: str

    def as_dict(self) -> dict[str, object]:
        return {
            "handover_minutes": self.handover_minutes,
            "teams": self.teams,
            "boundary_policy": self.boundary_policy,
        }


@dataclass(frozen=True)
class BasisEntry:
    """Calibration and concentration effective at a point in time."""

    chemical: str
    effective_from: str
    calibration: float
    concentration: float

    def as_dict(self) -> dict[str, object]:
        return {
            "chemical": self.chemical,
            "effective_from": self.effective_from,
            "calibration": self.calibration,
            "concentration": self.concentration,
        }


@dataclass(frozen=True)
class CumulativeEntry:
    """One append-only cumulative meter observation."""

    chemical: str
    time: str
    value: float
    rollover: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "chemical": self.chemical,
            "time": self.time,
            "value": self.value,
            "rollover": self.rollover,
        }


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


@dataclass(frozen=True)
class AggregateResult:
    """Reconciled or marked chemical/shift aggregate."""

    id: str
    group_key: str
    chemical: str
    shift: Shift
    status: str
    dose_total: float
    cumulative_total: float | None
    inventory_total: float | None
    discrepancies: list[dict[str, object]] = field(default_factory=list)
    entries: list[dict[str, object]] = field(default_factory=list)
    basis: list[dict[str, object]] = field(default_factory=list)
    inventory_lots: list[dict[str, object]] = field(default_factory=list)
    cumulative_segment: dict[str, object] | None = None
    source_fingerprint: str = ""
    calculated_at: str = ""
    stale_reason: str = ""
    run_id: str = ""
    active: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "group_key": self.group_key,
            "chemical": self.chemical,
            "shift": self.shift.as_dict(),
            "status": self.status,
            "dose_total": self.dose_total,
            "cumulative_total": self.cumulative_total,
            "inventory_total": self.inventory_total,
            "discrepancies": self.discrepancies,
            "entries": self.entries,
            "basis": self.basis,
            "inventory_lots": self.inventory_lots,
            "cumulative_segment": self.cumulative_segment,
            "source_fingerprint": self.source_fingerprint,
            "calculated_at": self.calculated_at,
            "stale_reason": self.stale_reason,
            "run_id": self.run_id,
            "active": self.active,
        }


@dataclass(frozen=True)
class AggregationRun:
    """Summary of one partially-failure-tolerant aggregation run."""

    run_id: str
    started_at: str
    finished_at: str
    status: str
    group_count: int
    calculated: int
    unchanged: int
    failed: int
    failures: list[dict[str, object]] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "group_count": self.group_count,
            "calculated": self.calculated,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "failures": self.failures,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class AggregationState:
    """Console projection for derived aggregates and recovery metadata."""

    results: list[AggregateResult]
    checkpoints: dict[str, dict[str, object]]
    last_run: AggregationRun | None
    discrepancy_policy: str
    boundary_policy: str

    def as_dict(self) -> dict[str, object]:
        return {
            "results": [result.as_dict() for result in self.results],
            "checkpoints": self.checkpoints,
            "last_run": None if self.last_run is None else self.last_run.as_dict(),
            "discrepancy_policy": self.discrepancy_policy,
            "boundary_policy": self.boundary_policy,
        }
