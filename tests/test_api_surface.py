"""Every console route is reachable and the line can be driven end to end over HTTP."""

from __future__ import annotations

from pathlib import Path

from uhtline.console.app import ConsoleApp
from uhtline.console.server import ConsoleServer
from uhtline.errors import ServiceError

from .support import http_request, manual_runtime


def test_every_get_route_answers_with_a_json_payload(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    for route in app.route_inventory():
        if route["method"] != "GET":
            continue
        path = route["pattern"].replace("{action}", "sterilization-ramp")
        status, payload = app.handle("GET", path, {}, {})
        assert status == 200
        assert isinstance(payload, dict)


def test_every_post_route_answers_or_reports_a_structured_failure(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    answered = 0
    refused = 0
    for route in app.route_inventory():
        if route["method"] != "POST":
            continue
        try:
            status, payload = app.handle("POST", route["pattern"], {}, {})
        except ServiceError as failure:
            refused += 1
            assert failure.code and failure.status >= 400
            continue
        answered += 1
        assert status == 200
        assert isinstance(payload, dict)
    assert answered >= 3
    assert refused >= 5


def test_the_line_can_be_driven_end_to_end_over_http(live_server: ConsoleServer) -> None:
    steps = [
        ("/api/batches/open", {"batch_id": "B-7000", "product": "milk"}),
        ("/api/intake/start", {}),
        ("/api/intake/receive", {"batch_id": "B-7000", "volume_litres": 700.0, "temperature_c": 6.0, "key": "http-1"}),
        ("/api/records/commit", {}),
        ("/api/balance/charge", {"volume_litres": 600.0}),
        ("/api/preheat/persist", {"sensor_id": "TS-PREHEAT", "raw_c": 87.5}),
        ("/api/uht/ramp", {"target_c": 137.0}),
    ]
    for path, body in steps:
        status, payload = http_request(live_server, "POST", path, body)
        assert status == 200, (path, payload)
    status, confirmation = http_request(live_server, "POST", "/api/uht/confirm", {})
    assert status == 200 and confirmation["confirmation_id"].startswith("cfm-sensors")
    status, _ = http_request(
        live_server,
        "POST",
        "/api/aseptic/sterilize",
        {"confirmation_id": confirmation["confirmation_id"]},
    )
    assert status == 200
    for path, body in [
        ("/api/hold/start", {}),
        ("/api/baselines", {}),
    ]:
        status, payload = http_request(live_server, "POST", path, body)
        assert status == 200, (path, payload)
    status, baseline = http_request(live_server, "POST", "/api/baselines", {})
    assert status == 200 and baseline["baseline_id"].startswith("bsl-flow-calibration")
    status, dwell = http_request(
        live_server,
        "POST",
        "/api/hold/evaluate",
        {"baseline_id": baseline["baseline_id"], "raw_lph": 12000.0},
    )
    assert status == 200 and dwell["verdict"] == "pass"
    for path, body in [
        ("/api/cool/start", {}),
        ("/api/aseptic/fill", {"volume_litres": 500.0, "key": "http-2"}),
        ("/api/records/commit", {}),
        ("/api/batches/close", {"batch_id": "B-7000", "outcome": "released"}),
    ]:
        status, payload = http_request(live_server, "POST", path, body)
        assert status == 200, (path, payload)
    status, state = http_request(live_server, "GET", "/api/state", None)
    assert status == 200
    assert state["stage"]["stage"] == "aseptic-fill"
    assert state["records"]["watermark"] == 4
    assert state["records"]["visible"] == 4


def test_records_rollback_route_discards_the_staged_tail(live_server: ConsoleServer) -> None:
    http_request(live_server, "POST", "/api/batches/open", {"batch_id": "B-7001", "product": "milk"})
    http_request(live_server, "POST", "/api/intake/start", {})
    http_request(live_server, "POST", "/api/intake/receive", {"batch_id": "B-7001", "volume_litres": 600.0, "temperature_c": 6.0})
    status, payload = http_request(live_server, "POST", "/api/records/rollback", {})
    assert status == 200
    assert payload["discarded"] == 1
    assert payload["state"]["appended"] == 0


def test_records_void_route_tombstones_a_committed_record(live_server: ConsoleServer) -> None:
    http_request(live_server, "POST", "/api/batches/open", {"batch_id": "B-7002", "product": "milk"})
    http_request(live_server, "POST", "/api/intake/start", {})
    _, receipt = http_request(
        live_server,
        "POST",
        "/api/intake/receive",
        {"batch_id": "B-7002", "volume_litres": 600.0, "temperature_c": 6.0},
    )
    http_request(live_server, "POST", "/api/records/commit", {})
    status, tombstone = http_request(
        live_server,
        "POST",
        "/api/records/void",
        {"record_id": receipt["receipt"]["record_id"], "reason": "operator rollback"},
    )
    assert status == 200
    assert tombstone["kind"] == "tombstone"
    http_request(live_server, "POST", "/api/records/commit", {})
    _, records = http_request(live_server, "GET", "/api/records", None)
    assert records["visible"] == []
    assert records["state"]["superseded"] == 1


def test_sensor_remap_route_moves_the_reading_to_a_new_position(live_server: ConsoleServer) -> None:
    status, payload = http_request(
        live_server,
        "POST",
        "/api/sensors/remap",
        {"sensor_id": "TS-STERILE", "position": "sterilization-inlet"},
    )
    assert status == 200
    assert payload["position"] == "sterilization-inlet"
    _, sensors = http_request(live_server, "GET", "/api/temperatures", None)
    assert sensors["sensors"]["TS-STERILE"]["position"] == "sterilization-inlet"


def test_calibration_route_moves_the_generation_and_invalidates_a_confirmation(live_server: ConsoleServer) -> None:
    http_request(live_server, "POST", "/api/batches/open", {"batch_id": "B-7003", "product": "milk"})
    http_request(live_server, "POST", "/api/intake/start", {})
    http_request(live_server, "POST", "/api/intake/receive", {"batch_id": "B-7003", "volume_litres": 600.0, "temperature_c": 6.0})
    http_request(live_server, "POST", "/api/balance/charge", {"volume_litres": 500.0})
    http_request(live_server, "POST", "/api/preheat/persist", {"sensor_id": "TS-PREHEAT", "raw_c": 87.5})
    http_request(live_server, "POST", "/api/uht/ramp", {"target_c": 137.0})
    _, confirmation = http_request(live_server, "POST", "/api/uht/confirm", {})
    status, _ = http_request(
        live_server,
        "POST",
        "/api/calibrate/temperature",
        {"sensor_id": "TS-STERILE", "gain": 1.02, "offset": 0.1},
    )
    assert status == 200
    status, failure = http_request(
        live_server,
        "POST",
        "/api/aseptic/sterilize",
        {"confirmation_id": confirmation["confirmation_id"]},
    )
    assert status == 409
    assert failure["error"] == "stale-warranty"
    _, warranties = http_request(live_server, "GET", "/api/warranties", None)
    assert warranties["states"]["superseded"] == 1
    assert warranties["inventory"]["confirmations"]


def test_line_reset_route_returns_the_line_to_idle(live_server: ConsoleServer) -> None:
    http_request(live_server, "POST", "/api/batches/open", {"batch_id": "B-7004", "product": "milk"})
    http_request(live_server, "POST", "/api/intake/start", {})
    status, payload = http_request(live_server, "POST", "/api/line/reset", {})
    assert status == 200
    assert payload["to_stage"] == "idle"


def test_alarm_routes_raise_and_clear_with_the_latch_free_board(live_server: ConsoleServer) -> None:
    status, raised = http_request(
        live_server,
        "POST",
        "/api/alarms/raise",
        {"code": "TEMP-DEV", "severity": "critical", "message": "deviation", "target": "uht"},
    )
    assert status == 200 and raised["severity"] == "critical"
    _, board = http_request(live_server, "GET", "/api/alarms", None)
    assert board["counts"]["highest"] == "critical"
    status, cleared = http_request(live_server, "POST", "/api/alarms/clear", {"code": "TEMP-DEV"})
    assert status == 200 and cleared["clear_reason"] == "operator"
    assert board["active"][0]["code"] == "TEMP-DEV"


def test_preheat_target_route_is_bounded_by_the_envelope(live_server: ConsoleServer) -> None:
    status, payload = http_request(live_server, "POST", "/api/preheat/target", {"target_c": 88.5})
    assert status == 200 and payload["target_c"] == 88.5
    status, failure = http_request(live_server, "POST", "/api/preheat/target", {"target_c": 99.0})
    assert status == 422 and failure["error"] == "out-of-range"


def test_timeline_route_returns_a_pinned_revision(live_server: ConsoleServer) -> None:
    http_request(live_server, "POST", "/api/state/record", {})
    http_request(live_server, "POST", "/api/intake/start", {})
    http_request(live_server, "POST", "/api/state/record", {})
    _, timeline = http_request(live_server, "GET", "/api/timeline?revision=1", None)
    assert timeline["revision"] == 1
    assert timeline["payload"]["stage"] == "idle"
    _, summary = http_request(live_server, "GET", "/api/timeline", None)
    assert summary["revisions"] == 2
    assert summary["current"]["payload"]["stage"] == "intake"


def test_logs_route_returns_the_recent_requests(live_server: ConsoleServer) -> None:
    http_request(live_server, "GET", "/health", None)
    status, payload = http_request(live_server, "GET", "/api/logs?limit=5", None)
    assert status == 200
    assert any("/health" in line for line in payload["lines"])


def test_time_advance_route_reports_the_invalidated_evidence(live_server: ConsoleServer) -> None:
    http_request(live_server, "POST", "/api/batches/open", {"batch_id": "B-7005", "product": "milk"})
    http_request(live_server, "POST", "/api/intake/start", {})
    http_request(live_server, "POST", "/api/intake/receive", {"batch_id": "B-7005", "volume_litres": 600.0, "temperature_c": 6.0})
    http_request(live_server, "POST", "/api/balance/charge", {"volume_litres": 500.0})
    http_request(live_server, "POST", "/api/preheat/persist", {"sensor_id": "TS-PREHEAT", "raw_c": 87.5})
    http_request(live_server, "POST", "/api/uht/ramp", {"target_c": 137.0})
    http_request(live_server, "POST", "/api/uht/confirm", {"ttl_seconds": 30})
    status, payload = http_request(live_server, "POST", "/api/time/advance", {"seconds": 31})
    assert status == 200
    assert payload["invalidated"] == 1


def test_config_route_reports_the_envelope_and_the_config_payload(live_server: ConsoleServer) -> None:
    status, payload = http_request(live_server, "GET", "/api/config", None)
    assert status == 200
    assert payload["config"]["temperature"]["sterilization_target_c"] == 137.0
    assert payload["envelope"]["line_code"] == "LN-01"
    assert payload["generations"]["scopes"]["sensors"]["generation"] >= 1


def test_window_evaluate_route_records_a_verdict(live_server: ConsoleServer) -> None:
    for raw in (136.6, 137.1, 137.4):
        status, _ = http_request(
            live_server,
            "POST",
            "/api/temperatures/record",
            {"sensor_id": "TS-STERILE", "raw_c": raw},
        )
        assert status == 200
    status, outcome = http_request(
        live_server,
        "POST",
        "/api/windows/evaluate",
        {"sensor_id": "TS-STERILE", "count": 3, "kind": "sterilization-window"},
    )
    assert status == 200
    assert outcome["verdict"] == "pass"
    assert outcome["accepted"] is True
    _, decisions = http_request(live_server, "GET", "/api/decisions?kind=sterilization-window", None)
    assert decisions["decisions"][-1]["verdict"] == "pass"


def test_hold_recover_and_cleaning_routes_settle_their_latches(live_server: ConsoleServer) -> None:
    _, baseline = http_request(live_server, "POST", "/api/baselines", {})
    http_request(live_server, "POST", "/api/hold/evaluate", {"baseline_id": baseline["baseline_id"], "raw_lph": 17500.0})
    status, blocked = http_request(
        live_server,
        "POST",
        "/api/hold/recover",
        {"value_c": 120.0, "baseline_id": baseline["baseline_id"], "raw_lph": 12000.0},
    )
    assert status == 423 and blocked["error"] == "latch-active"
    status, recovered = http_request(
        live_server,
        "POST",
        "/api/hold/recover",
        {"value_c": 137.0, "baseline_id": baseline["baseline_id"], "raw_lph": 12000.0},
    )
    assert status == 200 and recovered["cleared"] is True
    http_request(live_server, "POST", "/api/intake/start", {})
    status, reset = http_request(live_server, "POST", "/api/line/reset", {})
    assert status == 200 and reset["to_stage"] == "idle"
    status, entered = http_request(live_server, "POST", "/api/cip/enter", {})
    assert status == 200 and entered["to_stage"] == "cip"
    status, confirmation = http_request(live_server, "POST", "/api/cip/confirm", {"temperature_c": 81.0})
    assert status == 200 and confirmation["confirmation"]["subject"] == "cip-temperature"
    status, pump = http_request(live_server, "POST", "/api/cip/start", {"flow_lph": 6000.0})
    assert status == 200 and pump["action"] == "pump-start"
    status, done = http_request(live_server, "POST", "/api/cip/complete", {"elapsed_seconds": 200.0})
    assert status == 200 and done["stage"]["to_stage"] == "idle"
