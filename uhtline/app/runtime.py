"""Composition root that wires every section into one durable runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..alarms.board import AlarmBoard
from ..aseptic.tank import AsepticTank
from ..balance.tank import BalanceTank
from ..batches.decision import DecisionLog, StateTimeline
from ..batches.registry import BatchRegistry
from ..cip.cycle import CipCycle
from ..cool.cooler import Cooler
from ..core.clock import Clock, ManualClock
from ..core.config import ControlConfig, default_config
from ..errors import NotFoundError
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
from ..stages.machine import StageMachine
from ..telemetry.metrics import MetricsRegistry
from ..uht.section import UhtSection
from ..versioning.generations import GenerationRegistry
from ..versioning.warranties import WarrantyBook
from .control import LineControl

EVENT_STREAM = "line-events"

DEFAULT_SENSORS: tuple[tuple[str, str], ...] = (
    ("TS-PREHEAT", "preheat-outlet"),
    ("TS-STERILE", "sterilization-outlet"),
    ("TS-HOLD", "hold-outlet"),
    ("TS-COOL", "cooler-outlet"),
)

DEFAULT_GATES: tuple[tuple[str, str], ...] = (
    (gate_names.TEMPERATURE_DURABLE, "preheat temperature written to durable storage"),
    (gate_names.STERILIZATION_CONFIRMED, "sterilization confirmed for the current generation"),
    (gate_names.STERILIZATION_STOPPED, "sterilization section has stopped"),
    (gate_names.COOLING_STOPPED, "cooling section has stopped"),
    (gate_names.CIP_TEMPERATURE_CONFIRMED, "cleaning temperature confirmed"),
    (gate_names.ASEPTIC_STERILE, "aseptic tank verified sterile"),
)

DEFAULT_LATCHES: tuple[tuple[str, str, str], ...] = (
    (
        latch_names.STERILIZATION_INTERLOCK,
        "sterilization section interlock",
        "section re-verified and confirmed again",
    ),
    (
        latch_names.HOLD_BYPASS,
        "hold tube bypass valve",
        "temperature back inside the window and dwell passing",
    ),
    (
        latch_names.CIP_ALARM,
        "cleaning alarm latch",
        "cleaning temperature confirmed inside the wash window",
    ),
    (
        latch_names.ASEPTIC_PRESSURE,
        "aseptic tank pressure latch",
        "overpressure back inside the sterile band",
    ),
)


@dataclass
class Runtime:
    """Every shared service used by the CLI, the console and the scenarios."""

    config: ControlConfig
    store: DurableStore
    clock: Clock
    audit: AuditLedger
    alarms: AlarmBoard
    events: RecordJournal
    generations: GenerationRegistry
    warranties: WarrantyBook
    stages: StageMachine
    gates: GateBoard
    latches: LatchBoard
    thermometry: Thermometry
    flowmeter: FlowMeter
    intake: IntakeStation
    balance: BalanceTank
    preheat: PreheatSection
    uht: UhtSection
    hold: HoldTube
    cool: Cooler
    aseptic: AsepticTank
    cip: CipCycle
    batches: BatchRegistry
    decisions: DecisionLog
    timeline: StateTimeline
    metrics: MetricsRegistry
    control: LineControl

    def persist(self) -> dict[str, Any]:
        return self.control.persist_all()

    def backup(self, destination: Path) -> dict[str, Any]:
        self.persist()
        return self.store.backup(Path(destination))

    def recovery_report(self) -> dict[str, Any]:
        documents = self.store.inventory()
        invalid = [item for item in documents if not item["valid"]]
        stream = self.events.state()
        audit = self.audit.verify()
        return {
            "data_root": str(self.store.root),
            "documents": len(documents),
            "invalid_documents": len(invalid),
            "journals": self.store.journal_names(),
            "audit_valid": audit["valid"],
            "audit_entries": audit["entries"],
            "watermark": stream.watermark,
            "pending_records": stream.pending,
            "visible_records": stream.visible,
            "stage": self.stages.current().value,
            "sensors": [sensor.sensor_id for sensor in self.thermometry.sensors()],
            "valid": not invalid and bool(audit["valid"]),
        }


def _define_equipment(gates: GateBoard, latches: LatchBoard) -> None:
    for name, description in DEFAULT_GATES:
        gates.define(name, description=description)
    for name, description, clear_condition in DEFAULT_LATCHES:
        latches.define(name, description=description, clear_condition=clear_condition)


def _publish_scopes(runtime: Runtime) -> None:
    for scope, payload in runtime.config.scopes().items():
        runtime.generations.ensure(scope, payload, reason="commissioning")
    runtime.flowmeter.ensure_published(reason="commissioning")


def _commission_sensors(runtime: Runtime) -> None:
    for sensor_id, position in DEFAULT_SENSORS:
        try:
            runtime.thermometry.sensor(sensor_id)
        except NotFoundError:
            runtime.thermometry.register_sensor(sensor_id, position, reason="commissioning")


def build_runtime(
    config: ControlConfig | None = None,
    data_dir: str | Path | None = None,
    clock: Clock | None = None,
) -> Runtime:
    """Build every section and recover the latest durable state."""

    resolved_config = (config or default_config()).validate()
    resolved_clock = clock or ManualClock()
    root = Path(data_dir or Path.cwd() / "data")
    store = DurableStore(root, resolved_clock)
    audit = AuditLedger(store, resolved_clock)
    alarms = AlarmBoard(store, resolved_clock, resolved_config, audit)
    events = RecordJournal(store, resolved_clock, EVENT_STREAM)
    generations = GenerationRegistry(store, resolved_clock)
    warranties = WarrantyBook(store, resolved_clock, generations, resolved_config.evidence)
    stages = StageMachine(store, resolved_clock, audit)
    gates = GateBoard(store, resolved_clock)
    latches = LatchBoard(store, resolved_clock)
    metrics = MetricsRegistry()
    thermometry = Thermometry(store, resolved_clock, resolved_config, generations)
    flowmeter = FlowMeter(store, resolved_clock, resolved_config, generations)
    intake = IntakeStation(store, resolved_clock, resolved_config, events, audit)
    balance = BalanceTank(store, resolved_clock, resolved_config, gates, audit)
    preheat = PreheatSection(store, resolved_clock, resolved_config, gates, thermometry, audit)
    uht = UhtSection(
        store,
        resolved_clock,
        resolved_config,
        gates,
        latches,
        warranties,
        preheat,
        events,
        audit,
    )
    hold = HoldTube(store, resolved_clock, resolved_config, latches, warranties, audit)
    cool = Cooler(store, resolved_clock, resolved_config, gates, audit)
    aseptic = AsepticTank(store, resolved_clock, resolved_config, gates, latches, warranties, events, audit)
    cip = CipCycle(store, resolved_clock, resolved_config, gates, latches, warranties, events, audit)
    batches = BatchRegistry(store, resolved_clock, audit)
    decisions = DecisionLog(store, resolved_clock)
    timeline = StateTimeline(store, resolved_clock)
    control = LineControl(
        config=resolved_config,
        store=store,
        clock=resolved_clock,
        audit=audit,
        alarms=alarms,
        events=events,
        generations=generations,
        warranties=warranties,
        stages=stages,
        gates=gates,
        latches=latches,
        thermometry=thermometry,
        flowmeter=flowmeter,
        intake=intake,
        balance=balance,
        preheat=preheat,
        uht=uht,
        hold=hold,
        cool=cool,
        aseptic=aseptic,
        cip=cip,
        batches=batches,
        decisions=decisions,
        timeline=timeline,
        metrics=metrics,
    )
    runtime = Runtime(
        config=resolved_config,
        store=store,
        clock=resolved_clock,
        audit=audit,
        alarms=alarms,
        events=events,
        generations=generations,
        warranties=warranties,
        stages=stages,
        gates=gates,
        latches=latches,
        thermometry=thermometry,
        flowmeter=flowmeter,
        intake=intake,
        balance=balance,
        preheat=preheat,
        uht=uht,
        hold=hold,
        cool=cool,
        aseptic=aseptic,
        cip=cip,
        batches=batches,
        decisions=decisions,
        timeline=timeline,
        metrics=metrics,
        control=control,
    )
    _define_equipment(gates, latches)
    _publish_scopes(runtime)
    _commission_sensors(runtime)
    runtime.persist()
    return runtime


def restore_runtime(config: ControlConfig, data_dir: str | Path, backup_dir: str | Path) -> Runtime:
    """Restore a backup image and then rebuild the runtime over the recovered files."""

    clock = ManualClock()
    store = DurableStore(Path(data_dir), clock)
    store.restore(Path(backup_dir))
    return build_runtime(config=config, data_dir=data_dir, clock=ManualClock())


__all__ = ["DEFAULT_GATES", "DEFAULT_LATCHES", "DEFAULT_SENSORS", "EVENT_STREAM", "Runtime", "build_runtime", "restore_runtime"]
