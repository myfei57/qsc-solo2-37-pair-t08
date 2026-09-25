"""Contract: generation numbers, expiring confirmations, snapshots and baselines."""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.core.config import fast_test_config
from uhtline.errors import NotFoundError, StaleGenerationError, StaleWarrantyError

from .support import STERILE_SENSOR, manual_runtime


def test_confirmation_from_a_superseded_generation_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    confirmation = runtime.warranties.issue_confirmation("sensors", "sterilization", reason="test")
    runtime.control.recalibrate_temperature(STERILE_SENSOR, 1.02, 0.1, reason="recalibration")
    with pytest.raises(StaleWarrantyError) as failure:
        runtime.warranties.require_confirmation(confirmation.confirmation_id, scope="sensors")
    assert failure.value.details["state"] == "superseded"


def test_confirmation_is_rejected_after_its_ttl_elapses(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    confirmation = runtime.warranties.issue_confirmation(
        "sensors",
        "sterilization",
        ttl_seconds=30.0,
        reason="test",
    )
    runtime.control.advance_time(31.0)
    with pytest.raises(StaleWarrantyError) as failure:
        runtime.warranties.require_confirmation(confirmation.confirmation_id, scope="sensors")
    assert failure.value.details["state"] == "elapsed"


def test_confirmation_issued_for_another_subject_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    confirmation = runtime.warranties.issue_confirmation("sensors", "cip-temperature", reason="test")
    with pytest.raises(StaleWarrantyError) as failure:
        runtime.warranties.require_confirmation(
            confirmation.confirmation_id,
            scope="sensors",
            subject="sterilization",
        )
    assert failure.value.details["state"] == "subject-mismatch"


def test_confirmation_can_only_be_consumed_once(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    confirmation = runtime.warranties.issue_confirmation("sensors", "sterilization", reason="test")
    runtime.warranties.consume_confirmation(confirmation.confirmation_id, scope="sensors")
    with pytest.raises(StaleWarrantyError) as failure:
        runtime.warranties.require_confirmation(confirmation.confirmation_id, scope="sensors")
    assert failure.value.details["state"] == "consumed"


def test_baseline_is_rejected_after_the_flow_meter_is_recalibrated(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    baseline = runtime.control.confirm_flow_baseline(reason="test")
    runtime.control.recalibrate_flow(1.1, reason="recalibration")
    with pytest.raises(StaleWarrantyError) as failure:
        runtime.warranties.require_baseline(baseline["baseline_id"], scope="flow-calibration")
    assert failure.value.details["state"] == "superseded"


def test_snapshot_is_rejected_once_the_sensor_generation_moves(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    snapshot = runtime.control.capture_snapshot("state", reason="test")
    runtime.control.recalibrate_temperature(STERILE_SENSOR, 1.03, 0.0, reason="recalibration")
    with pytest.raises(StaleWarrantyError):
        runtime.control.require_snapshot(snapshot["snapshot_id"])


def test_snapshot_from_another_scope_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    snapshot = runtime.warranties.capture_snapshot("state", "sensors", {"value": 1}, reason="test")
    with pytest.raises(StaleWarrantyError) as failure:
        runtime.warranties.require_snapshot(snapshot.snapshot_id, scope="flow-calibration")
    assert failure.value.details["state"] == "scope-mismatch"


def test_unknown_evidence_reference_is_reported_as_missing(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(NotFoundError):
        runtime.warranties.require_confirmation("cfm-sensors-99999", scope="sensors")


def test_generations_increase_by_one_and_keep_their_history(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    first = runtime.generations.publish("test-scope", {"value": 1}, reason="test")
    second = runtime.generations.publish("test-scope", {"value": 2}, reason="test")
    assert (first.generation, second.generation) == (1, 2)
    assert len(runtime.generations.revisions("test-scope")) == 2
    assert runtime.generations.revision("test-scope", 1).payload == {"value": 1}


def test_require_current_rejects_a_superseded_generation(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.generations.publish("test-scope", {"value": 1}, reason="test")
    runtime.generations.publish("test-scope", {"value": 2}, reason="test")
    with pytest.raises(StaleGenerationError):
        runtime.generations.require_current("test-scope", 1)


def test_require_current_reports_an_unpublished_generation(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.generations.publish("test-scope", {"value": 1}, reason="test")
    with pytest.raises(StaleGenerationError):
        runtime.generations.require_current("test-scope", 7)


def test_expire_stale_counts_the_evidence_that_lost_validity(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.warranties.issue_confirmation("sensors", "sterilization", ttl_seconds=10.0, reason="test")
    runtime.warranties.record_baseline("flow-calibration", {"gain": 1.0}, ttl_seconds=10.0, reason="test")
    runtime.clock.advance_by(11.0)
    assert runtime.warranties.expire_stale() == 2
    assert runtime.warranties.state_counts() == {"elapsed": 2}


def test_advancing_the_clock_reports_the_evidence_it_invalidated(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.warranties.issue_confirmation("sensors", "sterilization", ttl_seconds=5.0, reason="test")
    assert runtime.control.advance_time(6.0)["invalidated"] == 1


def test_applying_a_changed_configuration_only_bumps_the_scope_that_moved(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    before = runtime.generations.as_dict()["scopes"]
    changed = fast_test_config().with_temperature(hold_maximum_seconds=25.0)
    report = runtime.control.apply_config(changed, reason="test")
    assert report["published"] == [{"scope": "temperature", "generation": before["temperature"]["generation"] + 1}]
    assert runtime.hold.config.temperature.hold_maximum_seconds == 25.0
    assert runtime.control.apply_config(changed, reason="test")["published"] == []
