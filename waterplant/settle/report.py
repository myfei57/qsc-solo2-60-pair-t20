"""Settlement projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass, field

# Absolute mass/volume tolerance for three-way reconciliation.
RECONCILE_TOLERANCE = 1e-6

# Fixed operational policies, see ``settlement.engine``.
MISMATCH_ANNOTATE = "annotate"
BOUNDARY_HANDOVER = "handover"

RUN_COMPLETE = "complete"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"

SEGMENT_RECONCILED = "reconciled"
SEGMENT_MISMATCH = "mismatch"
SEGMENT_MISSING_METER = "missing_meter"
SEGMENT_MISSING_STOCK = "missing_stock"
SEGMENT_IDLE = "idle"
SEGMENT_ERROR = "error"


@dataclass(frozen=True)
class SegmentView:
    """One chemical/shift segment as the console shows it."""

    chemical: str
    shift_key: str
    crew: str
    start: str
    end: str
    state: str
    stale: bool
    run_id: str
    fingerprint: str
    dosed_raw: float
    dosed_standard: float
    meter: float | None
    stock: float | None
    status: str
    discrepancies: list[dict[str, object]] = field(default_factory=list)
    entry_count: int = 0
    makeup_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "chemical": self.chemical,
            "shift_key": self.shift_key,
            "crew": self.crew,
            "start": self.start,
            "end": self.end,
            "state": self.state,
            "stale": self.stale,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "dosed_raw": self.dosed_raw,
            "dosed_standard": self.dosed_standard,
            "meter": self.meter,
            "stock": self.stock,
            "status": self.status,
            "discrepancies": self.discrepancies,
            "entry_count": self.entry_count,
            "makeup_count": self.makeup_count,
        }
