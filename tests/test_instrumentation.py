"""Temperature and flow instrumentation lineage."""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.errors import NotFoundError, StaleGenerationError, ValidationError

from .support import STERILE_SENSOR, manual_runtime


def test_registering_the_same_sensor_twice_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(ValidationError):
        runtime.thermometry.register_sensor(STERILE_SENSOR, "duplicate")


def test_an_unknown_sensor_is_reported(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(NotFoundError):
        runtime.thermometry.sensor("TS-MISSING")


def test_calibration_is_applied_to_new_readings(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    assert runtime.thermometry.convert(STERILE_SENSOR, 100.0) == 100.0
    runtime.thermometry.calibrate(STERILE_SENSOR, 1.1, 0.5, reason="calibration")
    assert runtime.thermometry.convert(STERILE_SENSOR, 100.0) == 110.5


def test_conversion_uses_the_calibration_of_the_requested_generation(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    first = runtime.thermometry.sensor(STERILE_SENSOR).generation
    runtime.thermometry.calibrate(STERILE_SENSOR, 1.2, 0.0, reason="calibration")
    current = runtime.thermometry.sensor(STERILE_SENSOR).generation
    assert runtime.thermometry.convert(STERILE_SENSOR, 100.0, generation=first) == 100.0
    assert runtime.thermometry.convert(STERILE_SENSOR, 100.0, generation=current) == 120.0


def test_conversion_before_the_first_calibration_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(StaleGenerationError):
        runtime.thermometry.convert(STERILE_SENSOR, 100.0, generation=0)


def test_the_sensor_map_follows_the_current_position(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    previous = runtime.thermometry.sensor(STERILE_SENSOR).generation
    runtime.thermometry.remap(STERILE_SENSOR, "sterilization-inlet", reason="replacement")
    assert runtime.thermometry.position_of(STERILE_SENSOR) == "sterilization-inlet"
    assert runtime.thermometry.map_at(previous)[STERILE_SENSOR] == "sterilization-outlet"
    assert runtime.thermometry.current_map()[STERILE_SENSOR] == "sterilization-inlet"


def test_reading_history_is_bounded_by_the_series_limit(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    series = runtime.thermometry.series(STERILE_SENSOR)
    for index in range(series.limit + 10):
        series.append(137.0 + index * 0.01, unit="degC", generation=1)
    assert len(series.all()) == series.limit
    assert len(series.window(5)) == 5
    assert series.latest().value == pytest.approx(137.0 + (series.limit + 9) * 0.01)


def test_reading_statistics_summarise_the_series(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.record_temperature(STERILE_SENSOR, 136.0, reason="test")
    runtime.control.record_temperature(STERILE_SENSOR, 138.0, reason="test")
    statistics = runtime.thermometry.series(STERILE_SENSOR).statistics()
    assert statistics == {
        "channel": STERILE_SENSOR,
        "count": 2,
        "minimum": 136.0,
        "maximum": 138.0,
        "mean": 137.0,
        "latest": 138.0,
    }


def test_measurement_applies_the_calibrated_gain(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.flowmeter.calibrate(1.05, reason="calibration")
    reading = runtime.flowmeter.measure(10000.0)
    assert reading.gain == 1.05
    assert reading.litres_per_hour == 10500.0
    assert reading.generation == runtime.generations.generation("flow-calibration")


def test_flow_series_records_the_corrected_rate(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.flowmeter.calibrate(1.1, reason="calibration")
    reading = runtime.control.record_flow(12000.0)
    assert reading["value"] == pytest.approx(13200.0)
    assert runtime.flowmeter.series().latest().value == pytest.approx(13200.0)


def test_flow_envelope_check_flags_a_rate_outside_the_window(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    assert runtime.flowmeter.within_envelope(12000.0) is True
    assert runtime.flowmeter.within_envelope(400.0) is False


def test_flow_calibration_history_records_every_change(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.flowmeter.calibrate(1.05, reason="first")
    runtime.flowmeter.calibrate(1.08, 0.5, reason="second")
    history = runtime.flowmeter.calibrations()
    assert [item["gain"] for item in history] == [1.05, 1.08]
    assert history[-1]["offset"] == 0.5
    assert runtime.flowmeter.gain == 1.08


def test_temperature_history_records_every_reading(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.record_temperature(STERILE_SENSOR, 136.0, reason="test")
    runtime.control.record_temperature(STERILE_SENSOR, 137.0, reason="test")
    history = runtime.thermometry.history(STERILE_SENSOR)
    assert [item.value for item in history] == [136.0, 137.0]
    assert runtime.thermometry.latest(STERILE_SENSOR).value == 137.0
