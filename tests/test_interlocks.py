"""Contract: staged sequencing, permit gates and latched clear conditions."""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.errors import GateClosedError, LatchActiveError, StageOrderError, StaleWarrantyError, StateError
from uhtline.stages import gates as gate_names
from uhtline.stages import latches as latch_names
from uhtline.stages.machine import Stage

from .support import (
    PREHEAT_SENSOR,
    STERILE_SENSOR,
    confirmed_runtime,
    line_ready,
    manual_runtime,
    sterilized_aseptic_runtime,
    sterilizing_runtime,
)


def test_stage_sequence_rejects_a_skipped_stage(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(StageOrderError) as failure:
        runtime.stages.enter(Stage.PREHEAT, reason="operator")
    assert failure.value.details["from_stage"] == "idle"
    assert failure.value.details["to_stage"] == "preheat"


def test_stage_sequence_rejects_a_reversed_transition(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.stages.enter(Stage.INTAKE, reason="operator")
    runtime.stages.enter(Stage.BALANCE, reason="operator")
    with pytest.raises(StageOrderError):
        runtime.stages.enter(Stage.INTAKE, reason="operator")


def test_entering_the_stage_that_is_already_active_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.stages.enter(Stage.INTAKE, reason="operator")
    with pytest.raises(StateError):
        runtime.stages.enter(Stage.INTAKE, reason="operator")


def test_advancing_past_a_complete_run_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    for stage in (Stage.INTAKE, Stage.BALANCE, Stage.PREHEAT, Stage.STERILIZE, Stage.HOLD, Stage.COOL, Stage.ASEPTIC_FILL, Stage.COMPLETE):
        runtime.stages.enter(stage, reason="operator")
    with pytest.raises(StateError):
        runtime.stages.advance(reason="operator")


def test_cleaning_only_starts_from_a_permitted_stage(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    with pytest.raises(StageOrderError):
        runtime.stages.start_cleaning(reason="operator")


def test_cleaning_returns_the_line_to_idle_when_advanced(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.stages.start_cleaning(reason="operator")
    assert runtime.stages.current() is Stage.CIP
    runtime.stages.advance(reason="operator")
    assert runtime.stages.current() is Stage.IDLE


def test_reset_returns_a_running_line_to_idle(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    runtime.stages.reset(reason="operator")
    assert runtime.stages.current() is Stage.IDLE
    assert runtime.stages.history()[-1].from_stage == "sterilize"


def test_ramp_is_refused_until_the_preheat_temperature_is_durable(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    control = runtime.control
    control.open_batch("B-0001", "milk", reason="test")
    control.start_intake(reason="test")
    control.receive(700.0, 6.0, batch_id="B-0001", reason="test")
    control.charge_balance(500.0, reason="test")
    with pytest.raises(GateClosedError) as failure:
        control.start_sterilization_ramp(137.0, reason="operator")
    assert failure.value.details["gate"] == gate_names.TEMPERATURE_DURABLE


def test_persisting_an_out_of_window_temperature_keeps_the_ramp_permit_closed(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    control = runtime.control
    control.open_batch("B-0001", "milk", reason="test")
    control.start_intake(reason="test")
    control.receive(700.0, 6.0, batch_id="B-0001", reason="test")
    control.charge_balance(500.0, reason="test")
    record = control.persist_preheat_temperature(PREHEAT_SENSOR, 70.0, reason="operator")
    assert record["in_spec"] is False
    with pytest.raises(GateClosedError):
        control.start_sterilization_ramp(137.0, reason="operator")


def test_ramp_is_refused_while_the_sterilization_interlock_is_set(tmp_path: Path) -> None:
    runtime = line_ready(tmp_path)
    runtime.control.uht.trip(reason="temperature deviation")
    with pytest.raises(LatchActiveError) as failure:
        runtime.control.start_sterilization_ramp(137.0, reason="operator")
    assert failure.value.details["latch"] == latch_names.STERILIZATION_INTERLOCK


def test_interlock_latch_requires_a_confirmed_recovery(tmp_path: Path) -> None:
    runtime = line_ready(tmp_path)
    runtime.control.uht.trip(reason="temperature deviation")
    with pytest.raises(LatchActiveError):
        runtime.control.uht.recover(reason="operator", confirmed=False)
    latch = runtime.control.uht.recover(reason="operator", confirmed=True)
    assert latch["active"] is False
    assert runtime.control.start_sterilization_ramp(137.0, reason="operator")["target_c"] == 137.0


def test_aseptic_fill_is_refused_without_the_sterilization_confirmation(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    runtime.control.start_hold(reason="test")
    runtime.control.start_cooling(reason="test")
    with pytest.raises(GateClosedError) as failure:
        runtime.control.fill_aseptic_tank(500.0, reason="operator")
    assert failure.value.details["gate"] == gate_names.ASEPTIC_STERILE


def test_aseptic_fill_is_refused_when_the_confirmation_became_stale(tmp_path: Path) -> None:
    runtime = confirmed_runtime(tmp_path)
    confirmation_id = str(runtime.uht.confirmation_id)
    runtime.control.recalibrate_temperature(STERILE_SENSOR, 1.04, 0.0, reason="recalibration")
    with pytest.raises(StaleWarrantyError):
        runtime.control.sterilize_aseptic_tank(confirmation_id=confirmation_id, reason="operator")


def test_aseptic_tank_rejects_a_confirmation_the_section_never_issued(tmp_path: Path) -> None:
    runtime = confirmed_runtime(tmp_path)
    with pytest.raises(StateError) as failure:
        runtime.control.sterilize_aseptic_tank(confirmation_id="cfm-sensors-99999", reason="operator")
    assert failure.value.details["supplied"] == "cfm-sensors-99999"


def test_cleaning_pump_is_refused_before_the_temperature_is_confirmed(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_cleaning(reason="operator")
    with pytest.raises(GateClosedError) as failure:
        runtime.control.start_cleaning_pump(6000.0, reason="operator")
    assert failure.value.details["gate"] == gate_names.CIP_TEMPERATURE_CONFIRMED


def test_cooling_stop_is_refused_before_the_sterilization_stops(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    runtime.control.start_hold(reason="test")
    runtime.control.start_cooling(reason="test")
    with pytest.raises(GateClosedError) as failure:
        runtime.control.stop_cooling(reason="operator")
    assert failure.value.details["gate"] == gate_names.STERILIZATION_STOPPED


def test_balance_stop_is_refused_before_the_cooling_stops(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    with pytest.raises(GateClosedError) as failure:
        runtime.control.stop_balance(reason="operator")
    assert failure.value.details["gate"] == gate_names.COOLING_STOPPED


def test_shutdown_stops_every_section_in_the_permitted_order(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    runtime.control.start_hold(reason="test")
    runtime.control.start_cooling(reason="test")
    result = runtime.control.shutdown(reason="operator shutdown")
    actions = [step["action"] for step in result["steps"]]
    assert actions == ["stop", "stop", "stop"]
    assert runtime.control.uht.is_running() is False
    assert runtime.control.cool.is_running() is False
    assert runtime.control.balance.is_stopped() is True


def test_hold_bypass_latch_stays_set_until_temperature_and_dwell_recover(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    baseline = runtime.control.confirm_flow_baseline(reason="test")
    outcome = runtime.control.evaluate_dwell(baseline_id=baseline["baseline_id"], raw_lph=17500.0, reason="test")
    assert outcome["verdict"] == "short"
    assert runtime.control.hold.bypass_active() is True
    with pytest.raises(LatchActiveError):
        runtime.control.recover_hold(
            130.0,
            baseline_id=baseline["baseline_id"],
            raw_lph=12000.0,
            reason="operator",
        )
    result = runtime.control.recover_hold(
        137.0,
        baseline_id=baseline["baseline_id"],
        raw_lph=12000.0,
        reason="operator",
    )
    assert result["cleared"] is True
    assert runtime.control.hold.bypass_active() is False


def test_cleaning_alarm_latch_requires_an_in_spec_temperature_to_reset(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_cleaning(reason="test")
    runtime.control.cip.raise_alarm(reason="conductivity deviation")
    with pytest.raises(LatchActiveError):
        runtime.control.cip.reset_alarm(value_c=40.0, reason="operator")
    result = runtime.control.cip.reset_alarm(value_c=80.5, reason="operator")
    assert result["latch"]["active"] is False


def test_aseptic_pressure_latch_blocks_the_fill_until_the_band_recovers(tmp_path: Path) -> None:
    runtime = sterilized_aseptic_runtime(tmp_path)
    runtime.control.start_hold(reason="test")
    runtime.control.start_cooling(reason="test")
    runtime.control.aseptic.pressurize(20.0, reason="test")
    with pytest.raises(LatchActiveError):
        runtime.control.fill_aseptic_tank(400.0, reason="operator")
    runtime.control.aseptic.pressurize(60.0, reason="operator")
    assert runtime.control.aseptic.snapshot()["pressure_latched"] is False
    assert runtime.control.fill_aseptic_tank(400.0, reason="operator")["level_litres"] == 400.0


def test_gate_inventory_reports_every_defined_permit(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    names = {gate["name"] for gate in runtime.gates.inventory()}
    assert names == {
        gate_names.TEMPERATURE_DURABLE,
        gate_names.STERILIZATION_CONFIRMED,
        gate_names.STERILIZATION_STOPPED,
        gate_names.COOLING_STOPPED,
        gate_names.CIP_TEMPERATURE_CONFIRMED,
        gate_names.ASEPTIC_STERILE,
    }
    assert set(runtime.gates.state().values()) == {"closed"}


def test_precheck_lists_the_blockers_of_a_gated_action(tmp_path: Path) -> None:
    runtime = line_ready(tmp_path)
    report = runtime.control.precheck("sterilization-ramp")
    assert report["permitted"] is True
    runtime.control.uht.trip(reason="temperature deviation")
    blocked = runtime.control.precheck("sterilization-ramp")
    assert blocked["permitted"] is False
    assert blocked["blockers"][0]["kind"] == "latch"


def test_charging_the_balance_tank_requires_the_intake_stage(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(StateError):
        runtime.control.charge_balance(500.0, reason="operator")
