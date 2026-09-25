"""Durable state survives a restart and the serve command runs a full lifecycle."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from uhtline.app.runtime import build_runtime
from uhtline.cli import main, run
from uhtline.console.server import ConsoleServer
from uhtline.core.clock import ManualClock
from uhtline.core.config import fast_test_config

from .support import STERILE_SENSOR


class StubServer:
    """Stand-in for the console server that stops as soon as it starts."""

    instances: list["StubServer"] = []

    def __init__(self, runtime: Any, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        self.stopped = False
        StubServer.instances.append(self)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def stop(self) -> None:
        self.stopped = True


def test_timeline_decisions_and_readings_survive_a_restart(tmp_path: Path) -> None:
    first = build_runtime(fast_test_config(), tmp_path, ManualClock())
    first.control.record_temperature(STERILE_SENSOR, 136.6, reason="test")
    first.control.record_temperature(STERILE_SENSOR, 137.1, reason="test")
    first.control.record_temperature(STERILE_SENSOR, 137.3, reason="test")
    first.control.evaluate_window(STERILE_SENSOR, 3, reason="test")
    first.control.record_state(reason="test")

    second = build_runtime(fast_test_config(), tmp_path, ManualClock())
    readings = second.thermometry.history(STERILE_SENSOR)
    assert [item.value for item in readings] == [136.6, 137.1, 137.3]
    assert second.decisions.counts()["by_kind"] == {"sterilization-window": 1}
    assert second.timeline.current().payload["stage"] == "idle"
    assert second.control.decision_history(kind="sterilization-window")[0]["verdict"] == "pass"


def test_evidence_and_generation_history_survive_a_restart(tmp_path: Path) -> None:
    first = build_runtime(fast_test_config(), tmp_path, ManualClock())
    first.flowmeter.calibrate(1.05, reason="calibration")
    baseline = first.control.confirm_flow_baseline(reason="test")
    confirmation = first.warranties.issue_confirmation("sensors", "sterilization", reason="test")

    second = build_runtime(fast_test_config(), tmp_path, ManualClock())
    assert second.flowmeter.gain == 1.05
    assert second.flowmeter.calibrations()[0]["gain"] == 1.05
    assert second.warranties.require_baseline(baseline["baseline_id"], scope="flow-calibration").generation == 2
    assert second.warranties.require_confirmation(confirmation.confirmation_id, scope="sensors").subject == "sterilization"
    assert second.generations.revisions("flow-calibration")[0].payload["gain"] == 1.0


def test_serve_command_starts_and_stops_the_console(tmp_path: Path) -> None:
    StubServer.instances.clear()
    exit_code = main(
        ["--data-dir", str(tmp_path), "serve", "--host", "127.0.0.1", "--port", "0", "--quiet"],
        server_factory=StubServer,  # type: ignore[arg-type]
    )
    assert exit_code == 0
    server = StubServer.instances[-1]
    assert server.stopped is True
    assert server.runtime.store.document_names()


def test_serve_command_announces_the_health_endpoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    StubServer.instances.clear()
    main(
        ["--data-dir", str(tmp_path), "serve", "--host", "127.0.0.1", "--port", "8080"],
        server_factory=StubServer,  # type: ignore[arg-type]
    )
    out = capsys.readouterr().out
    assert "console listening on http://127.0.0.1:8080" in out
    assert "/health" in out


def test_run_wraps_the_command_line_and_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["uhtline", "--data-dir", str(tmp_path), "stage"])
    with pytest.raises(SystemExit) as exit_code:
        run()
    assert exit_code.value.code == 0


def test_run_reports_a_service_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["uhtline", "--data-dir", str(tmp_path), "void", "LNE-99999"])
    with pytest.raises(SystemExit) as exit_code:
        run()
    assert exit_code.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"] == "not-found"
