"""HTTP handlers for every console endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.chlor import validate_demand
from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import calibrate_meter, validate_factor
from waterplant.intake import DEFAULT_WINDOW, Sensor, validate_flow
from waterplant.inventory import validate_quantity
from waterplant.ns import Stage, treatment_line
from waterplant.ph import validate_ph
from waterplant.quota import Quota, check_quota, validate_amount
from waterplant.reporting import audit_export, telemetry_export, to_csv
from waterplant.scheduler import validate_threshold
from waterplant.settle import HANDOVER, BOUNDARY_POLICIES
from waterplant.store import export_state

from . import checks, history, ops
from . import describe as describe_module
from . import simulate as simulate_module
from . import snapshot as snapshot_module
from . import telemetry as telemetry_module
from .cycle import run_cycle
from .http import Request, RequestError, Response, csv_response, json_response, text_response
from .report import text_report
from .routes import route_table
from .system import system_payload
from .version import version_payload

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def health(server: "Server", request: Request) -> Response:
    store = server.runtime.store
    return json_response(
        {"status": "ok", "keys": store.keys(), "size": store.count(), "data": export_state(store)}
    )


def health_checks(server: "Server", request: Request) -> Response:
    return json_response({"checks": checks.run_checks(server.runtime)})


def snapshot(server: "Server", request: Request) -> Response:
    return json_response(snapshot_module.collect(server.runtime))


def describe(server: "Server", request: Request) -> Response:
    return json_response(describe_module.collect(server.runtime))


def report(server: "Server", request: Request) -> Response:
    return text_response(text_report(server.runtime))


def pipeline(server: "Server", request: Request) -> Response:
    line = treatment_line()
    last = line.last()
    return json_response(
        {
            "name": line.name,
            "steps": [step.as_dict() for step in line.steps()],
            "ordered": line.before(Stage.INTAKE, Stage.COAG),
            "last_stage": "" if last is None else last.value,
        }
    )


def catalog(server: "Server", request: Request) -> Response:
    return json_response({"routes": [route.as_dict() for route in route_table()]})


def ops_counters(server: "Server", request: Request) -> Response:
    return json_response(ops.collect(server.runtime).as_dict())


def ops_reset_store(server: "Server", request: Request) -> Response:
    return json_response(ops.reset_store(server.runtime))


def telemetry(server: "Server", request: Request) -> Response:
    return json_response(telemetry_module.collect(server.runtime).as_dict())


def version(server: "Server", request: Request) -> Response:
    return json_response(version_payload())


def system(server: "Server", request: Request) -> Response:
    return json_response(system_payload())


def history_append(server: "Server", request: Request) -> Response:
    return history.append(server.runtime.store, request)


def history_list(server: "Server", request: Request) -> Response:
    return history.listing(server.runtime.store)


def history_clear(server: "Server", request: Request) -> Response:
    return history.clear(server.runtime.store)


def intake_flow(server: "Server", request: Request) -> Response:
    sensor = Sensor.from_payload(request.payload)
    validate_flow(sensor.flow)
    server.runtime.flow_repository.record(sensor)
    server.runtime.trend.record(sensor.flow)
    return json_response({"flow": sensor.flow, "turbidity": sensor.turbidity})


def intake_flow_get(server: "Server", request: Request) -> Response:
    value, present = server.runtime.flow_repository.load_flow()
    return json_response({"flow": value, "ok": present})


def coag_dose(server: "Server", request: Request) -> Response:
    flow = request.float_field("flow")
    validate_flow(flow)
    dose = server.runtime.coag_doser.update_flow_and_dose(flow)
    return json_response({"dose": dose})


def coag_turbidity(server: "Server", request: Request) -> Response:
    dose = server.runtime.sampler.judge(request.float_list("samples"))
    return json_response({"dose": dose})


def coag_ratio(server: "Server", request: Request) -> Response:
    return json_response({"ratio": server.runtime.coag_doser.current_ratio()})


def flow_replace(server: "Server", request: Request) -> Response:
    factor = request.float_field("factor")
    validate_factor(factor)
    meter = calibrate_meter(request.str_field("serial"), factor)
    server.runtime.calibration.replace(meter.factor)
    return json_response({"meter": meter.as_dict()})


def chlor_target(server: "Server", request: Request) -> Response:
    demand = request.float_field("demand")
    validate_demand(demand)
    target = server.runtime.well.update_residual_demand(demand)
    return json_response({"target": target})


def chlor_dose(server: "Server", request: Request) -> Response:
    return json_response({"dose": server.runtime.chlor_doser.apply_residual()})


def clearwell_level(server: "Server", request: Request) -> Response:
    target = request.float_field("target")
    validate_level(target)
    runtime = server.runtime
    minimum = runtime.well.adjust_level(target, runtime.inlet, runtime.outlet)
    return json_response({"min_level": minimum, "level": runtime.well.level()})


def filter_add(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    bed = server.runtime.bank.add_bed(
        request.str_field("id"), zone, request.float_field("load")
    )
    return json_response({"id": bed.id, "zone": bed.zone, "load": bed.load})


def filter_close(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.close(bed_id)
    return json_response({"closed": bed_id})


def filter_open(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.open(bed_id)
    return json_response({"opened": bed_id})


def filter_renumber(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    bed_id = request.str_field("id")
    server.runtime.bank.renumber(bed_id, zone)
    return json_response({"renumbered": bed_id, "zone": zone})


def filter_load(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    load = request.float_field("load")
    if load < 0:
        raise ValueError("load must be non-negative")
    server.runtime.bank.set_load(bed_id, load)
    return json_response({"id": bed_id, "load": load})


def filter_reset(server: "Server", request: Request) -> Response:
    server.runtime.bank.reset_closed()
    return json_response({"reset": True})


def filter_remove(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.bank.remove_bed(bed_id)
    return json_response({"removed": bed_id})


def filter_zone(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    zone, present = server.runtime.bank.bed_zone(bed_id)
    return json_response({"id": bed_id, "zone": zone, "ok": present})


def backwash_start(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.backwash.start(bed_id)
    return json_response({"started": bed_id})


def backwash_order(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    order = runtime.backwash.order_rotation()
    return json_response({"order": order, "on_duty": runtime.bank.on_duty() or ""})


def backwash_select(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    bed_id = server.runtime.backwash.select(zone)
    return json_response({"bed": bed_id, "zone": zone})


def backwash_enqueue(server: "Server", request: Request) -> Response:
    bed_id = request.str_field("id")
    server.runtime.backwash.enqueue(bed_id)
    return json_response({"enqueued": bed_id})


def backwash_recover(server: "Server", request: Request) -> Response:
    server.runtime.backwash.recover()
    return json_response({"recovered": True})


def backwash_replay(server: "Server", request: Request) -> Response:
    return json_response({"replayed": server.runtime.backwash.replay()})


def quota_add(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_amount(amount)
    return json_response({"value": server.runtime.accumulator.add(amount)})


def quota_get(server: "Server", request: Request) -> Response:
    return json_response({"value": server.runtime.accumulator.value()})


def quota_check(server: "Server", request: Request) -> Response:
    quota = Quota(chemical=request.str_field("chemical"), limit=request.float_field("limit"))
    remaining, ok = check_quota(quota.chemical, request.float_field("used"), quota.limit)
    return json_response({"remaining": remaining, "ok": ok})


def audit_list(server: "Server", request: Request) -> Response:
    return json_response({"entries": [entry.as_dict() for entry in server.runtime.auditor.entries()]})


def audit_summary(server: "Server", request: Request) -> Response:
    auditor = server.runtime.auditor
    return json_response({"count": auditor.count(), "by_kind": auditor.count_by_kind()})


def audit_filter(server: "Server", request: Request) -> Response:
    entries = server.runtime.auditor.filter(request.str_field("kind"))
    return json_response({"entries": [entry.as_dict() for entry in entries]})


def cycle(server: "Server", request: Request) -> Response:
    return run_cycle(server.runtime, request)


def simulate(server: "Server", request: Request) -> Response:
    return simulate_module.run_simulation(server.runtime, request)


def ph_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.stabilizer.state().as_dict())


def ph_read(server: "Server", request: Request) -> Response:
    value = request.float_field("value")
    validate_ph(value)
    return json_response(server.runtime.stabilizer.read(value).as_dict())


def ph_verdict(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.stabilizer.stabilize().as_dict())


def schedule_state(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    return json_response(runtime.scheduler.state(runtime.bank).as_dict())


def schedule_threshold(server: "Server", request: Request) -> Response:
    value = request.float_field("threshold")
    validate_threshold(value)
    return json_response({"threshold": server.runtime.scheduler.set_threshold(value)})


def schedule_plan(server: "Server", request: Request) -> Response:
    runtime = server.runtime
    entries = runtime.scheduler.plan(runtime.bank)
    return json_response(
        {"entries": [entry.as_dict() for entry in entries], "due": runtime.scheduler.due(runtime.bank)}
    )


def intake_trend(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.trend.stats().as_dict())


def intake_trend_window(server: "Server", request: Request) -> Response:
    value = request.int_field("window", DEFAULT_WINDOW)
    server.runtime.trend.set_window(value)
    return json_response({"window": server.runtime.trend.window()})


def intake_trend_reset(server: "Server", request: Request) -> Response:
    server.runtime.trend.reset()
    return json_response({"reset": True})


def audit_totals(server: "Server", request: Request) -> Response:
    return json_response({"totals": server.runtime.auditor.totals()})


def inventory_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.inventory.state().as_dict())


def inventory_receive(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_quantity(amount)
    lot = server.runtime.inventory.receive(
        request.str_field("chemical"),
        request.str_field("lot_id"),
        amount,
        at=request.str_field("at") or None,
    )
    return json_response({"lot": lot.as_dict()})


def inventory_consume(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_quantity(amount)
    chemical = request.str_field("chemical")
    at = request.str_field("at") or None
    ref = request.str_field("ref")
    consumed = server.runtime.inventory.consume(chemical, amount, at=at, ref=ref)
    return json_response(
        {"consumed": consumed, "balance": server.runtime.inventory.balance(chemical)}
    )


def inventory_reorder_level(server: "Server", request: Request) -> Response:
    level = request.float_field("level")
    validate_quantity(level, "level")
    return json_response({"reorder_level": server.runtime.inventory.set_reorder_level(level)})


def metrics(server: "Server", request: Request) -> Response:
    return json_response(server.metrics.snapshot().as_dict())


def metrics_reset(server: "Server", request: Request) -> Response:
    server.metrics.reset()
    return json_response({"reset": True})


def export_audit(server: "Server", request: Request) -> Response:
    columns, rows = audit_export(server.runtime.auditor)
    return csv_response(to_csv(columns, rows))


def _shift_keys(request: Request) -> set[str] | None:
    keys = request.payload.get("shift_keys")
    if keys is None:
        return None
    if not isinstance(keys, list):
        raise RequestError(400, "shift_keys must be a list")
    return {str(key) for key in keys}


def _chemical_filter(request: Request) -> list[str] | None:
    chemicals = request.payload.get("chemicals")
    if chemicals is None:
        return None
    if not isinstance(chemicals, list):
        raise RequestError(400, "chemicals must be a list")
    return [str(item) for item in chemicals]


def settle_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.settlement.state())


def settle_compute(server: "Server", request: Request) -> Response:
    force = bool(request.payload.get("force", False))
    run = server.runtime.settlement.compute(
        shift_keys=_shift_keys(request),
        chemicals=_chemical_filter(request),
        force=force,
    )
    return json_response(run)


def settle_recover(server: "Server", request: Request) -> Response:
    result = server.runtime.settlement.recover(shift_keys=_shift_keys(request))
    return json_response(result)


def settle_segments(server: "Server", request: Request) -> Response:
    chemical = request.str_field("chemical")
    shift_key = request.str_field("shift_key")
    if chemical and shift_key:
        return json_response(
            {"segments": server.runtime.settlement.segment_history(chemical, shift_key)}
        )
    return json_response({"segments": server.runtime.settlement.latest_segments()})


def settle_runs(server: "Server", request: Request) -> Response:
    return json_response({"runs": server.runtime.settlement.runs()})


def settle_schedule_get(server: "Server", request: Request) -> Response:
    schedule = server.runtime.settlement.schedule
    return json_response(
        {
            **schedule.state(),
            "current": schedule.current().as_dict(),
        }
    )


def settle_schedule_arrange(server: "Server", request: Request) -> Response:
    payload_roster = request.payload.get("roster")
    if not isinstance(payload_roster, list) or not payload_roster:
        raise RequestError(400, "roster must be a non empty list of {crew,start_minute}")
    roster: list[tuple[str, int]] = []
    for item in payload_roster:
        if not isinstance(item, dict):
            raise RequestError(400, "each roster row must be an object")
        roster.append((str(item["crew"]), int(item["start_minute"])))
    policy = request.str_field("boundary_policy", HANDOVER)
    if policy not in BOUNDARY_POLICIES:
        raise RequestError(400, f"boundary_policy must be one of {BOUNDARY_POLICIES}")
    segments = server.runtime.settlement.schedule.arrange(roster, policy)
    return json_response(
        {
            "boundary_policy": policy,
            "roster": [segment.as_dict() for segment in segments],
        }
    )


def settle_calibrations(server: "Server", request: Request) -> Response:
    chemical = request.str_field("chemical")
    versions = server.runtime.settlement.calibrations.versions(chemical or None)
    return json_response({"versions": [version.as_dict() for version in versions]})


def settle_calibration_register(server: "Server", request: Request) -> Response:
    value = request.float_field("value")
    if value <= 0:
        raise RequestError(400, "calibration value must be positive")
    effective_from = request.str_field("effective_from") or None
    version = server.runtime.settlement.calibrations.register(
        request.str_field("chemical"),
        value,
        effective_from=effective_from,
        note=request.str_field("note"),
    )
    return json_response(version.as_dict())


def settle_concentrations(server: "Server", request: Request) -> Response:
    chemical = request.str_field("chemical")
    versions = server.runtime.settlement.concentrations.versions(chemical or None)
    return json_response({"versions": [version.as_dict() for version in versions]})


def settle_concentration_register(server: "Server", request: Request) -> Response:
    value = request.float_field("value")
    if value <= 0:
        raise RequestError(400, "concentration value must be positive")
    effective_from = request.str_field("effective_from") or None
    version = server.runtime.settlement.concentrations.register(
        request.str_field("chemical"),
        value,
        effective_from=effective_from,
        note=request.str_field("note"),
    )
    return json_response(version.as_dict())


def settle_makeups(server: "Server", request: Request) -> Response:
    makeup_ledger = server.runtime.settlement.makeups
    chemical = request.str_field("chemical")
    shift_key = request.str_field("shift_key")
    makeups = makeup_ledger.makeups(chemical or None, shift_key or None)
    return json_response(
        {"makeups": [makeup.as_dict() for makeup in makeups], "dangling": [
            makeup.as_dict() for makeup in makeup_ledger.dangling()
        ]}
    )


def settle_makeup_add(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    at = request.str_field("at") or None
    raw_meter = request.payload.get("meter_value")
    meter_value = None if raw_meter is None else float(raw_meter)
    draw_stock = bool(request.payload.get("draw_stock", True))
    makeup = server.runtime.settlement.makeups.add(
        chemical=request.str_field("chemical"),
        amount=amount,
        for_shift=request.str_field("for_shift"),
        reason=request.str_field("reason"),
        at=at,
        draw_stock=draw_stock,
        meter_value=meter_value,
    )
    return json_response(makeup.as_dict())


def settle_meter_readings(server: "Server", request: Request) -> Response:
    chemical = request.str_field("chemical")
    readings = server.runtime.settlement.meters.readings(chemical or None)
    return json_response({"readings": [reading.as_dict() for reading in readings]})


def settle_meter_reading_add(server: "Server", request: Request) -> Response:
    chemical = request.str_field("chemical")
    value = request.float_field("value")
    at = request.str_field("at") or None
    source = request.str_field("source") or "manual"
    reading = server.runtime.settlement.meters.reading(chemical, value, at=at, source=source)
    return json_response(reading.as_dict())


def settle_meter_cap(server: "Server", request: Request) -> Response:
    chemical = request.str_field("chemical")
    meters = server.runtime.settlement.meters
    if request.payload.get("cap") is None:
        return json_response({"chemical": chemical, "wrap_cap": meters.wrap_cap(chemical)})
    cap = request.float_field("cap")
    return json_response({"chemical": chemical, "wrap_cap": meters.set_wrap_cap(chemical, cap)})


def export_telemetry(server: "Server", request: Request) -> Response:
    columns, rows = telemetry_export(telemetry_module.collect(server.runtime))
    return csv_response(to_csv(columns, rows))
