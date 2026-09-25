"""Contract: every command value is bounded by the configured envelope."""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.errors import RangeError, ValidationError

from .support import (
    PREHEAT_SENSOR,
    line_ready,
    manual_runtime,
    sterilized_aseptic_runtime,
)


def test_intake_screening_reports_reasons_without_changing_state(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    report = runtime.intake.screen(40.0, 4.0)
    assert report["accepted"] is False
    assert report["window_litres"][0] == 200.0
    assert runtime.intake.total_litres() == 0.0


def test_intake_volume_above_the_window_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_intake(reason="test")
    with pytest.raises(RangeError) as failure:
        runtime.control.receive(5000.0, 6.0, batch_id="B-0001", reason="test")
    assert failure.value.details["maximum"] == 1200.0


def test_intake_temperature_outside_the_cold_chain_window_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_intake(reason="test")
    with pytest.raises(RangeError) as failure:
        runtime.control.receive(700.0, 40.0, batch_id="B-0001", reason="test")
    assert failure.value.details["field"] == "temperature_c"


def test_repeating_an_intake_receipt_key_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_intake(reason="test")
    runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test", key="receipt-1")
    with pytest.raises(Exception) as failure:
        runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test", key="receipt-1")
    assert "duplicate" in str(failure.value.code)


def test_balance_charge_that_would_overflow_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.balance.charge(900.0, reason="test")
    with pytest.raises(RangeError) as failure:
        runtime.balance.charge(900.0, reason="test")
    assert failure.value.details["capacity_litres"] == 1500.0


def test_balance_drain_beyond_the_current_level_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.balance.charge(600.0, reason="test")
    with pytest.raises(RangeError):
        runtime.balance.drain(700.0, reason="test")


def test_preheat_target_outside_the_tolerance_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(RangeError):
        runtime.preheat.set_target(95.0, reason="operator")


def test_sterilization_target_outside_the_window_is_rejected(tmp_path: Path) -> None:
    runtime = line_ready(tmp_path)
    with pytest.raises(RangeError):
        runtime.control.start_sterilization_ramp(160.0, reason="operator")


def test_aseptic_fill_that_would_overflow_the_tank_is_rejected(tmp_path: Path) -> None:
    runtime = sterilized_aseptic_runtime(tmp_path)
    runtime.control.start_hold(reason="test")
    runtime.control.start_cooling(reason="test")
    runtime.aseptic.fill(1000.0, reason="test")
    runtime.aseptic.fill(1000.0, reason="test")
    with pytest.raises(RangeError):
        runtime.aseptic.fill(100.0, reason="operator")


def test_cleaning_temperature_outside_the_wash_window_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_cleaning(reason="test")
    with pytest.raises(RangeError):
        runtime.control.confirm_cleaning_temperature(50.0, reason="operator")


def test_cleaning_cycle_shorter_than_the_minimum_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_cleaning(reason="test")
    with pytest.raises(RangeError) as failure:
        runtime.cip.cycle_complete(elapsed_seconds=10.0, reason="operator")
    assert failure.value.details["minimum"] == 120.0


def test_flow_gain_outside_the_permitted_band_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(ValidationError) as failure:
        runtime.control.recalibrate_flow(2.0, reason="calibration")
    assert failure.value.details["maximum"] == 1.25


def test_sensor_calibration_gain_must_be_positive(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(ValidationError):
        runtime.control.recalibrate_temperature(PREHEAT_SENSOR, 0.0, reason="calibration")


def test_hold_dwell_flags_a_flow_outside_the_envelope(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    baseline = runtime.control.confirm_flow_baseline(reason="test")
    outcome = runtime.control.evaluate_dwell(
        baseline_id=baseline["baseline_id"],
        raw_lph=30000.0,
        reason="test",
    )
    assert outcome["verdict"] == "flow-out-of-range"
    assert runtime.control.hold.bypass_active() is True


def test_cooler_outlet_outside_the_permitted_range_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(RangeError):
        runtime.cool.set_outlet(60.0, reason="operator")
