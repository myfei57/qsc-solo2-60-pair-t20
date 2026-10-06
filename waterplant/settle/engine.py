"""Per chemical, per shift settlement (collection and reconciliation).

What is fixed here rather than left to the operator:

* **Shift boundaries** follow the crew arrangement in
  :mod:`waterplant.settle.schedule`. The default policy is ``handover``: a
  shift starts exactly when its crew takes over, which may be a non whole-hour
  time. Switching the arrangement to ``on_hour`` forces boundary minutes to
  zero. Boundaries therefore come from the roster, never guessed from the
  clock.
* **Mismatch handling** is ``annotate``: settlement always publishes a
  result for every segment and marks the mismatching pair, naming the shift
  segment and which two of dosed/meter/stock disagree. Segments are never
  silently withheld (no suspend), and a failed *process* is different from a
  data mismatch — it is recorded as a partial run and can be recovered.

Guarantees:

* Raw audit entries, inventory movements and meter readings are only read,
  never rewritten. Every result is an appended settlement run document.
* Doses are standardised with the calibration factor and concentration
  version in effect at the dose time; the chosen version ids are recorded.
* A makeup dose settles against ``for_shift`` (time sequence dependency), and
  older runs become ``superseded``; if inputs change without a newer run
  (late audit entry, late calibration version, live factor drift) the latest
  result is reported ``stale``.
* Reruns are idempotent: when the newest run is complete and its input
  fingerprint matches, it is returned unchanged and no document is appended.
* Failures are partial: one failing segment is captured on that segment
  while the rest of the run completes. :meth:`recover` prunes dangling makeup
  markers and reruns the affected window deterministically.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from waterplant.audit import Auditor
from waterplant.flow import Calibration
from waterplant.inventory import Inventory
from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .basis import CalibrationRegistry, ConcentrationRegistry
from .makeup import MakeupLedger, makeup_ref
from .meters import MeterLog
from .report import (
    RECONCILE_TOLERANCE,
    RUN_COMPLETE,
    RUN_FAILED,
    RUN_PARTIAL,
    SEGMENT_ERROR,
    SEGMENT_IDLE,
    SEGMENT_MISMATCH,
    SEGMENT_MISSING_METER,
    SEGMENT_MISSING_STOCK,
    SEGMENT_RECONCILED,
)
from .schedule import Shift, ShiftSchedule
from .timeutil import epoch_of, stamp_now

RUNS_KEY = "settle:runs"


def _fingerprint(payload: object) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


class Settlement:
    """Collects dosing into shifts and reconciles it against meter and stock."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.auditor = Auditor(store)
        self.inventory = Inventory(store)
        self.schedule = ShiftSchedule(store)
        self.calibrations = CalibrationRegistry(store)
        self.concentrations = ConcentrationRegistry(store)
        self.makeups = MakeupLedger(store)
        self.meters = MeterLog(store)

    # ------------------------------------------------------------------ runs

    def _load_runs(self) -> list[dict[str, object]]:
        runs: list[dict[str, object]] = []
        for item in load_commands(self._store, RUNS_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                runs.append(payload)
        return runs

    def _persist_run(self, document: dict[str, object]) -> None:
        append_command(self._store, RUNS_KEY, json.dumps(document, ensure_ascii=False))

    def runs(self) -> list[dict[str, object]]:
        return self._load_runs()

    def run_for(self, shift_key: str, chemical: str | None = None) -> dict[str, object] | None:
        """Newest run document containing the named segment."""

        chosen: dict[str, object] | None = None
        for run in self._load_runs():
            for segment in run.get("segments", []):
                if not isinstance(segment, dict):
                    continue
                if segment.get("shift_key") != shift_key:
                    continue
                if chemical is not None and segment.get("chemical") != chemical:
                    continue
                chosen = run
                break
        return chosen

    # ------------------------------------------------------------- grouping

    def _window_span(
        self, shift_keys: set[str] | None
    ) -> tuple[list[Shift], dict[str, Shift], list[dict[str, str]]]:
        shifts_by_key: dict[str, Shift] = {}
        window_errors: list[dict[str, str]] = []
        if shift_keys is None:
            timestamps: list[str] = []
            for entry in self.auditor.entries():
                try:
                    epoch_of(entry.time)
                except ValueError:
                    window_errors.append(
                        {"entry_id": entry.id, "error": f"unparseable entry time {entry.time!r}"}
                    )
                    continue
                timestamps.append(entry.time)
            for makeup in self.makeups.makeups():
                timestamps.append(makeup.recorded_at)
            if not timestamps:
                return [], {}, window_errors
            start = min(timestamps)
            end = max(timestamps)
            shifts = self.schedule.shifts_between(start, end)
        else:
            shifts = []
            for key in sorted(shift_keys):
                shift = self._shift_from_key(key)
                shifts_by_key[key] = shift
                shifts.append(shift)
            shifts = sorted(shifts, key=lambda shift: shift.start_epoch)
        for shift in shifts:
            shifts_by_key[shift.key] = shift
        return shifts, shifts_by_key, window_errors

    def _shift_from_key(self, key: str) -> Shift:
        try:
            start_stamp, crew = key.split("|", 1)
        except ValueError as exc:
            raise ValueError(f"malformed shift key {key!r}") from exc
        shift = self.schedule.shift_for(start_stamp)
        return Shift(
            key=key,
            crew=crew,
            start_epoch=shift.start_epoch,
            end_epoch=shift.end_epoch,
        )

    def _chemicals_in_play(self, shifts_by_key: dict[str, Shift]) -> list[str]:
        names: list[str] = []

        def note(name: str) -> None:
            if name and name not in names:
                names.append(name)

        for entry in self.auditor.entries():
            note(entry.kind)
        for makeup in self.makeups.makeups():
            note(makeup.chemical)
        for record in self.meters.readings():
            note(record.chemical)
        for movement in self.inventory.movements(kind="consume"):
            note(movement.chemical)
        return names

    # -------------------------------------------------------------- compute

    def compute(
        self,
        shift_keys: set[str] | None = None,
        chemicals: list[str] | None = None,
        now: str | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        """Collect and reconcile one window.

        With ``force=False`` a complete newest run whose fingerprint matches
        is returned as-is (idempotent). Otherwise a fresh run document is
        appended; older runs stay on record and become superseded.
        """

        run_id = uuid.uuid4().hex
        moment = now or stamp_now()
        shifts, shifts_by_key, window_errors = self._window_span(shift_keys)
        chemical_list = list(chemicals) if chemicals is not None else self._chemicals_in_play(shifts_by_key)
        windows = [
            (shift.key, shift.start_epoch, shift.end_epoch) for shift in shifts
        ]

        if not shifts:
            document = self._empty_run(run_id, moment, chemical_list, window_errors)
            self._persist_run(document)
            return self._idempotent_or(document, force)

        makeup_index = self.makeups.index_by_audit()
        # Records (meter readings, stock draws) produced while applying a
        # correction carry ``makeup:<audit id>`` and are collected into the
        # shift the correction was made for, not the physical crew time.
        reattribute: dict[str, dict[str, str]] = {}
        for makeup in makeup_index.values():
            ref = makeup_ref(makeup.audit_id)
            reattribute.setdefault(makeup.chemical, {})[ref] = makeup.for_shift

        meter_rows: dict[str, dict[str, dict[str, object]]] = {}
        stock_rows: dict[str, dict[str, dict[str, object]]] = {}
        for chemical in chemical_list:
            meter_rows[chemical] = self.meters.deltas_by_shift(
                chemical, windows, reattribute.get(chemical, {})
            )
            stock_rows[chemical] = self.inventory.consumed_by_shift(
                chemical, windows, reattribute.get(chemical, {})
            )

        entries = self.auditor.entries()
        # Entries that cannot be placed (bad time/chemical) are recoverable
        # run errors instead of poisoning every segment.
        run_errors: list[dict[str, str]] = list(window_errors)
        assigned: dict[tuple[str, str], list[dict[str, object]]] = {}

        def flag(entry_id: str, message: str) -> None:
            if not any(error.get("entry_id") == entry_id for error in run_errors):
                run_errors.append({"entry_id": entry_id, "error": message})

        for entry in entries:
            makeup = makeup_index.get(entry.id)
            if makeup is not None:
                if makeup.for_shift not in shifts_by_key:
                    continue
                if makeup.chemical != entry.kind:
                    flag(
                        entry.id,
                        f"makeup chemical {makeup.chemical} disagrees with audit chemical {entry.kind}",
                    )
                    continue
                target_key = makeup.for_shift
                is_makeup = True
                when = entry.time
            else:
                try:
                    shift = self.schedule.shift_for(entry.time)
                except ValueError as exc:
                    flag(entry.id, str(exc))
                    continue
                if shift.key not in shifts_by_key:
                    continue
                target_key = shift.key
                is_makeup = False
                when = entry.time
            assigned.setdefault((entry.kind, target_key), []).append(
                {
                    "id": entry.id,
                    "detail": entry.detail,
                    "time": when,
                    "is_makeup": is_makeup,
                }
            )

        live_factor = Calibration(self._store).current()
        segments: list[dict[str, object]] = []
        for chemical in chemical_list:
            for shift in shifts:
                entries_for_segment = assigned.get((chemical, shift.key), [])
                meter_row = meter_rows.get(chemical, {}).get(shift.key)
                stock_row = stock_rows.get(chemical, {}).get(shift.key)
                # A chemical that simply was not used, metered or drawn during
                # a shift has no segment to reconcile.
                if not entries_for_segment and meter_row is None and stock_row is None:
                    continue
                segment, errors = self._build_segment(
                    run_id=run_id,
                    chemical=chemical,
                    shift=shift,
                    entries=entries_for_segment,
                    meter_row=meter_row,
                    stock_row=stock_row,
                    live_factor=live_factor,
                )
                for error in errors:
                    flag(error.get("entry_id", ""), error.get("error", ""))
                segments.append(segment)

        dangling = [makeup.as_dict() for makeup in self.makeups.dangling()]
        for marker in dangling:
            run_errors.append(
                {
                    "error": "dangling makeup marker; run recovery",
                    "makeup": marker,
                }
            )

        statuses = {str(segment["status"]) for segment in segments}
        if not segments or statuses == {SEGMENT_ERROR}:
            run_status = RUN_FAILED
        elif SEGMENT_ERROR in statuses or run_errors:
            run_status = RUN_PARTIAL
        else:
            run_status = RUN_COMPLETE

        inputs = {
            "shifts": [shift.key for shift in shifts],
            "chemicals": chemical_list,
            "live_calibration": round(live_factor, 12),
            "segments": [
                {
                    "chemical": segment["chemical"],
                    "shift_key": segment["shift_key"],
                    "fingerprint": segment["fingerprint"],
                }
                for segment in segments
            ],
            "entry_errors": run_errors,
            "policies": {"boundary": "handover", "mismatch": "annotate"},
        }
        document = {
            "run_id": run_id,
            "computed_at": moment,
            "status": run_status,
            "window_start": shifts[0].start,
            "window_end": shifts[-1].end,
            "chemicals": chemical_list,
            "policies": {"boundary": "handover", "mismatch": "annotate"},
            "fingerprint": _fingerprint(inputs),
            "entry_count": len(entries),
            "segment_count": len(segments),
            "errors": run_errors,
            "segments": segments,
        }
        self._persist_run(document)
        return self._idempotent_or(document, force)

    def _idempotent_or(self, document: dict[str, object], force: bool) -> dict[str, object]:
        if force:
            return document
        previous = self._previous_complete(document)
        if previous is not None:
            result = dict(previous)
            result["idempotent"] = True
            return result
        document["idempotent"] = False
        return document

    def _previous_complete(self, document: dict[str, object]) -> dict[str, object] | None:
        """The immediately preceding run, if complete with the same fingerprint."""

        runs = self._load_runs()
        if len(runs) < 2:
            return None
        previous = runs[-2]
        if previous.get("fingerprint") != document.get("fingerprint"):
            return None
        if previous.get("status") != RUN_COMPLETE:
            return None
        return previous

    def _empty_run(
        self,
        run_id: str,
        moment: str,
        chemicals: list[str],
        window_errors: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        errors = list(window_errors or [])
        inputs = {"shifts": [], "chemicals": chemicals, "entry_errors": errors}
        status = RUN_PARTIAL if errors else RUN_COMPLETE
        return {
            "run_id": run_id,
            "computed_at": moment,
            "status": status,
            "window_start": None,
            "window_end": None,
            "chemicals": chemicals,
            "policies": {"boundary": "handover", "mismatch": "annotate"},
            "fingerprint": _fingerprint(inputs),
            "entry_count": self.auditor.count(),
            "segment_count": 0,
            "errors": [],
            "segments": [],
            "idempotent": False,
        }

    def _build_segment(
        self,
        run_id: str,
        chemical: str,
        shift: Shift,
        entries: list[dict[str, object]],
        meter_row: dict[str, object] | None,
        stock_row: dict[str, object] | None,
        live_factor: float,
    ) -> tuple[dict[str, object], list[dict[str, str]]]:
        errors: list[dict[str, str]] = []
        dosed_raw = 0.0
        dosed_standard = 0.0
        basis_trace: list[dict[str, object]] = []
        makeup_count = 0
        for entry in entries:
            try:
                raw = float(entry["detail"])
            except (KeyError, TypeError, ValueError):
                errors.append(
                    {"entry_id": str(entry.get("id", "")), "error": "dose detail is not numeric"}
                )
                continue
            when = str(entry["time"])
            calibration, calib_id = self.calibrations.effective_for(chemical, when, live_factor)
            concentration, conc_id = self.concentrations.effective_for(chemical, when)
            standard = raw * calibration * concentration
            dosed_raw += raw
            dosed_standard += standard
            if entry.get("is_makeup"):
                makeup_count += 1
            basis_trace.append(
                {
                    "entry_id": entry["id"],
                    "raw": raw,
                    "standard": standard,
                    "calibration": calibration,
                    "calibration_id": calib_id,
                    "concentration": concentration,
                    "concentration_id": conc_id,
                    "makeup": bool(entry.get("is_makeup")),
                }
            )

        meter: float | None = None
        reading_ids: list[str] = []
        meter_faults: list[str] = []
        if meter_row is not None:
            meter = float(meter_row.get("meter", 0.0))
            reading_ids = [str(item) for item in meter_row.get("reading_ids", [])]
            meter_faults = [str(item) for item in meter_row.get("faults", [])]

        stock: float | None = None
        movement_ids: list[str] = []
        if stock_row is not None:
            stock = float(stock_row.get("stock", 0.0))
            movement_ids = [str(item) for item in stock_row.get("movement_ids", [])]

        if errors:
            status = SEGMENT_ERROR
            discrepancies: list[dict[str, object]] = []
        else:
            status, discrepancies = self._reconcile(
                chemical, shift, dosed_standard, meter, stock, meter_faults
            )

        segment_inputs = {
            "chemical": chemical,
            "shift_key": shift.key,
            "entries": [
                {
                    "id": item["id"],
                    "detail": item["detail"],
                    "time": item["time"],
                    "makeup": item["is_makeup"],
                }
                for item in entries
            ],
            "meter": None if meter is None else round(meter, 12),
            "meter_reading_ids": reading_ids,
            "stock": None if stock is None else round(stock, 12),
            "stock_movement_ids": movement_ids,
            "live_calibration": round(live_factor, 12),
            # The versions in effect at dose time are part of the inputs, so a
            # retroactively registered version marks older runs stale.
            "basis_versions": [
                {
                    "entry_id": trace["entry_id"],
                    "calibration_id": trace["calibration_id"],
                    "concentration_id": trace["concentration_id"],
                    "calibration": round(float(trace["calibration"]), 12),
                    "concentration": round(float(trace["concentration"]), 12),
                }
                for trace in basis_trace
            ],
        }
        segment = {
            "run_id": run_id,
            "chemical": chemical,
            "shift_key": shift.key,
            "crew": shift.crew,
            "start": shift.start,
            "end": shift.end,
            "status": status,
            "fingerprint": _fingerprint(segment_inputs),
            "input_fingerprint": _fingerprint(segment_inputs),
            "dosed_raw": round(dosed_raw, 12),
            "dosed_standard": round(dosed_standard, 12),
            "meter": None if meter is None else round(meter, 12),
            "stock": None if stock is None else round(stock, 12),
            "entry_count": len(entries),
            "makeup_count": makeup_count,
            "reading_ids": reading_ids,
            "movement_ids": movement_ids,
            "discrepancies": discrepancies,
            "basis": basis_trace,
            "errors": errors,
        }
        return segment, errors

    @staticmethod
    def _pair(label_a: str, value_a: float, label_b: str, value_b: float) -> dict[str, object] | None:
        if abs(value_a - value_b) > RECONCILE_TOLERANCE:
            return {
                "pair": [label_a, label_b],
                label_a: round(value_a, 12),
                label_b: round(value_b, 12),
                "delta": round(value_a - value_b, 12),
            }
        return None

    def _reconcile(
        self,
        chemical: str,
        shift: Shift,
        dosed: float,
        meter: float | None,
        stock: float | None,
        meter_faults: list[str],
    ) -> tuple[str, list[dict[str, object]]]:
        """Compare dosed vs meter vs stock; missing sides are not mismatches."""

        discrepancies: list[dict[str, object]] = []
        segment = f"{chemical} @ {shift.key} ({shift.start}..{shift.end})"
        present = {"dosed": dosed}
        if meter is not None:
            present["meter"] = meter
        if stock is not None:
            present["stock"] = stock

        labels = list(present)
        for index, left in enumerate(labels):
            for right in labels[index + 1:]:
                mismatch = self._pair(left, present[left], right, present[right])
                if mismatch is not None:
                    mismatch["segment"] = segment
                    discrepancies.append(mismatch)
        for fault in meter_faults:
            discrepancies.append({"segment": segment, "meter_fault": fault})

        if discrepancies:
            return SEGMENT_MISMATCH, discrepancies
        if meter is None and stock is None:
            # Nothing to reconcile the dosed total against; this is not a
            # numeric mismatch but it also cannot be called reconciled.
            return SEGMENT_MISMATCH, [
                {"segment": segment, "note": "no meter reading or stock consumption to reconcile against"}
            ]
        if meter is None:
            return SEGMENT_MISSING_METER, []
        if stock is None:
            return SEGMENT_MISSING_STOCK, []
        return SEGMENT_RECONCILED, []

    # --------------------------------------------------------------- recovery

    def recover(self, shift_keys: set[str] | None = None, now: str | None = None) -> dict[str, object]:
        """Repair partial writes, then deterministically recompute the window.

        Dangling makeup markers are pruned (their raw audit entries remain).
        The rerun is a normal :meth:`compute` call with ``force`` so a fresh
        run document is always appended; repeated recovery of the same inputs
        then collapses back to idempotency.
        """

        pruned = self.makeups.recover()
        run = self.compute(shift_keys=shift_keys, now=now, force=True)
        return {"pruned_makeups": pruned, "run": run}

    # ----------------------------------------------------------------- views

    def latest_segments(self) -> list[dict[str, object]]:
        """Newest segment per chemical/shift with stale/superseded marking.

        A newest run that does not match the inputs now visible is ``stale``.
        Segments buried by a newer run are reported ``superseded`` here via
        :meth:`segment_history`.
        """

        runs = self._load_runs()
        latest_index: dict[tuple[str, str], int] = {}
        for index, run in enumerate(runs):
            for segment in run.get("segments", []):
                if not isinstance(segment, dict):
                    continue
                key = (str(segment.get("chemical")), str(segment.get("shift_key")))
                latest_index[key] = index

        views: list[dict[str, object]] = []
        for (chemical, shift_key), run_index in sorted(
            latest_index.items(), key=lambda item: (item[0][1], item[0][0])
        ):
            run = runs[run_index]
            segment = next(
                segment
                for segment in run["segments"]
                if segment.get("chemical") == chemical and segment.get("shift_key") == shift_key
            )
            live = self._current_segment_fingerprint(chemical, shift_key)
            stale = live is not None and live != segment.get("fingerprint")
            view = dict(segment)
            view["run_id"] = run.get("run_id")
            view["computed_at"] = run.get("computed_at")
            view["run_status"] = run.get("status")
            view["state"] = "stale" if stale else "current"
            view["stale"] = stale
            views.append(view)
        return views

    def _current_segment_fingerprint(self, chemical: str, shift_key: str) -> str | None:
        """Compute what the segment fingerprint would be from live inputs."""

        try:
            shift = self._shift_from_key(shift_key)
        except ValueError:
            return None
        live_factor = Calibration(self._store).current()
        makeup_index = self.makeups.index_by_audit()
        entries: list[dict[str, object]] = []
        for entry in self.auditor.entries():
            makeup = makeup_index.get(entry.id)
            if makeup is not None:
                if makeup.for_shift != shift_key or makeup.chemical != chemical:
                    continue
                is_makeup = True
            else:
                try:
                    if self.schedule.shift_for(entry.time).key != shift_key:
                        continue
                except ValueError:
                    continue
                is_makeup = False
            if entry.kind != chemical:
                continue
            entries.append(
                {
                    "id": entry.id,
                    "detail": entry.detail,
                    "time": entry.time,
                    "is_makeup": is_makeup,
                }
            )
        windows = [(shift.key, shift.start_epoch, shift.end_epoch)]
        reattribute = {
            makeup_ref(makeup.audit_id): makeup.for_shift
            for makeup in self.makeups.makeups(chemical=chemical)
        }
        meter_row = self.meters.deltas_by_shift(chemical, windows, reattribute).get(shift_key)
        stock_row = self.inventory.consumed_by_shift(chemical, windows, reattribute).get(shift_key)
        if not entries and meter_row is None and stock_row is None:
            return None
        segment, _ = self._build_segment(
            run_id="",
            chemical=chemical,
            shift=shift,
            entries=entries,
            meter_row=meter_row,
            stock_row=stock_row,
            live_factor=live_factor,
        )
        return str(segment["fingerprint"])

    def segment_history(self, chemical: str, shift_key: str) -> list[dict[str, object]]:
        """Every run that touched a segment, oldest first, with its state."""

        history: list[dict[str, object]] = []
        runs = self._load_runs()
        newest_index = -1
        for index, run in enumerate(runs):
            for segment in run.get("segments", []):
                if (
                    isinstance(segment, dict)
                    and segment.get("chemical") == chemical
                    and segment.get("shift_key") == shift_key
                ):
                    newest_index = index
        for index, run in enumerate(runs):
            for segment in run.get("segments", []):
                if (
                    not isinstance(segment, dict)
                    or segment.get("chemical") != chemical
                    or segment.get("shift_key") != shift_key
                ):
                    continue
                if index == newest_index:
                    live = self._current_segment_fingerprint(chemical, shift_key)
                    state = "stale" if live is not None and live != segment.get("fingerprint") else "current"
                else:
                    state = "superseded"
                view = dict(segment)
                view["run_id"] = run.get("run_id")
                view["computed_at"] = run.get("computed_at")
                view["run_status"] = run.get("status")
                view["state"] = state
                history.append(view)
        return history

    def state(self) -> dict[str, object]:
        views = self.latest_segments()
        mismatched = [view for view in views if view.get("status") == SEGMENT_MISMATCH]
        stale = [view for view in views if view.get("state") == "stale"]
        errored = [view for view in views if view.get("status") == SEGMENT_ERROR]
        return {
            "policies": {"boundary": "handover", "mismatch": "annotate"},
            "runs": len(self._load_runs()),
            "segments": views,
            "mismatch_count": len(mismatched),
            "stale_count": len(stale),
            "error_count": len(errored),
            "schedule": self.schedule.state(),
        }

    def describe(self) -> str:
        state = self.state()
        return (
            f"settle runs={state['runs']} segments={len(state['segments'])} "
            f"mismatch={state['mismatch_count']} stale={state['stale_count']} "
            f"errors={state['error_count']}"
        )
