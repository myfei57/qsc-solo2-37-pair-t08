"""Route table that maps HTTP requests onto the control service."""

from __future__ import annotations

from typing import Any

from ..app.runtime import Runtime
from ..core.config import envelope_report
from ..errors import ValidationError
from . import pages
from .router import Router


def _number(body: dict[str, Any], name: str, default: float | None = None) -> float:
    if name not in body or body[name] is None:
        if default is None:
            raise ValidationError("request field is missing", field=name)
        return float(default)
    try:
        return float(body[name])
    except (TypeError, ValueError) as exc:
        raise ValidationError("request field must be a number", field=name, value=body[name]) from exc


def _integer(body: dict[str, Any], name: str, default: int) -> int:
    return int(_number(body, name, float(default)))


def _text(body: dict[str, Any], name: str, default: str | None = None) -> str:
    if name not in body or body[name] is None:
        if default is None:
            raise ValidationError("request field is missing", field=name)
        return default
    return str(body[name])


def _optional(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    return None if value is None else str(value)


class ConsoleApp:
    """Translates one HTTP exchange into a control call and back into JSON."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.control = runtime.control
        self.router = Router()
        self._log_lines: list[str] = []
        self._register_routes()

    def log(self, message: str) -> None:
        self._log_lines.append(str(message))
        if len(self._log_lines) > 500:
            self._log_lines = self._log_lines[-500:]

    def logs(self, limit: int = 50) -> list[str]:
        return self._log_lines[-max(0, int(limit)) :]

    # -- dispatch ----------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any],
    ) -> tuple[int, Any]:
        handler, params = self.router.match(method, path)
        payload = handler(params, query, body)
        return 200, payload

    def page(self, name: str) -> str:
        control = self.control
        snapshot = control.snapshot()
        if name == "overview":
            return pages.render_overview(snapshot, control.health())
        if name == "records":
            return pages.render_records(snapshot, control.visible_records(25), control.pending_records())
        if name == "decisions":
            return pages.render_decisions(snapshot, control.decision_history(limit=25))
        if name == "cleaning":
            return pages.render_cleaning(snapshot, control.alarms.active())
        raise ValidationError("unknown page", page=name)

    def route_inventory(self) -> list[dict[str, Any]]:
        return self.router.inventory()

    # -- route wiring ------------------------------------------------------

    def _register_routes(self) -> None:
        router = self.router
        router.get("/health", self._health, "line health envelope")
        router.get("/api/state", self._state, "full control snapshot")
        router.get("/api/config", self._config, "configured operating envelope")
        router.get("/api/temperatures", self._temperatures, "sensor map with latest values")
        router.get("/api/generations", self._generations, "published generation lineages")
        router.get("/api/warranties", self._warranties, "confirmations, snapshots and baselines")
        router.get("/api/records", self._records, "committed or staged record stream")
        router.get("/api/decisions", self._decisions, "decision log with filters")
        router.get("/api/timeline", self._timeline, "state timeline with revisions")
        router.get("/api/batches", self._batches, "batch registry")
        router.get("/api/alarms", self._alarms, "alarm board")
        router.get("/api/audit", self._audit, "tamper-evident audit chain")
        router.get("/api/logs", self._logs, "recent console requests")
        router.get("/api/precheck/{action}", self._precheck, "read-only permit preview")
        router.post("/api/records/commit", self._records_commit, "advance the record watermark")
        router.post("/api/records/rollback", self._records_rollback, "discard the staged tail")
        router.post("/api/records/void", self._records_void, "tombstone one record")
        router.post("/api/batches/open", self._batch_open, "open a batch")
        router.post("/api/batches/close", self._batch_close, "close the active batch")
        router.post("/api/intake/start", self._intake_start, "enter the intake stage")
        router.post("/api/intake/receive", self._intake_receive, "stage an intake receipt")
        router.post("/api/balance/charge", self._balance_charge, "charge the balance tank")
        router.post("/api/balance/stop", self._balance_stop, "stop the balance tank")
        router.post("/api/preheat/target", self._preheat_target, "set the preheat target")
        router.post("/api/preheat/persist", self._preheat_persist, "persist the preheat temperature")
        router.post("/api/uht/ramp", self._uht_ramp, "start the sterilization ramp")
        router.post("/api/uht/confirm", self._uht_confirm, "confirm the sterilization section")
        router.post("/api/uht/stop", self._uht_stop, "stop the sterilization section")
        router.post("/api/hold/start", self._hold_start, "enter the hold stage")
        router.post("/api/hold/evaluate", self._hold_evaluate, "evaluate the hold dwell")
        router.post("/api/hold/recover", self._hold_recover, "recover the bypass latch")
        router.post("/api/cool/start", self._cool_start, "start the cooling section")
        router.post("/api/cool/stop", self._cool_stop, "stop the cooling section")
        router.post("/api/shutdown", self._shutdown, "stop every section in order")
        router.post("/api/line/reset", self._reset, "reset the line to idle")
        router.post("/api/aseptic/sterilize", self._aseptic_sterilize, "accept the sterilization confirmation")
        router.post("/api/aseptic/fill", self._aseptic_fill, "fill the aseptic tank")
        router.post("/api/aseptic/pressure", self._aseptic_pressure, "record the aseptic pressure")
        router.post("/api/cip/confirm", self._cip_confirm, "confirm the cleaning temperature")
        router.post("/api/cip/enter", self._cip_enter, "enter the cleaning branch")
        router.post("/api/cip/start", self._cip_start, "start the cleaning pump")
        router.post("/api/cip/complete", self._cip_complete, "complete the cleaning cycle")
        router.post("/api/baselines", self._baseline, "record a flow baseline")
        router.post("/api/snapshots", self._snapshot, "capture a configuration snapshot")
        router.post("/api/calibrate/temperature", self._calibrate_temperature, "recalibrate a sensor")
        router.post("/api/calibrate/flow", self._calibrate_flow, "recalibrate the flow meter")
        router.post("/api/sensors/remap", self._remap_sensor, "move a sensor to another position")
        router.post("/api/temperatures/record", self._record_temperature, "record one temperature sample")
        router.post("/api/windows/evaluate", self._evaluate_window, "evaluate a reading window")
        router.post("/api/state/record", self._record_state, "publish a state revision")
        router.post("/api/alarms/raise", self._alarm_raise, "raise an alarm")
        router.post("/api/alarms/clear", self._alarm_clear, "clear an alarm")
        router.post("/api/time/advance", self._advance_time, "advance the deterministic clock")

    # -- GET handlers ------------------------------------------------------

    def _health(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        payload = self.control.health()
        payload["recovery"] = self.runtime.recovery_report()
        payload["routes"] = self.router.count()
        return payload

    def _state(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.snapshot()

    def _config(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return {
            "envelope": envelope_report(self.control.config),
            "config": self.control.config.as_dict(),
            "generations": self.control.generations.as_dict(),
        }

    def _temperatures(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return {"sensors": self.control.temperatures(), "flow": self.control.flowmeter.gain}

    def _generations(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.generations.as_dict()

    def _warranties(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return {
            "states": self.control.warranties.state_counts(),
            "inventory": self.control.warranties.inventory(),
        }

    def _records(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        if query.get("pending") in {"1", "true", "yes"}:
            return {"pending": self.control.pending_records(), "state": self.control.stream_state()}
        limit = int(query.get("limit", 50))
        kind = query.get("kind")
        records = self.control.visible_records(limit) if kind is None else self.control.records_of_kind(kind)
        return {"visible": records[-limit:], "state": self.control.stream_state()}

    def _decisions(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return {
            "counts": self.control.decisions.counts(),
            "decisions": self.control.decision_history(
                kind=query.get("kind"),
                verdict=query.get("verdict"),
                batch_id=query.get("batch"),
                limit=int(query.get("limit", 50)),
            ),
        }

    def _timeline(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        if "revision" in query:
            return self.control.state_as_of(int(query["revision"]))
        return self.control.timeline.snapshot()

    def _batches(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        state = query.get("state")
        product = query.get("product")
        return {
            "snapshot": self.control.batches.snapshot(),
            "batches": [
                record.as_dict() for record in self.control.batches.batches(state=state, product=product)
            ],
        }

    def _alarms(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return {
            "counts": self.control.alarms.counts(),
            "active": self.control.alarms.active(),
            "history": self.control.alarms.history(limit=int(query.get("limit", 20))),
        }

    def _audit(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        limit = int(query.get("limit", 25))
        return {
            "integrity": self.control.audit.verify(),
            "entries": [entry.as_dict() for entry in self.control.audit.entries(limit=limit)],
            "targets": self.control.audit.targets(),
        }

    def _precheck(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.precheck(params["action"])

    def _logs(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return {"lines": self.logs(int(query.get("limit", 50)))}

    # -- POST handlers -----------------------------------------------------

    def _records_commit(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        through = body.get("through")
        return self.control.commit_records(None if through is None else int(through))

    def _records_rollback(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.rollback_records()

    def _records_void(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.void_record(_text(body, "record_id"), reason=_text(body, "reason", "operator rollback"))

    def _batch_open(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.open_batch(
            _text(body, "batch_id"),
            _text(body, "product"),
            reason=_text(body, "reason", "operator"),
        ).as_dict()

    def _batch_close(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.close_batch(
            _text(body, "batch_id"),
            _text(body, "outcome"),
            reason=_text(body, "reason", "operator"),
        ).as_dict()

    def _intake_start(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.start_intake(reason=_text(body, "reason", "operator"))

    def _intake_receive(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.receive(
            _number(body, "volume_litres"),
            _number(body, "temperature_c"),
            batch_id=_text(body, "batch_id"),
            reason=_text(body, "reason", "operator"),
            key=_optional(body, "key"),
        )

    def _balance_charge(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.charge_balance(_number(body, "volume_litres"), reason=_text(body, "reason", "operator"))

    def _balance_stop(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.stop_balance(reason=_text(body, "reason", "operator"))

    def _preheat_target(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.preheat.set_target(_number(body, "target_c"), reason=_text(body, "reason", "operator"))

    def _preheat_persist(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.persist_preheat_temperature(
            _text(body, "sensor_id"),
            _number(body, "raw_c"),
            reason=_text(body, "reason", "operator"),
        )

    def _uht_ramp(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.start_sterilization_ramp(_number(body, "target_c"), reason=_text(body, "reason", "operator"))

    def _uht_confirm(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        ttl = body.get("ttl_seconds")
        return self.control.confirm_sterilization(
            reason=_text(body, "reason", "operator"),
            ttl_seconds=None if ttl is None else float(ttl),
        )

    def _uht_stop(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.stop_sterilization(reason=_text(body, "reason", "operator"))

    def _hold_start(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.start_hold(reason=_text(body, "reason", "operator"))

    def _hold_evaluate(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.evaluate_dwell(
            baseline_id=_text(body, "baseline_id"),
            raw_lph=_number(body, "raw_lph"),
            reason=_text(body, "reason", "operator"),
        )

    def _hold_recover(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.recover_hold(
            _number(body, "value_c"),
            baseline_id=_text(body, "baseline_id"),
            raw_lph=_number(body, "raw_lph"),
            reason=_text(body, "reason", "operator"),
        )

    def _cool_start(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.start_cooling(reason=_text(body, "reason", "operator"))

    def _cool_stop(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.stop_cooling(reason=_text(body, "reason", "operator"))

    def _shutdown(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.shutdown(reason=_text(body, "reason", "operator shutdown"))

    def _reset(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.reset_line(reason=_text(body, "reason", "operator reset"))

    def _aseptic_sterilize(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.sterilize_aseptic_tank(
            confirmation_id=_text(body, "confirmation_id"),
            reason=_text(body, "reason", "operator"),
        )

    def _aseptic_fill(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.fill_aseptic_tank(
            _number(body, "volume_litres"),
            reason=_text(body, "reason", "operator"),
            key=_optional(body, "key"),
        )

    def _aseptic_pressure(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.aseptic.pressurize(
            _number(body, "pressure_kpa"),
            reason=_text(body, "reason", "operator"),
        )

    def _cip_confirm(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        ttl = body.get("ttl_seconds")
        return self.control.confirm_cleaning_temperature(
            _number(body, "temperature_c"),
            reason=_text(body, "reason", "operator"),
            ttl_seconds=None if ttl is None else float(ttl),
        )

    def _cip_enter(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.start_cleaning(reason=_text(body, "reason", "operator"))

    def _cip_start(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.start_cleaning_pump(_number(body, "flow_lph"), reason=_text(body, "reason", "operator"))

    def _cip_complete(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.finish_cleaning(
            _number(body, "elapsed_seconds"),
            reason=_text(body, "reason", "operator"),
        )

    def _baseline(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.confirm_flow_baseline(reason=_text(body, "reason", "operator baseline"))

    def _snapshot(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        ttl = body.get("ttl_seconds")
        return self.control.capture_snapshot(
            _text(body, "name", "state"),
            reason=_text(body, "reason", "operator snapshot"),
            ttl_seconds=None if ttl is None else float(ttl),
        )

    def _calibrate_temperature(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.recalibrate_temperature(
            _text(body, "sensor_id"),
            _number(body, "gain"),
            _number(body, "offset", 0.0),
            reason=_text(body, "reason", "calibration"),
        )

    def _calibrate_flow(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.recalibrate_flow(
            _number(body, "gain"),
            _number(body, "offset", 0.0),
            reason=_text(body, "reason", "calibration"),
        )

    def _remap_sensor(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.remap_sensor(
            _text(body, "sensor_id"),
            _text(body, "position"),
            reason=_text(body, "reason", "sensor replacement"),
        )

    def _record_temperature(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.record_temperature(
            _text(body, "sensor_id"),
            _number(body, "raw_c"),
            reason=_text(body, "reason", "operator"),
        )

    def _evaluate_window(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.evaluate_window(
            _text(body, "sensor_id"),
            _integer(body, "count", 4),
            reason=_text(body, "reason", "operator"),
            kind=_text(body, "kind", "sterilization-window"),
        )

    def _record_state(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.record_state(reason=_text(body, "reason", "operator"))

    def _alarm_raise(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.raise_alarm(
            _text(body, "code"),
            severity=_text(body, "severity", "warning"),
            message=_text(body, "message", ""),
            target=_text(body, "target", "line"),
            reason=_text(body, "reason", ""),
        )

    def _alarm_clear(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.clear_alarm(_text(body, "code"), reason=_text(body, "reason", "operator"))

    def _advance_time(self, params: dict[str, str], query: dict[str, str], body: dict[str, Any]) -> Any:
        return self.control.advance_time(_number(body, "seconds", 0.0))


__all__ = ["ConsoleApp"]
