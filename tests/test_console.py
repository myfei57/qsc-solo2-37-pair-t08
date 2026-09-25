"""Console routing, HTML rendering and the live HTTP surface."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from uhtline.console.app import ConsoleApp
from uhtline.console.pages import PAGE_NAMES
from uhtline.console.server import ConsoleServer
from uhtline.errors import GateClosedError, ValidationError

from .support import http_request, manual_runtime, sterilizing_runtime

request = http_request


def test_health_route_reports_the_line_and_recovery(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    status, payload = app.handle("GET", "/health", {}, {})
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["recovery"]["valid"] is True
    assert payload["routes"] == app.router.count()


def test_state_route_covers_every_section(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    _, payload = app.handle("GET", "/api/state", {}, {})
    assert payload["stage"]["stage"] == "idle"
    assert payload["records"]["watermark"] == 0


def test_records_route_separates_staged_and_committed_records(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    app = ConsoleApp(runtime)
    runtime.control.start_intake(reason="test")
    runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test")
    _, staged = app.handle("GET", "/api/records", {"pending": "1"}, {})
    assert [item["kind"] for item in staged["pending"]] == ["intake"]
    _, committed = app.handle("POST", "/api/records/commit", {}, {})
    assert committed["watermark"] == 1
    _, visible = app.handle("GET", "/api/records", {"limit": "10"}, {})
    assert [item["kind"] for item in visible["visible"]] == ["intake"]


def test_records_route_can_filter_by_kind(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    app = ConsoleApp(runtime)
    runtime.control.start_intake(reason="test")
    runtime.control.receive(700.0, 6.0, batch_id="B-0001", reason="test")
    runtime.control.commit_records()
    _, payload = app.handle("GET", "/api/records", {"kind": "sterilization-ramp"}, {})
    assert payload["visible"] == []


def test_a_gated_action_surfaces_the_permit_failure(tmp_path: Path) -> None:
    app = ConsoleApp(sterilizing_runtime(tmp_path))
    with pytest.raises(GateClosedError):
        app.handle("POST", "/api/cip/start", {}, {"flow_lph": 6000.0})


def test_unknown_route_and_method_are_rejected(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    with pytest.raises(ValidationError) as missing:
        app.handle("GET", "/api/does-not-exist", {}, {})
    assert missing.value.details["path"] == "/api/does-not-exist"
    with pytest.raises(ValidationError) as wrong_method:
        app.handle("POST", "/health", {}, {})
    assert wrong_method.value.details["allowed"] == ["GET"]


def test_a_missing_request_field_is_reported(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    with pytest.raises(ValidationError) as failure:
        app.handle("POST", "/api/intake/receive", {}, {"volume_litres": 700.0})
    assert failure.value.details["field"] == "temperature_c"


def test_precheck_route_lists_the_blockers(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    _, payload = app.handle("GET", "/api/precheck/sterilization-ramp", {}, {})
    assert payload["permitted"] is False
    assert payload["blockers"][0]["name"] == "preheat-temperature-durable"


def test_every_page_renders_html(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    for name in PAGE_NAMES:
        html = app.page(name)
        assert html.startswith("<!doctype html>")
        assert name in html


def test_an_unknown_page_is_rejected(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    with pytest.raises(ValidationError):
        app.page("overview-extra")


def test_route_inventory_lists_every_registration(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    inventory = app.route_inventory()
    assert len(inventory) == app.router.count()
    assert {"method": "GET", "pattern": "/health", "description": "line health envelope", "parameters": []} in inventory


def test_registering_the_same_route_twice_is_rejected(tmp_path: Path) -> None:
    app = ConsoleApp(manual_runtime(tmp_path))
    with pytest.raises(ValidationError):
        app.router.add("GET", "/health", lambda params, query, body: {}, "duplicate")


def test_decision_and_audit_routes_report_their_summaries(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    app = ConsoleApp(runtime)
    runtime.decisions.record("dwell", "bsl-1", "pass")
    _, decisions = app.handle("GET", "/api/decisions", {"verdict": "pass"}, {})
    assert decisions["counts"]["by_verdict"] == {"pass": 1}
    _, audit = app.handle("GET", "/api/audit", {"limit": "5"}, {})
    assert audit["integrity"]["valid"] is True


def test_http_health_endpoint_answers_over_the_socket(live_server: ConsoleServer) -> None:
    status, payload = request(live_server, "GET", "/health")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["line_code"] == "LN-01"


def test_http_gated_action_returns_a_conflict(live_server: ConsoleServer) -> None:
    status, payload = request(live_server, "POST", "/api/cip/start", {"flow_lph": 6000.0})
    assert status == 409
    assert payload["error"] == "gate-closed"
    assert payload["details"]["gate"] == "cip-temperature-confirmed"


def test_http_unknown_route_returns_a_validation_failure(live_server: ConsoleServer) -> None:
    status, payload = request(live_server, "GET", "/api/nothing")
    assert status == 422
    assert payload["error"] == "validation-error"


def test_http_state_changing_route_updates_the_snapshot(live_server: ConsoleServer) -> None:
    status, payload = request(live_server, "POST", "/api/batches/open", {"batch_id": "B-9000", "product": "milk"})
    assert status == 200
    assert payload["state"] == "open"
    status, snapshot = request(live_server, "GET", "/api/batches")
    assert snapshot["snapshot"]["active"]["batch_id"] == "B-9000"


def test_http_overview_page_serves_html(live_server: ConsoleServer) -> None:
    url = f"http://{live_server.host}:{live_server.port}/overview"
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read().decode("utf-8")
        assert response.status == 200
    assert "permits" in body
    assert "latches" in body


def test_http_rejects_a_body_that_is_not_a_json_object(live_server: ConsoleServer) -> None:
    url = f"http://{live_server.host}:{live_server.port}/api/time/advance"
    prepared = urllib.request.Request(url, data=b"[1, 2, 3]", method="POST", headers={"content-type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as failure:
        urllib.request.urlopen(prepared, timeout=10)
    assert failure.value.code == 422
