"""Shared deterministic fixtures for the line tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from uhtline.app.runtime import build_runtime
from uhtline.app.runtime import Runtime
from uhtline.core.clock import ManualClock
from uhtline.core.config import ControlConfig, fast_test_config
from uhtline.console.server import ConsoleServer
from uhtline.persistence.store import DurableStore

PREHEAT_SENSOR = "TS-PREHEAT"
STERILE_SENSOR = "TS-STERILE"
HOLD_SENSOR = "TS-HOLD"

DEFAULT_BATCH = "B-0001"


def manual_runtime(tmp_path: Path, config: ControlConfig | None = None) -> Runtime:
    return build_runtime(config or fast_test_config(), tmp_path, ManualClock())


def manual_store(tmp_path: Path) -> tuple[DurableStore, ManualClock]:
    clock = ManualClock()
    return DurableStore(tmp_path, clock), clock


def line_ready(tmp_path: Path, batch_id: str = DEFAULT_BATCH) -> Runtime:
    """Advance the line to the sterilization stage with a durable preheat value."""

    runtime = manual_runtime(tmp_path)
    control = runtime.control
    control.open_batch(batch_id, "milk", reason="test setup")
    control.start_intake(reason="test setup")
    control.receive(700.0, 6.0, batch_id=batch_id, reason="test setup")
    control.charge_balance(500.0, reason="test setup")
    control.persist_preheat_temperature(PREHEAT_SENSOR, 87.5, reason="test setup")
    return runtime


def sterilizing_runtime(tmp_path: Path, batch_id: str = DEFAULT_BATCH) -> Runtime:
    runtime = line_ready(tmp_path, batch_id)
    runtime.control.start_sterilization_ramp(137.0, reason="test setup")
    return runtime


def confirmed_runtime(tmp_path: Path, batch_id: str = DEFAULT_BATCH) -> Runtime:
    runtime = sterilizing_runtime(tmp_path, batch_id)
    runtime.control.confirm_sterilization(reason="test setup")
    return runtime


def sterilized_aseptic_runtime(tmp_path: Path, batch_id: str = DEFAULT_BATCH) -> Runtime:
    runtime = confirmed_runtime(tmp_path, batch_id)
    runtime.control.sterilize_aseptic_tank(
        confirmation_id=str(runtime.uht.confirmation_id),
        reason="test setup",
    )
    return runtime


def http_request(server: ConsoleServer, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
    """Send one JSON request to a live console server and decode the reply."""

    url = f"http://{server.host}:{server.port}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    prepared = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(prepared, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
