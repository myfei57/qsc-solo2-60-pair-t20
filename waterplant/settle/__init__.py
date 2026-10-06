"""Chemical dosing settlement: collection, reconciliation and recovery.

Settlement groups raw dosing audit entries into crew shifts per chemical,
standardises them with the calibration and concentration versions in effect
at dose time, adds makeup corrections to the shift they were made for, and
reconciles the collected amount against cumulative meter deltas and inventory
stock drawn. Results are append only; raw records are never rewritten.
"""

from .basis import (
    CALIBRATION_KEY,
    CONCENTRATION_KEY,
    BasisVersion,
    CalibrationRegistry,
    ConcentrationRegistry,
)
from .engine import RUNS_KEY, Settlement
from .makeup import MAKEUP_KEY, Makeup, MakeupLedger
from .meters import MeterLog, MeterReading
from .report import (
    MISMATCH_ANNOTATE,
    RECONCILE_TOLERANCE,
    RUN_COMPLETE,
    RUN_FAILED,
    RUN_PARTIAL,
    SEGMENT_ERROR,
    SEGMENT_MISMATCH,
    SEGMENT_MISSING_METER,
    SEGMENT_MISSING_STOCK,
    SEGMENT_IDLE,
    SEGMENT_RECONCILED,
    SEGMENT_ERROR,
    SegmentView,
)
from .schedule import (
    HANDOVER,
    ON_HOUR,
    BOUNDARY_POLICIES,
    SCHEDULE_KEY,
    Shift,
    ShiftSchedule,
)

__all__ = [
    "BOUNDARY_POLICIES",
    "CALIBRATION_KEY",
    "CONCENTRATION_KEY",
    "HANDOVER",
    "MAKEUP_KEY",
    "MISMATCH_ANNOTATE",
    "ON_HOUR",
    "RECONCILE_TOLERANCE",
    "RUNS_KEY",
    "RUN_COMPLETE",
    "RUN_FAILED",
    "RUN_PARTIAL",
    "BasisVersion",
    "CalibrationRegistry",
    "ConcentrationRegistry",
    "Makeup",
    "MakeupLedger",
    "MeterLog",
    "MeterReading",
    "SEGMENT_MISMATCH",
    "SEGMENT_MISSING_METER",
    "SEGMENT_MISSING_STOCK",
    "SEGMENT_IDLE",
    "SEGMENT_RECONCILED",
    "SEGMENT_ERROR",
    "SegmentView",
    "Settlement",
    "Shift",
    "ShiftSchedule",
]
