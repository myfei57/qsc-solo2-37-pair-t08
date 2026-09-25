"""Control service: the single entry point every interface talks to."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..alarms.board import AlarmBoard
from ..aseptic.tank import AsepticTank
from ..balance.tank import BalanceTank
from ..batches.decision import DecisionLog, StateTimeline, WindowDecision
from ..batches.registry import BatchRecord, BatchRegistry
from ..cip.cycle import CipCycle
from ..cool.cooler import Cooler
from ..core.clock import Clock
from ..core.config import ControlConfig, envelope_report
from ..errors import NotFoundError, StateError
from ..hold.tube import HoldTube
from ..instrumentation.flowmeter import FlowMeter
from ..instrumentation.thermometry import Thermometry
from ..intake.station import IntakeStation
from ..persistence.audit import AuditLedger
from ..persistence.journal import RecordJournal
from ..persistence.store import DurableStore
from ..preheat.heater import PreheatSection
from ..stages import gates as gate_names
from ..stages import latches as latch_names
from ..stages.gates import GateBoard
from ..stages.latches import LatchBoard
from ..stages.machine import Stage, StageMachine
from ..telemetry.metrics import MetricsRegistry
from ..uht.section import UhtSection
from ..versioning.generations import GenerationRegistry
from ..versioning.warranties import WarrantyBook

ACTION_GATES: dict[str, tuple[str, ...]] = {
    "sterilization-ramp": (gate_names.TEMPERATURE_DURABLE,),
    "aseptic-fill": (gate_names.ASEPTIC_STERILE,),
    "cip-pump-start": (gate_names.CIP_TEMPERATURE_CONFIRMED,),
    "cooling-stop": (gate_names.STERILIZATION_STOPPED,),
    "balance-stop": (gate_names.COOLING_STOPPED,),
}

ACTION_LATCHES: dict[str, tuple[str, ...]] = {
    "sterilization-ramp": (latch_names.STERILIZATION_INTERLOCK,),
    "aseptic-fill": (latch_names.ASEPTIC_PRESSURE,),
    "cip-pump-start": (latch_names.CIP_ALARM,),
}


class LineControl:
    """Sequences the sections, publishes decisions and owns the record stream."""

    def __init__(
        self,
        *,
        config: ControlConfig,
        store: DurableStore,
        clock: Clock,
        audit: AuditLedger,
        alarms: AlarmBoard,
        events: RecordJournal,
        generations: GenerationRegistry,
        warranties: WarrantyBook,
        stages: StageMachine,
        gates: GateBoard,
        latches: LatchBoard,
        thermometry: Thermometry,
        flowmeter: FlowMeter,
        intake: IntakeStation,
        balance: BalanceTank,
        preheat: PreheatSection,
        uht: UhtSection,
        hold: HoldTube,
        cool: Cooler,
        aseptic: AsepticTank,
        cip: CipCycle,
        batches: BatchRegistry,
        decisions: DecisionLog,
        timeline: StateTimeline,
        metrics: MetricsRegistry,
    ) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self.audit = audit
        self.alarms = alarms
        self.events = events
        self.generations = generations
        self.warranties = warranties
        self.stages = stages
        self.gates = gates
        self.latches = latches
        self.thermometry = thermometry
        self.flowmeter = flowmeter
        self.intake = intake
        self.balance = balance
        self.preheat = preheat
        self.uht = uht
        self.hold = hold
        self.cool = cool
        self.aseptic = aseptic
        self.cip = cip
        self.batches = batches
        self.decisions = decisions
        self.timeline = timeline
        self.window = WindowDecision(config.temperature)
        self.metrics = metrics

    # -- helpers -----------------------------------------------------------

    def _count(self, name: str) -> None:
        self.metrics.increment(name)

    def _require_stage(self, stage: Stage, *, action: str) -> None:
        if not self.stages.is_at(stage):
            raise StateError(
                "action is not permitted at the current stage",
                action=action,
                expected=stage.value,
                stage=self.stages.current().value,
            )

    # -- configuration -----------------------------------------------------

    def apply_config(self, config: ControlConfig, *, reason: str) -> dict[str, Any]:
        """Publish the generations that changed and report what moved."""

        config.validate()
        published: list[dict[str, Any]] = []
        for scope, payload in config.scopes().items():
            revision = self.generations.republish_if_changed(scope, payload, reason=reason)
            if revision is not None:
                published.append({"scope": scope, "generation": revision.generation})
        if published:
            invalidated = self.warranties.expire_stale()
            self.config = config
            self.window = WindowDecision(config.temperature)
            for component in (
                self.alarms,
                self.thermometry,
                self.flowmeter,
                self.intake,
                self.balance,
                self.preheat,
                self.uht,
                self.hold,
                self.cool,
                self.aseptic,
                self.cip,
            ):
                component.config = config
            self.audit.record("config-apply", "line", str(reason), cause=None)
            return {"published": published, "invalidated": invalidated}
        return {"published": [], "invalidated": 0}

    # -- record stream -----------------------------------------------------

    def commit_records(self, through: int | None = None) -> dict[str, Any]:
        state = self.events.commit(through)
        self.metrics.gauge("records.watermark", state.watermark)
        self.audit.record("records-commit", "line-events", f"watermark={state.watermark}", cause=None)
        return state.as_dict()

    def rollback_records(self) -> dict[str, Any]:
        discarded = self.events.rollback()
        self.audit.record("records-rollback", "line-events", f"discarded={discarded}", cause=None)
        return {"discarded": discarded, "state": self.events.state().as_dict()}

    def void_record(self, record_id: str, *, reason: str) -> dict[str, Any]:
        record = self.events.tombstone(record_id, reason)
        self.audit.record("records-void", record_id, str(reason), cause=None)
        return record.as_dict()

    def visible_records(self, limit: int = 50) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.events.visible()[-max(0, int(limit)) :]]

    def pending_records(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.events.pending()]

    def records_of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.events.of_kind(str(kind))]

    def stream_state(self) -> dict[str, Any]:
        return self.events.state().as_dict()

    # -- batch -------------------------------------------------------------

    def open_batch(self, batch_id: str, product: str, *, reason: str) -> BatchRecord:
        record = self.batches.open(batch_id, product, reason=reason)
        self._count("batch.open")
        return record

    def close_batch(self, batch_id: str, outcome: str, *, reason: str) -> BatchRecord:
        validated = self.batches.validate_outcome(outcome)
        record = self.batches.close(batch_id, validated, reason=reason)
        self._count("batch.close")
        return record

    # -- production sequence ----------------------------------------------

    def start_intake(self, *, reason: str) -> dict[str, Any]:
        transition = self.stages.enter(Stage.INTAKE, reason=reason)
        self._count("stage.enter")
        return transition.as_dict()

    def receive(
        self,
        volume_litres: float,
        temperature_c: float,
        *,
        batch_id: str,
        reason: str,
        key: str | None = None,
    ) -> dict[str, Any]:
        self._require_stage(Stage.INTAKE, action="receive")
        result = self.intake.register(
            volume_litres,
            temperature_c,
            batch_id=batch_id,
            reason=reason,
            key=key,
        )
        self._count("intake.receive")
        return result

    def charge_balance(self, volume_litres: float, *, reason: str) -> dict[str, Any]:
        self._require_stage(Stage.INTAKE, action="balance-charge")
        entry = self.balance.charge(volume_litres, reason=reason)
        self.stages.enter(Stage.BALANCE, reason=reason)
        self._count("balance.charge")
        return entry

    def persist_preheat_temperature(self, sensor_id: str, raw_c: float, *, reason: str) -> dict[str, Any]:
        self._require_stage(Stage.BALANCE, action="preheat-persist")
        record = self.preheat.persist_temperature(sensor_id, raw_c, reason=reason)
        self.stages.enter(Stage.PREHEAT, reason=reason)
        self._count("preheat.persist")
        return record

    def start_sterilization_ramp(self, target_c: float, *, reason: str) -> dict[str, Any]:
        self.gates.require_open(gate_names.TEMPERATURE_DURABLE, action="sterilization-ramp")
        self.latches.require_clear(latch_names.STERILIZATION_INTERLOCK, action="sterilization-ramp")
        self._require_stage(Stage.PREHEAT, action="sterilization-ramp")
        entry = self.uht.start_ramp(target_c, reason=reason)
        self.stages.enter(Stage.STERILIZE, reason=reason)
        self._count("uht.ramp")
        return entry

    def confirm_sterilization(self, *, reason: str, ttl_seconds: float | None = None) -> dict[str, Any]:
        self._require_stage(Stage.STERILIZE, action="sterilization-confirm")
        confirmation = self.uht.confirm_sterilization(reason=reason, ttl_seconds=ttl_seconds)
        self._count("uht.confirm")
        return confirmation.as_dict()

    def sterilize_aseptic_tank(self, *, confirmation_id: str, reason: str) -> dict[str, Any]:
        issued = self.uht.require_confirmation()
        if issued.confirmation_id != str(confirmation_id):
            raise StateError(
                "the aseptic tank accepted a confirmation this section never issued",
                section="uht",
                expected=issued.confirmation_id,
                supplied=str(confirmation_id),
            )
        result = self.aseptic.sterilize(confirmation_id=confirmation_id, reason=reason)
        self._count("aseptic.sterilize")
        return result

    def start_hold(self, *, reason: str) -> dict[str, Any]:
        self._require_stage(Stage.STERILIZE, action="hold-start")
        transition = self.stages.enter(Stage.HOLD, reason=reason)
        self._count("stage.enter")
        return transition.as_dict()

    def evaluate_dwell(self, *, baseline_id: str, raw_lph: float, reason: str) -> dict[str, Any]:
        entry = self.hold.evaluate(baseline_id=baseline_id, raw_lph=raw_lph, reason=reason)
        self.decisions.record(
            "dwell",
            entry["baseline_id"],
            entry["verdict"],
            detail=f"{entry['dwell_seconds']:g}s",
            batch_id=self._active_batch_id(),
            generation=entry["baseline_generation"],
        )
        self._count("hold.evaluate")
        return entry

    def recover_hold(self, value_c: float, *, baseline_id: str, raw_lph: float, reason: str) -> dict[str, Any]:
        result = self.hold.recover(
            value_c,
            baseline_id=baseline_id,
            raw_lph=raw_lph,
            reason=reason,
        )
        self.decisions.record(
            "hold-recover",
            "hold-tube",
            "pass" if result["cleared"] else "hold",
            detail=result["latch"]["detail"],
            batch_id=self._active_batch_id(),
            generation=self.generations.generation("sensors"),
        )
        return result

    def start_cooling(self, *, reason: str) -> dict[str, Any]:
        self._require_stage(Stage.HOLD, action="cooling-start")
        self.stages.enter(Stage.COOL, reason=reason)
        entry = self.cool.start(reason=reason)
        self._count("cool.start")
        return entry

    def fill_aseptic_tank(self, volume_litres: float, *, reason: str, key: str | None = None) -> dict[str, Any]:
        self._require_stage(Stage.COOL, action="aseptic-fill")
        result = self.aseptic.fill(volume_litres, reason=reason, key=key)
        self.stages.enter(Stage.ASEPTIC_FILL, reason=reason)
        self._count("aseptic.fill")
        return result

    def complete_batch(self, outcome: str, *, reason: str) -> dict[str, Any]:
        self._require_stage(Stage.ASEPTIC_FILL, action="batch-complete")
        active = self.batches.active()
        if active is None:
            raise StateError("no batch is open", action="batch-complete")
        closed = self.close_batch(active.batch_id, outcome, reason=reason)
        self.stages.enter(Stage.COMPLETE, reason=reason)
        return closed.as_dict()

    def stop_sterilization(self, *, reason: str) -> dict[str, Any]:
        entry = self.uht.stop(reason=reason)
        self._count("uht.stop")
        return entry

    def stop_cooling(self, *, reason: str) -> dict[str, Any]:
        entry = self.cool.stop(reason=reason)
        self._count("cool.stop")
        return entry

    def stop_balance(self, *, reason: str) -> dict[str, Any]:
        entry = self.balance.stop(reason=reason)
        self._count("balance.stop")
        return entry

    def shutdown(self, *, reason: str) -> dict[str, Any]:
        """Stop the sections in the only permitted order."""

        steps = [
            self.stop_sterilization(reason=reason),
            self.stop_cooling(reason=reason),
            self.stop_balance(reason=reason),
        ]
        return {"steps": steps, "stage": self.stages.current().value}

    def reset_line(self, *, reason: str) -> dict[str, Any]:
        transition = self.stages.reset(reason=reason)
        self._count("stage.reset")
        return transition.as_dict()

    # -- cleaning ----------------------------------------------------------

    def start_cleaning(self, *, reason: str) -> dict[str, Any]:
        transition = self.stages.start_cleaning(reason=reason)
        self._count("cip.enter")
        return transition.as_dict()

    def confirm_cleaning_temperature(
        self,
        value_c: float,
        *,
        reason: str,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        result = self.cip.confirm_temperature(value_c, reason=reason, ttl_seconds=ttl_seconds)
        self._count("cip.confirm")
        return result

    def start_cleaning_pump(self, flow_lph: float, *, reason: str) -> dict[str, Any]:
        entry = self.cip.start_pump(flow_lph=flow_lph, reason=reason)
        self._count("cip.start")
        return entry

    def finish_cleaning(self, elapsed_seconds: float, *, reason: str) -> dict[str, Any]:
        completion = self.cip.cycle_complete(elapsed_seconds=elapsed_seconds, reason=reason)
        stop = self.cip.stop_pump(reason=reason)
        transition = self.stages.advance(reason=reason)
        return {"completion": completion, "stop": stop, "stage": transition.as_dict()}

    # -- instrumentation and evidence -------------------------------------

    def record_temperature(self, sensor_id: str, raw_c: float, *, reason: str) -> dict[str, Any]:
        reading = self.thermometry.record_reading(sensor_id, raw_c)
        return {"reading": reading.as_dict(), "reason": str(reason)}

    def record_flow(self, raw_lph: float) -> dict[str, Any]:
        reading = self.flowmeter.record(raw_lph)
        return reading.as_dict()

    def recalibrate_temperature(
        self,
        sensor_id: str,
        gain: float,
        offset: float = 0.0,
        *,
        reason: str,
    ) -> dict[str, Any]:
        sensor = self.thermometry.calibrate(sensor_id, gain, offset, reason=reason)
        self.warranties.expire_stale()
        self._count("instruments.calibrate")
        return sensor.as_dict()

    def remap_sensor(self, sensor_id: str, position: str, *, reason: str) -> dict[str, Any]:
        sensor = self.thermometry.remap(sensor_id, position, reason=reason)
        self.warranties.expire_stale()
        return sensor.as_dict()

    def recalibrate_flow(self, gain: float, offset: float = 0.0, *, reason: str) -> dict[str, Any]:
        revision = self.flowmeter.calibrate(gain, offset, reason=reason)
        self.warranties.expire_stale()
        self._count("instruments.calibrate")
        return revision.as_dict()

    def confirm_flow_baseline(self, *, reason: str) -> dict[str, Any]:
        baseline = self.warranties.record_baseline(
            "flow-calibration",
            {
                "gain": self.flowmeter.gain,
                "offset": self.flowmeter.offset,
                "nominal_lph": self.config.flow.nominal_litres_per_hour,
            },
            reason=reason,
        )
        self._count("baseline.record")
        return baseline.as_dict()

    def capture_snapshot(self, name: str, *, reason: str, ttl_seconds: float | None = None) -> dict[str, Any]:
        snapshot = self.warranties.capture_snapshot(
            name,
            "sensors",
            {"state": self.state_digest(), "stage": self.stages.current().value},
            ttl_seconds=ttl_seconds,
            reason=reason,
        )
        self._count("snapshot.capture")
        return snapshot.as_dict()

    def require_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        return self.warranties.require_snapshot(snapshot_id, scope="sensors").as_dict()

    # -- decisions ---------------------------------------------------------

    def evaluate_window(
        self,
        sensor_id: str,
        count: int,
        *,
        reason: str,
        kind: str = "sterilization-window",
    ) -> dict[str, Any]:
        readings = self.thermometry.series(str(sensor_id)).window(count)
        outcome = self.window.evaluate(str(sensor_id), readings)
        generation = self.generations.generation("sensors")
        payload = outcome.as_dict()
        self.decisions.record(
            kind,
            str(sensor_id),
            outcome.verdict,
            detail="; ".join(outcome.reasons),
            batch_id=self._active_batch_id(),
            generation=generation,
        )
        self.timeline.record(kind, payload, generation=generation)
        self._count("decision.evaluate")
        return payload

    def record_state(self, *, reason: str) -> dict[str, Any]:
        entry = self.timeline.record(
            "state",
            self.state_digest(),
            generation=self.generations.generation("sensors"),
        )
        self.audit.record("state-record", "line", str(reason), cause=None)
        return entry.as_dict()

    def state_as_of(self, revision: int) -> dict[str, Any]:
        entry = self.timeline.as_of(revision)
        if entry is None:
            raise NotFoundError("no state was published at or before that revision", revision=int(revision))
        return entry.as_dict()

    def decision_history(
        self,
        *,
        kind: str | None = None,
        verdict: str | None = None,
        batch_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.decisions.query(kind=kind, verdict=verdict, batch_id=batch_id, limit=limit)

    def _active_batch_id(self) -> str | None:
        active = self.batches.active()
        return None if active is None else active.batch_id

    # -- alarms ------------------------------------------------------------

    def raise_alarm(self, code: str, *, severity: str, message: str, target: str = "line", reason: str = "") -> dict[str, Any]:
        alarm = self.alarms.raise_alarm(code, severity=severity, message=message, target=target, reason=reason)
        self._count("alarm.raise")
        return alarm

    def clear_alarm(self, code: str, *, reason: str) -> dict[str, Any]:
        alarm = self.alarms.clear(code, reason=reason)
        self._count("alarm.clear")
        return alarm

    # -- reporting ---------------------------------------------------------

    def state_digest(self) -> dict[str, Any]:
        return {
            "stage": self.stages.current().value,
            "batch": self._active_batch_id(),
            "watermark": self.events.watermark(),
            "balance_litres": self.balance.level_litres(),
            "aseptic_litres": self.aseptic.volume_litres(),
            "sterilization_running": self.uht.is_running(),
            "cooling_running": self.cool.is_running(),
            "cleaning_running": self.cip.is_running(),
            "generation": self.generations.generation("sensors"),
        }

    def health(self) -> dict[str, Any]:
        self.metrics.gauge("records.watermark", self.events.watermark())
        self.metrics.gauge("alarms.active", self.alarms.counts()["active"])
        stream = self.events.state()
        return {
            "status": "ok",
            "line_code": self.config.line_code,
            "stage": self.stages.current().value,
            "records": stream.as_dict(),
            "audit_entries": self.audit.size(),
            "audit_valid": self.audit.verify()["valid"],
            "alarms": self.alarms.counts(),
            "latches": self.latches.state(),
            "active_latches": self.latches.active(),
            "gates": self.gates.state(),
            "generations": self.generations.as_dict(),
            "warranty_states": self.warranties.state_counts(),
            "decisions": self.decisions.counts(),
            "metrics": self.metrics.snapshot(),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "config": envelope_report(self.config),
            "stage": self.stages.snapshot(),
            "batch": self.batches.snapshot(),
            "intake": self.intake.snapshot(),
            "balance": self.balance.snapshot(),
            "preheat": self.preheat.snapshot(),
            "uht": self.uht.snapshot(),
            "hold": self.hold.snapshot(),
            "cool": self.cool.snapshot(),
            "aseptic": self.aseptic.snapshot(),
            "cleaning": self.cip.snapshot(),
            "gates": self.gates.inventory(),
            "latches": self.latches.inventory(),
            "records": self.events.state().as_dict(),
            "generations": self.generations.as_dict(),
            "warranties": self.warranties.state_counts(),
            "decisions": self.decisions.counts(),
            "timeline": self.timeline.snapshot(),
        }

    def precheck(self, action: str) -> dict[str, Any]:
        """Read-only preview of the permits an action would need."""

        required_gates = ACTION_GATES.get(action, ())
        required_latches = ACTION_LATCHES.get(action, ())
        blockers: list[dict[str, Any]] = [
            {"kind": "gate", "name": name, "state": self.gates.get(name).state}
            for name in required_gates
            if not self.gates.is_open(name)
        ]
        blockers.extend(
            {"kind": "latch", "name": name, "active": True}
            for name in required_latches
            if self.latches.is_active(name)
        )
        return {
            "action": str(action),
            "permitted": not blockers,
            "stage": self.stages.current().value,
            "required_gates": list(required_gates),
            "required_latches": list(required_latches),
            "blockers": blockers,
        }

    def advance_time(self, seconds: float) -> dict[str, Any]:
        self.clock.advance_by(float(seconds))
        invalidated = self.warranties.expire_stale()
        return {"clock": self.clock.timestamp(), "invalidated": invalidated}

    def persist_all(self) -> dict[str, Any]:
        for component in (
            self.stages,
            self.gates,
            self.latches,
            self.intake,
            self.balance,
            self.preheat,
            self.uht,
            self.hold,
            self.cool,
            self.aseptic,
            self.cip,
            self.batches,
            self.timeline,
            self.decisions,
            self.warranties,
            self.generations,
            self.thermometry,
            self.flowmeter,
        ):
            component.persist()
        return {
            "documents": self.store.document_names(),
            "journals": self.store.journal_names(),
            "watermark": self.events.watermark(),
        }

    def counters(self) -> Mapping[str, float]:
        return self.metrics.snapshot()["counters"]

    def temperatures(self, sensor_ids: Sequence[str] | None = None) -> dict[str, Any]:
        identifiers = list(sensor_ids) if sensor_ids else [sensor.sensor_id for sensor in self.thermometry.sensors()]
        return {
            identifier: {
                "position": self.thermometry.position_of(identifier),
                "latest": None
                if self.thermometry.latest(identifier) is None
                else self.thermometry.latest(identifier).value,
                "statistics": self.thermometry.series(identifier).statistics(),
            }
            for identifier in identifiers
        }


__all__ = ["ACTION_GATES", "ACTION_LATCHES", "LineControl"]
