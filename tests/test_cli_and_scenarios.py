"""Command line surface and the deterministic scenario suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uhtline.app.runtime import build_runtime
from uhtline.cli import main
from uhtline.core.clock import ManualClock
from uhtline.core.config import fast_test_config
from uhtline.scenario import run_scenario, scenario_names

from .support import manual_runtime, sterilizing_runtime


def test_scenario_names_cover_the_four_contracts() -> None:
    assert scenario_names() == [
        "cleaning-latch",
        "hold-latch",
        "recalibration-invalidates",
        "restart-recovery",
        "shutdown-order",
        "sterile-run",
    ]


def test_sterile_run_completes_every_step(tmp_path: Path) -> None:
    result = run_scenario(manual_runtime(tmp_path), "sterile-run")
    assert all(step["ok"] for step in result["steps"])
    assert result["stage"] == "complete"


def test_restart_recovery_replays_committed_records_only(tmp_path: Path) -> None:
    result = run_scenario(manual_runtime(tmp_path), "restart-recovery")
    assert result["visible_after_restart"] == ["intake"]
    assert result["pending_after_restart"] == ["intake"]
    assert result["before_restart"]["watermark"] == 1
    assert result["after_restart"]["watermark"] == 1


def test_recalibration_invalidates_the_previous_confirmation(tmp_path: Path) -> None:
    result = run_scenario(manual_runtime(tmp_path), "recalibration-invalidates")
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["sterilize-with-stale-confirmation"]["error"] == "stale-warranty"
    assert steps["sterilize-with-fresh-confirmation"]["ok"] is True
    assert result["warranties"]["superseded"] >= 1


def test_shutdown_order_scenario_reports_the_blocked_step(tmp_path: Path) -> None:
    result = run_scenario(manual_runtime(tmp_path), "shutdown-order")
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["cooling-before-sterilization"]["error"] == "gate-closed"
    assert steps["shutdown"]["ok"] is True


def test_cleaning_latch_scenario_clears_only_with_an_in_spec_temperature(tmp_path: Path) -> None:
    result = run_scenario(manual_runtime(tmp_path), "cleaning-latch")
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["reset-with-cold-water"]["error"] == "latch-active"
    assert steps["reset-with-hot-water"]["ok"] is True
    assert result["stage"] == "idle"


def test_hold_latch_scenario_recovers_the_bypass(tmp_path: Path) -> None:
    result = run_scenario(manual_runtime(tmp_path), "hold-latch")
    steps = {step["step"]: step for step in result["steps"]}
    assert steps["short-dwell"]["dwell"]["verdict"] == "short"
    assert steps["recover-below-window"]["error"] == "latch-active"
    assert steps["recover-inside-window"]["ok"] is True
    assert result["bypass_active"] is False


def test_unknown_scenario_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        run_scenario(manual_runtime(tmp_path), "no-such-scenario")


def test_cli_status_prints_the_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data-dir", str(tmp_path), "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"]["stage"] == "idle"


def test_cli_health_and_recovery_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data-dir", str(tmp_path), "status", "--health"]) == 0
    health = json.loads(capsys.readouterr().out)
    assert health["status"] == "ok"
    assert health["recovery"]["valid"] is True
    assert main(["--data-dir", str(tmp_path), "status", "--recovery"]) == 0
    recovery = json.loads(capsys.readouterr().out)
    assert recovery["stage"] == "idle"


def test_cli_records_reports_visible_and_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime = build_runtime(fast_test_config(), tmp_path, ManualClock())
    runtime.control.start_intake(reason="test")
    runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test")
    runtime.control.commit_records()
    assert main(["--data-dir", str(tmp_path), "records", "--limit", "5"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["kind"] for item in payload["visible"]] == ["intake"]
    assert payload["state"]["watermark"] == 1


def test_cli_void_tombstones_a_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime = build_runtime(fast_test_config(), tmp_path, ManualClock())
    runtime.control.start_intake(reason="test")
    result = runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test")
    runtime.control.commit_records()
    record_id = result["receipt"]["record_id"]
    assert main(["--data-dir", str(tmp_path), "void", record_id, "--reason", "test rollback"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "tombstone"
    assert payload["payload"]["target"] == record_id


def test_cli_precheck_and_config_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data-dir", str(tmp_path), "precheck", "sterilization-ramp"]) == 0
    precheck = json.loads(capsys.readouterr().out)
    assert precheck["permitted"] is False
    assert main(["--data-dir", str(tmp_path), "config", "--compare-default"]) == 0
    config = json.loads(capsys.readouterr().out)
    assert config["differences"] == []
    assert config["scopes"]["temperature"]["sterilization_target_c"] == 137.0


def test_cli_decisions_and_alarms_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime = sterilizing_runtime(tmp_path)
    runtime.decisions.record("dwell", "bsl-1", "short")
    runtime.control.raise_alarm("TEMP-DEV", severity="warning", message="deviation")
    assert main(["--data-dir", str(tmp_path), "decisions", "--verdict", "short"]) == 0
    decisions = json.loads(capsys.readouterr().out)
    assert decisions["decisions"][0]["subject"] == "bsl-1"
    assert main(["--data-dir", str(tmp_path), "alarms"]) == 0
    alarms = json.loads(capsys.readouterr().out)
    assert alarms["counts"]["active"] == 1


def test_cli_step_advances_the_deterministic_clock(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.warranties.issue_confirmation("sensors", "sterilization", ttl_seconds=5.0, reason="test")
    assert main(["--data-dir", str(tmp_path), "step", "--seconds", "10"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["invalidated"] == 1
    assert payload["clock"].startswith("2026-01-01T00:00:10")


def test_cli_backup_and_restore_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime = build_runtime(fast_test_config(), tmp_path / "live", ManualClock())
    runtime.preheat.persist_temperature("TS-PREHEAT", 87.5, reason="test")
    backup_dir = tmp_path / "image"
    assert main(["--data-dir", str(tmp_path / "live"), "backup", "--destination", str(backup_dir)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["files"]
    assert main(["--data-dir", str(tmp_path / "restored"), "restore", "--source", str(backup_dir)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["documents"] >= 1
    assert report["valid"] is True
    assert report["image"] == str(backup_dir)


def test_cli_scenario_list_and_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data-dir", str(tmp_path), "scenario", "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["scenarios"] == scenario_names()
    assert main(["--data-dir", str(tmp_path), "scenario", "sterile-run", "--fast"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "complete"


def test_cli_batches_sensors_and_generations_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.open_batch("B-0001", "milk", reason="test")
    runtime.control.record_temperature("TS-STERILE", 137.0, reason="test")
    assert main(["--data-dir", str(tmp_path), "batches"]) == 0
    batches = json.loads(capsys.readouterr().out)
    assert batches["active"]["batch_id"] == "B-0001"
    assert main(["--data-dir", str(tmp_path), "sensors"]) == 0
    sensors = json.loads(capsys.readouterr().out)
    assert sensors["sensors"]["TS-STERILE"]["latest"] == 137.0
    assert main(["--data-dir", str(tmp_path), "generations"]) == 0
    generations = json.loads(capsys.readouterr().out)
    assert generations["scopes"]["sensors"]["generation"] >= 1
