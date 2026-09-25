"""Behaviour of the individual sections and the shared alarm board."""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.app.runtime import build_runtime
from uhtline.core.clock import ManualClock
from uhtline.core.config import fast_test_config
from uhtline.errors import NotFoundError, StateError, ValidationError
from uhtline.telemetry.metrics import MetricsRegistry

from .support import STERILE_SENSOR, manual_runtime, sterilizing_runtime


def test_intake_receipt_is_staged_before_the_watermark_covers_it(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_intake(reason="test")
    result = runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test", key="receipt-1")
    assert result["receipt"]["volume_litres"] == 700.0
    assert runtime.control.visible_records() == []
    assert [item["kind"] for item in runtime.control.pending_records()] == ["intake"]
    assert runtime.intake.total_litres() == 700.0
    assert runtime.intake.require_receipt("B-0001")["record_id"] == result["receipt"]["record_id"]
    assert runtime.intake.snapshot()["recent"][0]["volume_litres"] == 700.0


def test_draining_the_balance_tank_lowers_the_level(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.balance.charge(600.0, reason="test")
    runtime.balance.drain(200.0, reason="test")
    assert runtime.balance.level_litres() == 400.0
    assert runtime.balance.is_charging() is True
    assert runtime.balance.snapshot()["history"][-1]["action"] == "drain"


def test_preheat_temperature_is_read_back_after_a_restart(tmp_path: Path) -> None:
    first = build_runtime(fast_test_config(), tmp_path, ManualClock())
    first.preheat.persist_temperature("TS-PREHEAT", 87.5, reason="test")
    second = build_runtime(fast_test_config(), tmp_path, ManualClock())
    assert second.preheat.durable_temperature()["value_c"] == 87.5
    assert second.preheat.is_durable() is True
    assert second.preheat.snapshot()["permit_open"] is True


def test_preheat_requires_a_durable_record_before_reporting_one(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    assert runtime.preheat.is_durable() is False
    with pytest.raises(StateError):
        runtime.preheat.durable_temperature()


def test_cooling_stop_opens_the_shutdown_permit(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    runtime.control.start_hold(reason="test")
    runtime.control.start_cooling(reason="test")
    runtime.control.stop_sterilization(reason="test")
    runtime.control.stop_cooling(reason="test")
    assert runtime.cool.is_running() is False
    assert runtime.cool.snapshot()["stop_permit_open"] is True
    runtime.control.stop_balance(reason="test")
    assert runtime.balance.is_stopped() is True


def test_aseptic_snapshot_reports_the_sterile_state_and_fills(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    confirmation = runtime.control.confirm_sterilization(reason="test")
    runtime.control.sterilize_aseptic_tank(confirmation_id=confirmation["confirmation_id"], reason="test")
    runtime.control.start_hold(reason="test")
    runtime.control.start_cooling(reason="test")
    runtime.control.fill_aseptic_tank(300.0, reason="test")
    snapshot = runtime.aseptic.snapshot()
    assert snapshot["sterile"] is True
    assert snapshot["volume_litres"] == 300.0
    assert snapshot["fills"] == 1


def test_cancelling_the_sterile_state_closes_the_fill_permit(tmp_path: Path) -> None:
    runtime = sterilizing_runtime(tmp_path)
    confirmation = runtime.control.confirm_sterilization(reason="test")
    runtime.control.sterilize_aseptic_tank(confirmation_id=confirmation["confirmation_id"], reason="test")
    gate = runtime.aseptic.cancel_sterile(reason="operator")
    assert gate["state"] == "closed"
    with pytest.raises(StateError):
        runtime.aseptic.cancel_sterile(reason="operator")


def test_cleaning_section_snapshot_reports_the_confirmation(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_cleaning(reason="test")
    result = runtime.control.confirm_cleaning_temperature(81.5, reason="test")
    snapshot = runtime.cip.snapshot()
    assert snapshot["temperature_c"] == 81.5
    assert snapshot["confirmation_id"] == result["confirmation"]["confirmation_id"]
    assert snapshot["alarm_active"] is False


def test_cleaning_pump_cannot_start_twice(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_cleaning(reason="test")
    runtime.control.confirm_cleaning_temperature(81.0, reason="test")
    runtime.control.start_cleaning_pump(6000.0, reason="test")
    with pytest.raises(StateError):
        runtime.control.start_cleaning_pump(6000.0, reason="test")


def test_alarm_board_ranks_and_counts_active_alarms(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.raise_alarm("TEMP-DEV", severity="warning", message="deviation", target="uht")
    runtime.control.raise_alarm("PRESSURE-LOW", severity="critical", message="low pressure", target="aseptic")
    counts = runtime.alarms.counts()
    assert counts["active"] == 2
    assert counts["highest"] == "critical"
    assert runtime.alarms.highest_severity() == "critical"
    assert [alarm["code"] for alarm in runtime.alarms.active(severity="warning")] == ["TEMP-DEV"]
    assert [alarm["code"] for alarm in runtime.alarms.active(target="aseptic")] == ["PRESSURE-LOW"]


def test_repeating_an_active_alarm_increments_its_occurrences(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.raise_alarm("TEMP-DEV", severity="warning", message="deviation")
    repeat = runtime.control.raise_alarm("TEMP-DEV", severity="warning", message="deviation")
    assert repeat["occurrences"] == 2
    assert runtime.alarms.counts()["active"] == 1


def test_alarm_board_rejects_an_unknown_severity(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(ValidationError):
        runtime.control.raise_alarm("TEMP-DEV", severity="urgent", message="deviation")


def test_clearing_an_inactive_alarm_is_reported(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(NotFoundError):
        runtime.control.clear_alarm("TEMP-DEV", reason="operator")


def test_alarm_history_records_raise_and_clear(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.raise_alarm("TEMP-DEV", severity="warning", message="deviation")
    runtime.control.clear_alarm("TEMP-DEV", reason="operator")
    history = runtime.alarms.history()
    assert [item["severity"] for item in history] == ["warning", "warning"]
    assert history[-1]["clear_reason"] == "operator"
    assert runtime.alarms.counts()["active"] == 0


def test_metrics_registry_tracks_counters_and_gauges() -> None:
    metrics = MetricsRegistry()
    metrics.increment("commands")
    metrics.increment("commands", 2)
    metrics.gauge("watermark", 4)
    snapshot = metrics.snapshot()
    assert snapshot["counters"] == {"commands": 3.0}
    assert snapshot["gauges"] == {"watermark": 4.0}
    assert metrics.count("commands") == 3.0
    assert metrics.peek("watermark") == 4.0
    metrics.reset()
    assert metrics.snapshot() == {"counters": {}, "gauges": {}}


def test_metrics_registry_rejects_an_empty_name() -> None:
    metrics = MetricsRegistry()
    with pytest.raises(ValidationError):
        metrics.increment("   ")


def test_health_envelope_reports_the_stage_and_record_stream(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.start_intake(reason="test")
    runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test")
    runtime.control.commit_records()
    health = runtime.control.health()
    assert health["status"] == "ok"
    assert health["stage"] == "intake"
    assert health["records"]["watermark"] == 1
    assert health["records"]["visible"] == 1
    assert health["audit_valid"] is True
    assert health["gates"][next(iter(health["gates"]))] == "closed"


def test_snapshot_payload_covers_every_section(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    snapshot = runtime.control.snapshot()
    assert set(snapshot) == {
        "config",
        "stage",
        "batch",
        "intake",
        "balance",
        "preheat",
        "uht",
        "hold",
        "cool",
        "aseptic",
        "cleaning",
        "gates",
        "latches",
        "records",
        "generations",
        "warranties",
        "decisions",
        "timeline",
    }


def test_sensor_view_reports_positions_and_statistics(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.record_temperature(STERILE_SENSOR, 137.0, reason="test")
    view = runtime.control.temperatures([STERILE_SENSOR])
    assert view[STERILE_SENSOR]["position"] == "sterilization-outlet"
    assert view[STERILE_SENSOR]["latest"] == 137.0
    assert view[STERILE_SENSOR]["statistics"]["count"] == 1
    assert isinstance(runtime.control.counters(), dict)


def test_batch_registry_snapshot_groups_by_product(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.open_batch("B-0001", "milk", reason="test")
    runtime.control.close_batch("B-0001", "released", reason="test")
    snapshot = runtime.batches.snapshot()
    assert snapshot["batches"] == 1
    assert snapshot["open_count"] == 0
    assert snapshot["by_product"] == {"milk": 1}
    assert runtime.batches.count() == 1
    assert runtime.batches.get("B-0001").outcome == "released"


def test_restart_keeps_the_gates_and_latches(tmp_path: Path) -> None:
    first = sterilizing_runtime(tmp_path)
    first.uht.trip(reason="temperature deviation")
    second = build_runtime(fast_test_config(), tmp_path, ManualClock())
    assert second.gates.is_open("preheat-temperature-durable") is True
    assert second.latches.is_active("sterilization-interlock") is True
    assert second.stages.current().value == "sterilize"
    assert second.audit.verify()["valid"] is True
