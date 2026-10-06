"""Behavioural tests for dosing settlement."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.audit.auditor import AUDIT_KEY
from waterplant.settle import (
    HANDOVER,
    ON_HOUR,
    RUN_COMPLETE,
    RUN_PARTIAL,
    SEGMENT_MISMATCH,
    SEGMENT_MISSING_METER,
    SEGMENT_MISSING_STOCK,
    SEGMENT_RECONCILED,
    SEGMENT_ERROR,
    Settlement,
)
from waterplant.store import Store, load_commands


def settlement(tmp: str) -> Settlement:
    store = Store.open(f"{tmp}/state.json")
    settle = Settlement(store)
    settle.schedule.use_defaults()
    return settle


B = "2026-10-05T08:00:00Z|B"
B_NEXT = "2026-10-06T08:00:00Z|B"


class ScheduleCase(unittest.TestCase):
    def test_default_whole_hour_handover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            self.assertEqual(settle.schedule.shift_for("2026-10-05T07:59:59Z").crew, "A")
            shift = settle.schedule.shift_for("2026-10-05T08:00:00Z")
            self.assertEqual(shift.crew, "B")
            self.assertEqual(shift.key, B)
            self.assertEqual(shift.start, "2026-10-05T08:00:00Z")
            self.assertEqual(shift.end, "2026-10-05T16:00:00Z")

    def test_boundaries_follow_handover_minutes_not_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.schedule.arrange(
                [("A", 7 * 60 + 30), ("B", 15 * 60 + 30), ("C", 23 * 60 + 30)],
                HANDOVER,
            )
            self.assertEqual(settle.schedule.shift_for("2026-10-05T07:45:00Z").crew, "A")
            self.assertEqual(settle.schedule.shift_for("2026-10-05T15:45:00Z").crew, "B")
            settle.schedule.arrange(
                [("A", 7 * 60 + 30), ("B", 15 * 60 + 30), ("C", 23 * 60 + 30)],
                ON_HOUR,
            )
            shift = settle.schedule.shift_for("2026-10-05T07:45:00Z")
            self.assertEqual(shift.crew, "A")
            self.assertEqual(shift.start, "2026-10-05T07:00:00Z")

    def test_roster_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            with self.assertRaises(ValueError):
                settle.schedule.arrange([("A", 100), ("B", 100)], HANDOVER)
            with self.assertRaises(ValueError):
                settle.schedule.arrange([("A", 8 * 60), ("B", 0)], HANDOVER)
            with self.assertRaises(ValueError):
                settle.schedule.arrange([("A", -1)], HANDOVER)


class BasisCase(unittest.TestCase):
    def test_effective_dated_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            value, version_id = settle.calibrations.effective_for(
                "coagulant", "2026-10-05T09:00:00Z", live_factor=1.0
            )
            self.assertEqual((value, version_id), (1.0, None))
            version = settle.calibrations.register(
                "coagulant", 1.5, effective_from="2026-10-05T12:00:00Z"
            )
            self.assertEqual(
                settle.calibrations.effective_for(
                    "coagulant", "2026-10-05T11:59:00Z", live_factor=1.0
                ),
                (1.0, None),
            )
            self.assertEqual(
                settle.calibrations.effective_for(
                    "coagulant", "2026-10-05T12:01:00Z", live_factor=1.0
                ),
                (1.5, version.id),
            )
            # other chemicals keep the identity calibration even with a live factor
            self.assertEqual(
                settle.calibrations.effective_for(
                    "chlorine", "2026-10-05T12:01:00Z", live_factor=3.0
                ),
                (1.0, None),
            )

    def test_later_version_does_not_retouch_old_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.auditor.record("coagulant", "4.0", at="2026-10-05T09:00:00Z")
            first = settle.compute()
            settle.calibrations.register(
                "coagulant", 2.0, effective_from="2026-10-05T00:00:00Z"
            )
            views = settle.latest_segments()
            target = [v for v in views if v["shift_key"] == B][0]
            self.assertTrue(target["stale"])
            self.assertEqual(target["state"], "stale")
            rerun = settle.compute(force=True)
            segment = [s for s in rerun["segments"] if s["shift_key"] == B][0]
            self.assertEqual(segment["dosed_standard"], 8.0)
            self.assertEqual(segment["basis"][0]["calibration"], 2.0)
            # the old run document survives and is marked superseded
            history = settle.segment_history("coagulant", B)
            self.assertEqual([h["state"] for h in history], ["superseded", "current"])
            self.assertEqual(history[0]["dosed_standard"], 4.0)


class ReconcileCase(unittest.TestCase):
    def _balanced_shift(self, settle: Settlement, chemical: str) -> None:
        settle.auditor.record(chemical, "10.0", at="2026-10-05T09:00:00Z")
        settle.auditor.record(chemical, "5.0", at="2026-10-05T10:00:00Z")
        settle.inventory.receive(chemical, "L1", 100.0, at="2026-10-05T08:30:00Z")
        settle.inventory.consume(chemical, 15.0, at="2026-10-05T12:00:00Z")
        settle.meters.reading(chemical, 0.0, at="2026-10-05T08:00:00Z")
        settle.meters.reading(chemical, 15.0, at="2026-10-05T15:59:00Z")

    def test_three_way_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            self._balanced_shift(settle, "coagulant")
            run = settle.compute()
            segment = [s for s in run["segments"] if s["shift_key"] == B][0]
            self.assertEqual(run["status"], RUN_COMPLETE)
            self.assertEqual(segment["status"], SEGMENT_RECONCILED)
            self.assertEqual((segment["dosed_raw"], segment["meter"], segment["stock"]), (15.0, 15.0, 15.0))

    def test_mismatch_names_segment_and_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.auditor.record("coagulant", "10.0", at="2026-10-05T09:00:00Z")
            settle.inventory.receive("coagulant", "L1", 100.0, at="2026-10-05T08:30:00Z")
            settle.inventory.consume("coagulant", 9.0, at="2026-10-05T12:00:00Z")
            settle.meters.reading("coagulant", 0.0, at="2026-10-05T08:00:00Z")
            settle.meters.reading("coagulant", 10.0, at="2026-10-05T15:59:00Z")
            run = settle.compute()
            segment = [s for s in run["segments"] if s["shift_key"] == B][0]
            # annotate policy: result is still published, with the pair flagged
            self.assertEqual(run["status"], RUN_COMPLETE)
            self.assertEqual(segment["status"], SEGMENT_MISMATCH)
            discrepancy = segment["discrepancies"][0]
            self.assertEqual(discrepancy["pair"], ["dosed", "stock"])
            self.assertEqual(discrepancy["delta"], 1.0)
            self.assertIn(B, discrepancy["segment"])

    def test_missing_sides_are_not_false_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.auditor.record("coagulant", "10.0", at="2026-10-05T09:00:00Z")
            run = settle.compute()
            segment = [s for s in run["segments"] if s["shift_key"] == B][0]
            self.assertEqual(segment["status"], SEGMENT_MISMATCH)
            settle.meters.reading("coagulant", 10.0, at="2026-10-05T15:59:00Z")
            run = settle.compute(force=True)
            segment = [s for s in run["segments"] if s["shift_key"] == B][0]
            self.assertEqual(segment["status"], SEGMENT_MISSING_STOCK)
            settle.inventory.receive("coagulant", "L1", 100.0, at="2026-10-05T08:30:00Z")
            settle.inventory.consume("coagulant", 10.0, at="2026-10-05T12:00:00Z")
            run = settle.compute(force=True)
            segment = [s for s in run["segments"] if s["shift_key"] == B][0]
            self.assertEqual(segment["status"], SEGMENT_RECONCILED)

    def test_wrapping_meter_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.auditor.record("chlorine", "60.0", at="2026-10-05T09:00:00Z")
            settle.meters.reading("chlorine", 90.0, at="2026-10-05T08:00:00Z")
            settle.meters.reading("chlorine", 50.0, at="2026-10-05T15:59:00Z")
            run = settle.compute()
            segment = [s for s in run["segments"] if s["shift_key"] == B][0]
            self.assertEqual(segment["meter"], 60.0)
            self.assertEqual(segment["status"], SEGMENT_MISSING_STOCK)

    def test_backwards_jump_on_unwrapped_meter_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.meters.set_wrap_cap("coagulant", 0.0)
            settle.auditor.record("coagulant", "5.0", at="2026-10-05T09:00:00Z")
            settle.meters.reading("coagulant", 20.0, at="2026-10-05T09:00:00Z")
            settle.meters.reading("coagulant", 8.0, at="2026-10-05T10:00:00Z")
            run = settle.compute()
            segment = [s for s in run["segments"] if s["shift_key"] == B][0]
            self.assertEqual(segment["status"], SEGMENT_MISMATCH)
            self.assertTrue(
                any(
                    "went backwards" in str(discrepancy)
                    for discrepancy in segment["discrepancies"]
                )
            )


class MakeupCase(unittest.TestCase):
    def test_makeup_settles_against_target_shift_and_stales_old_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.auditor.record("coagulant", "10.0", at="2026-10-05T09:00:00Z")
            settle.inventory.receive("coagulant", "L1", 100.0, at="2026-10-05T08:30:00Z")
            settle.inventory.consume("coagulant", 10.0, at="2026-10-05T12:00:00Z")
            settle.meters.reading("coagulant", 0.0, at="2026-10-05T08:00:00Z")
            settle.meters.reading("coagulant", 10.0, at="2026-10-05T15:59:00Z")
            settle.compute()

            # correction is physically applied during the next crew's shift,
            # a day later; its stock draw and meter reading are linked to it
            settle.makeups.add(
                "coagulant",
                2.0,
                B,
                reason="underdose",
                at="2026-10-06T09:00:00Z",
                meter_value=12.0,
            )

            old = [v for v in settle.latest_segments() if v["shift_key"] == B][0]
            self.assertTrue(old["stale"])
            self.assertEqual(old["dosed_raw"], 10.0)

            settle.compute()
            target = settle.run_for(B, "coagulant")
            segment = [s for s in target["segments"] if s["shift_key"] == B][0]
            self.assertEqual(segment["dosed_raw"], 12.0)
            self.assertEqual(segment["makeup_count"], 1)
            self.assertEqual(segment["meter"], 12.0)
            self.assertEqual(segment["stock"], 12.0)
            self.assertEqual(segment["status"], SEGMENT_RECONCILED)
            # the linked correction records never leak into the later shift
            next_shift = [
                s for s in target["segments"] if s["shift_key"] == B_NEXT
            ]
            self.assertEqual(next_shift, [])


class IdempotencyAndRecoveryCase(unittest.TestCase):
    def test_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.auditor.record("coagulant", "1.0", at="2026-10-05T09:00:00Z")
            first = settle.compute()
            second = settle.compute()
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(len(settle.runs()), 2)

    def test_partial_failure_is_persisted_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            settle = Settlement(store)
            settle.schedule.use_defaults()
            settle.auditor.record("coagulant", "5.0", at="2026-10-05T09:00:00Z")
            settle.auditor.record("chlorine", "2.0", at="2026-10-05T10:00:00Z")

            raw = json.loads(store.get(AUDIT_KEY)[0])
            raw.append(
                json.dumps(
                    {"id": "bad", "time": "2026-10-05T11:00:00Z", "kind": "coagulant", "detail": "x"}
                )
            )
            store.put(AUDIT_KEY, json.dumps(raw))

            failed = settle.compute(force=True)
            self.assertEqual(failed["status"], RUN_PARTIAL)
            bad_segment = [
                s for s in failed["segments"] if s["shift_key"] == B and s["chemical"] == "coagulant"
            ][0]
            self.assertEqual(bad_segment["status"], SEGMENT_ERROR)
            # good entries in the same run survive
            self.assertEqual(bad_segment["dosed_raw"], 5.0)
            chlor = [
                s for s in failed["segments"] if s["shift_key"] == B and s["chemical"] == "chlorine"
            ][0]
            self.assertNotEqual(chlor["status"], SEGMENT_ERROR)

            good = [item for item in raw if '"bad"' not in item]
            store.put(AUDIT_KEY, json.dumps(good))
            recovered = settle.recover()["run"]
            self.assertEqual(recovered["status"], RUN_COMPLETE)
            self.assertTrue(settle.compute().get("idempotent"))


class ImmutabilityCase(unittest.TestCase):
    def test_runs_and_raw_records_are_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            settle = Settlement(store)
            settle.schedule.use_defaults()
            settle.auditor.record("coagulant", "3.0", at="2026-10-05T09:00:00Z")
            settle.inventory.receive("coagulant", "L1", 10.0, at="2026-10-05T08:30:00Z")
            settle.compute()
            settle.makeups.add(
                "coagulant",
                1.0,
                B,
                reason="x",
                at="2026-10-06T09:00:00Z",
                meter_value=4.0,
            )
            settle.compute()

            audit_items = load_commands(store, AUDIT_KEY)
            self.assertEqual(len(audit_items), 2)
            self.assertEqual(json.loads(audit_items[0])["detail"], "3.0")
            # no run document is ever overwritten: reruns only append
            run_items = load_commands(store, "settle:runs")
            first_run = json.loads(run_items[0])
            self.assertEqual(
                [s["dosed_raw"] for s in first_run["segments"] if s["chemical"] == "coagulant"],
                [3.0],
            )
            history = settle.segment_history("coagulant", B)
            self.assertEqual([h["state"] for h in history], ["superseded", "current"])


class InventoryMovementCase(unittest.TestCase):
    def test_consumption_windows_align_to_shifts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settle = settlement(tmp)
            settle.inventory.receive("coagulant", "L1", 100.0, at="2026-10-05T00:00:00Z")
            settle.inventory.consume("coagulant", 3.0, at="2026-10-05T07:59:00Z")
            settle.inventory.consume("coagulant", 4.0, at="2026-10-05T08:00:01Z")
            windows = [
                (shift.key, shift.start_epoch, shift.end_epoch)
                for shift in settle.schedule.shifts_between("2026-10-05T00:00:00Z", "2026-10-05T16:00:00Z")
            ]
            rows = settle.inventory.consumed_by_shift("coagulant", windows)
            self.assertEqual(rows["2026-10-05T00:00:00Z|A"]["stock"], 3.0)
            self.assertEqual(rows[B]["stock"], 4.0)


if __name__ == "__main__":
    unittest.main()
