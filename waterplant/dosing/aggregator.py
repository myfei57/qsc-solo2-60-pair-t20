"""Aggregate immutable dosing records and reconcile them per chemical/shift."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .basis import BasisLedger
from .cumulative import CumulativeLedger
from .movements import MovementLedger
from .report import (
    AggregateResult,
    AggregationRun,
    AggregationState,
    BasisEntry,
)
from .schedule import BOUNDARY_POLICY, ShiftSchedule
from .timeutil import now_string, parse_time

AGG_STATE_KEY = "dosing:aggregation-state"
DISCREPANCY_POLICY = "publish-and-mark"
TOLERANCE = 1e-6


@dataclass
class _LoadedGroups:
    groups: dict[str, list[dict[str, object]]]
    malformed: list[dict[str, object]]


class Aggregator:
    """Builds derived, versioned aggregates without editing source records."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._auditor = Auditor(store)
        self._schedule = ShiftSchedule(store)
        self._basis = BasisLedger(store)
        self._cumulative = CumulativeLedger(store)
        self._movements = MovementLedger(store)

    def run(self, chemical: str | None = None) -> AggregationRun:
        """Recompute affected groups; other groups stay available on partial failure."""

        run_id = uuid.uuid4().hex
        started_at = now_string()
        state = self._load_state()
        raw_groups = self._load_groups()
        calculated = 0
        unchanged = 0
        failed = len(raw_groups.malformed)
        failures = list(raw_groups.malformed)
        stale_keys: list[str] = []
        checkpoints = dict(state.get("checkpoints", {}))
        results = list(state.get("results", []))
        for failure in raw_groups.malformed:
            checkpoints[
                f"malformed:{failure.get('sequence', '?')}:{failure.get('audit_id', '')}"
            ] = {
                "status": "failed",
                "source_fingerprint": "",
                "error": failure.get("error", "malformed source record"),
                "failed_at": now_string(),
            }

        group_keys = sorted(
            key for key in raw_groups.groups if chemical is None or key.startswith(chemical + "|")
        )

        for group_key in group_keys:
            entries = raw_groups.groups[group_key]
            checkpoint = checkpoints.get(group_key, {})
            try:
                source_fingerprint = self._source_fingerprint(group_key, entries)
            except Exception as exc:  # noqa: BLE001 - retain the failure for retry
                failed += 1
                failures.append({"group_key": group_key, "error": str(exc)})
                checkpoints[group_key] = {
                    "status": "failed",
                    "error": str(exc),
                    "failed_at": now_string(),
                }
                stale_keys.append(group_key)
                continue
            if checkpoint.get("status") != "failed":
                if checkpoint.get("source_fingerprint") == source_fingerprint:
                    active = any(
                        item.get("group_key") == group_key and item.get("active")
                        for item in results
                    )
                    if active:
                        unchanged += 1
                        continue
            try:
                result = self._calculate(group_key, entries, source_fingerprint, run_id)
                results = _supersede(results, group_key, "superseded-by-recompute", run_id)
                results.append(result.as_dict())
                checkpoints[group_key] = {
                    "status": result.status,
                    "source_fingerprint": source_fingerprint,
                    "result_id": result.id,
                    "calculated_at": result.calculated_at,
                }
                calculated += 1
            except Exception as exc:  # noqa: BLE001 - per-group failure must be recoverable
                failed += 1
                failures.append({"group_key": group_key, "error": str(exc)})
                checkpoints[group_key] = {
                    "status": "failed",
                    "source_fingerprint": source_fingerprint,
                    "error": str(exc),
                    "failed_at": now_string(),
                }
                stale_keys.append(group_key)

        missing_keys: list[str] = []
        for old in results:
            if not old.get("active", True):
                continue
            group_key = str(old.get("group_key", ""))
            if chemical is not None and not group_key.startswith(chemical + "|"):
                continue
            entries = raw_groups.groups.get(group_key)
            if entries is None:
                missing_keys.append(group_key)
                stale_keys.append(group_key)
                continue
            fingerprint = None
            try:
                fingerprint = self._source_fingerprint(group_key, entries)
            except Exception as exc:  # noqa: BLE001 - projection must remain available
                stale_keys.append(group_key)
                checkpoints.setdefault(group_key, {})["warning"] = str(exc)
            if fingerprint is not None and old.get("source_fingerprint") != fingerprint:
                stale_keys.append(group_key)

        for group_key in missing_keys:
            results = _supersede(results, group_key, "source-group-missing", run_id)

        status = "partial_failure" if failed else "ok"
        run = AggregationRun(
            run_id=run_id,
            started_at=started_at,
            finished_at=now_string(),
            status=status,
            group_count=len(group_keys) + len(raw_groups.malformed),
            calculated=calculated,
            unchanged=unchanged,
            failed=failed,
            failures=failures,
            stale=sorted(set(stale_keys)),
        )
        self._save_state(
            {
                "version": 1,
                "results": results,
                "checkpoints": checkpoints,
                "last_run": run.as_dict(),
                "discrepancy_policy": DISCREPANCY_POLICY,
                "boundary_policy": BOUNDARY_POLICY,
            }
        )
        return run

    def state(self) -> AggregationState:
        raw = self._load_state()
        groups = self._load_groups().groups
        projected: list[AggregateResult] = []
        for item in raw.get("results", []):
            result = self._result_from_dict(item)
            if result.active:
                entries = groups.get(result.group_key)
                if entries is None:
                    result = _with_stale(result, "source-group-missing")
                else:
                    try:
                        changed = self._source_fingerprint(
                            result.group_key, entries
                        ) != result.source_fingerprint
                    except Exception:  # noqa: BLE001 - keep showing the last known aggregate
                        changed = True
                    if changed:
                        result = _with_stale(result, "source-changed-after-aggregation")
            projected.append(result)
        last_run = raw.get("last_run")
        return AggregationState(
            results=projected,
            checkpoints=dict(raw.get("checkpoints", {})),
            last_run=None if not isinstance(last_run, dict) else self._run_from_dict(last_run),
            discrepancy_policy=DISCREPANCY_POLICY,
            boundary_policy=BOUNDARY_POLICY,
        )

    def active_results(self, chemical: str | None = None) -> list[AggregateResult]:
        results = [result for result in self.state().results if result.active]
        if chemical is None:
            return results
        return [result for result in results if result.chemical == chemical]

    def describe(self) -> str:
        state = self.state()
        active = [result for result in state.results if result.active]
        discrepancies = sum(1 for result in active if result.status == "discrepant")
        stale = sum(1 for result in active if result.stale_reason)
        return (
            f"dosing aggregates={len(active)} discrepancies={discrepancies} "
            f"stale={stale} policy={DISCREPANCY_POLICY} boundary={BOUNDARY_POLICY}"
        )

    def _calculate(
        self,
        group_key: str,
        entries: list[dict[str, object]],
        source_fingerprint: str,
        run_id: str,
    ) -> AggregateResult:
        chemical = str(entries[0]["kind"])
        shift_key = str(entries[0]["shift_key"])
        shift = self._schedule.locate(str(entries[0]["time"]))
        if shift.key != shift_key:
            raise ValueError("shift schedule no longer resolves source group")
        references: list[dict[str, object]] = []
        basis_snapshots: list[dict[str, object]] = []
        dose_total = 0.0
        for index, entry in enumerate(entries):
            amount = float(entry["detail"])
            raw_amount = entry.get("raw_amount")
            calibration = entry.get("calibration")
            concentration = entry.get("concentration")
            if raw_amount is None or calibration is None or concentration is None:
                basis = self._basis.effective(chemical, str(entry["time"]))
                calibration = basis.calibration
                concentration = basis.concentration
                standard_amount = amount * basis.concentration
            else:
                standard_amount = float(raw_amount) * float(calibration) * float(concentration)
            dose_total += standard_amount
            references.append(
                {
                    "sequence": index,
                    "audit_id": entry.get("id", ""),
                    "time": entry["time"],
                    "dose_type": entry.get("dose_type", "dose"),
                    "recorded_amount": amount,
                    "standard_amount": standard_amount,
                }
            )
            basis_snapshots.append(
                BasisEntry(
                    chemical=chemical,
                    effective_from=str(entry["time"]),
                    calibration=float(calibration),
                    concentration=float(concentration),
                ).as_dict()
            )

        discrepancies: list[dict[str, object]] = []
        cumulative_total, cumulative_segment = self._cumulative.delta(
            chemical, shift.start_epoch, shift.end_epoch
        )
        if cumulative_total is None:
            discrepancies.append(
                {
                    "source": "cumulative",
                    "segment": shift.as_dict(),
                    "message": "no cumulative readings bracket this shift",
                }
            )
        elif abs(cumulative_total - dose_total) > TOLERANCE:
            discrepancies.append(
                {
                    "source": "cumulative",
                    "segment": shift.as_dict(),
                    "expected_dose": dose_total,
                    "actual": cumulative_total,
                    "difference": cumulative_total - dose_total,
                }
            )

        inventory_total, inventory_movements = self._movements.consumed(
            chemical, shift.start_epoch, shift.end_epoch
        )
        if not inventory_movements:
            discrepancies.append(
                {
                    "source": "inventory",
                    "segment": shift.as_dict(),
                    "message": "no lot-level consumption movement recorded for this shift",
                }
            )
            inventory_total = None
        elif abs(inventory_total - dose_total) > TOLERANCE:
            discrepancies.append(
                {
                    "source": "inventory",
                    "segment": shift.as_dict(),
                    "expected_dose": dose_total,
                    "actual": inventory_total,
                    "difference": inventory_total - dose_total,
                }
            )

        status = "discrepant" if discrepancies else "ready"
        timestamp = now_string()
        return AggregateResult(
            id=hashlib.sha256(f"{group_key}|{source_fingerprint}|{timestamp}".encode()).hexdigest(),
            group_key=group_key,
            chemical=chemical,
            shift=shift,
            status=status,
            dose_total=dose_total,
            cumulative_total=cumulative_total,
            inventory_total=inventory_total,
            discrepancies=discrepancies,
            entries=references,
            basis=basis_snapshots,
            inventory_lots=inventory_movements,
            cumulative_segment=cumulative_segment,
            source_fingerprint=source_fingerprint,
            calculated_at=timestamp,
            run_id=run_id,
        )

    def _load_groups(self) -> _LoadedGroups:
        groups: dict[str, list[dict[str, object]]] = {}
        malformed: list[dict[str, object]] = []
        for sequence, entry in enumerate(self._auditor.entries()):
            if entry.dose_type not in {"dose", "supplement"}:
                continue
            try:
                amount = float(entry.detail)
                parse_time(entry.time)
                shift = self._schedule.locate(entry.time)
            except (TypeError, ValueError) as exc:
                malformed.append(
                    {
                        "group_key": "__malformed__",
                        "sequence": sequence,
                        "audit_id": entry.id,
                        "error": str(exc),
                    }
                )
                continue
            if amount < 0:
                malformed.append(
                    {
                        "group_key": "__malformed__",
                        "sequence": sequence,
                        "audit_id": entry.id,
                        "error": "dose amount must be non-negative",
                    }
                )
                continue
            group_key = f"{entry.kind}|{shift.key}"
            payload = entry.as_dict()
            payload["sequence"] = sequence
            payload["shift_key"] = shift.key
            groups.setdefault(group_key, []).append(payload)
        return _LoadedGroups(groups=groups, malformed=malformed)

    def _source_fingerprint(self, group_key: str, entries: list[dict[str, object]]) -> str:
        chemical = str(entries[0]["kind"])
        first_time = str(entries[0]["time"])
        shift = self._schedule.locate(first_time)
        basis = []
        for entry in entries:
            chemical_name = str(entry["kind"])
            event_time = str(entry["time"])
            if entry.get("raw_amount") is None:
                basis.append(self._basis.effective(chemical_name, event_time).as_dict())
            else:
                basis.append(
                    BasisEntry(
                        chemical=chemical_name,
                        effective_from=event_time,
                        calibration=float(entry.get("calibration", 1.0)),
                        concentration=float(entry.get("concentration", 1.0)),
                    ).as_dict()
                )
        cumulative_total, cumulative_segment = self._cumulative.delta(
            chemical, shift.start_epoch, shift.end_epoch
        )
        inventory_total, inventory_movements = self._movements.consumed(
            chemical, shift.start_epoch, shift.end_epoch
        )
        canonical = json.dumps(
            {
                "group_key": group_key,
                "entries": entries,
                "basis": basis,
                "cumulative_total": cumulative_total,
                "cumulative_segment": cumulative_segment,
                "inventory_total": inventory_total,
                "inventory_movements": inventory_movements,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _load_state(self) -> dict[str, object]:
        raw, present = self._store.get(AGG_STATE_KEY)
        if not present:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        if not isinstance(payload.get("results", []), list) or not isinstance(
            payload.get("checkpoints", {}), dict
        ):
            return {}
        return payload

    def _save_state(self, state: dict[str, object]) -> None:
        self._store.put(AGG_STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True))

    def _result_from_dict(self, payload: dict[str, object]) -> AggregateResult:
        shift_payload = payload.get("shift", {})
        if not isinstance(shift_payload, dict):
            raise ValueError("persisted aggregate shift is corrupt")
        shift = self._schedule.locate(str(shift_payload.get("start", "")))
        return AggregateResult(
            id=str(payload.get("id", "")),
            group_key=str(payload.get("group_key", "")),
            chemical=str(payload.get("chemical", "")),
            shift=shift,
            status=str(payload.get("status", "")),
            dose_total=float(payload.get("dose_total", 0.0)),
            cumulative_total=_optional_float(payload.get("cumulative_total")),
            inventory_total=_optional_float(payload.get("inventory_total")),
            discrepancies=_list_of_dicts(payload.get("discrepancies")),
            entries=_list_of_dicts(payload.get("entries")),
            basis=_list_of_dicts(payload.get("basis")),
            inventory_lots=_list_of_dicts(payload.get("inventory_lots")),
            cumulative_segment=payload.get("cumulative_segment")
            if isinstance(payload.get("cumulative_segment"), dict)
            else None,
            source_fingerprint=str(payload.get("source_fingerprint", "")),
            calculated_at=str(payload.get("calculated_at", "")),
            stale_reason=str(payload.get("stale_reason", "")),
            run_id=str(payload.get("run_id", "")),
            active=bool(payload.get("active", True)),
        )

    def _run_from_dict(self, payload: dict[str, object]) -> AggregationRun:
        return AggregationRun(
            run_id=str(payload.get("run_id", "")),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at", "")),
            status=str(payload.get("status", "")),
            group_count=int(payload.get("group_count", 0)),
            calculated=int(payload.get("calculated", 0)),
            unchanged=int(payload.get("unchanged", 0)),
            failed=int(payload.get("failed", 0)),
            failures=_list_of_dicts(payload.get("failures")),
            stale=[str(item) for item in payload.get("stale", []) if isinstance(item, str)],
        )


def _supersede(
    results: list[dict[str, object]], group_key: str, reason: str, run_id: str
) -> list[dict[str, object]]:
    updated = copy.deepcopy(results)
    for item in updated:
        if item.get("group_key") == group_key and item.get("active", True):
            item["active"] = False
            item["stale_reason"] = reason
            item["superseded_by_run"] = run_id
    return updated


def _with_stale(result: AggregateResult, reason: str) -> AggregateResult:
    return AggregateResult(
        id=result.id,
        group_key=result.group_key,
        chemical=result.chemical,
        shift=result.shift,
        status=result.status,
        dose_total=result.dose_total,
        cumulative_total=result.cumulative_total,
        inventory_total=result.inventory_total,
        discrepancies=result.discrepancies,
        entries=result.entries,
        basis=result.basis,
        inventory_lots=result.inventory_lots,
        cumulative_segment=result.cumulative_segment,
        source_fingerprint=result.source_fingerprint,
        calculated_at=result.calculated_at,
        stale_reason=reason,
        run_id=result.run_id,
        active=result.active,
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
