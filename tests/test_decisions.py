"""Contract: window thresholds, current versus historical state and query filters."""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.core.clock import ManualClock
from uhtline.core.config import fast_test_config
from uhtline.errors import DuplicateError, NotFoundError, StateError, ValidationError
from uhtline.persistence.store import DurableStore
from uhtline.telemetry.readings import ReadingSeries

from .support import manual_runtime, manual_store

ENVELOPE = fast_test_config().temperature


def feed(series: ReadingSeries, clock: ManualClock, values: list[float], step: float = 1.0) -> None:
    for value in values:
        series.append(value, unit="degC", generation=1)
        clock.advance_by(step)


def test_window_decision_holds_when_there_are_too_few_samples(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    series = ReadingSeries(DurableStore(tmp_path, ManualClock()), ManualClock(), "window")
    feed(series, ManualClock(), [137.0, 137.1])
    outcome = runtime.control.window.evaluate("TS-STERILE", series.window(2))
    assert outcome.verdict == "hold"
    assert outcome.accepted is False
    assert "samples" in outcome.reasons[0]


def test_window_decision_fails_when_the_in_spec_ratio_is_low(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    store, clock = manual_store(tmp_path)
    series = ReadingSeries(store, clock, "window")
    feed(series, clock, [131.0, 132.0, 133.0, 137.0])
    outcome = runtime.control.window.evaluate("TS-STERILE", series.window(4))
    assert outcome.verdict == "fail"
    assert outcome.ratio == 0.25


def test_window_decision_fails_on_an_overlong_sample_gap(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    store, clock = manual_store(tmp_path)
    series = ReadingSeries(store, clock, "window")
    feed(series, clock, [137.0, 137.1, 137.2], step=1.0)
    clock.advance_by(ENVELOPE.window_maximum_gap_seconds + 5.0)
    feed(series, clock, [137.3], step=1.0)
    outcome = runtime.control.window.evaluate("TS-STERILE", series.window(4))
    assert outcome.verdict == "fail"
    assert outcome.observed_gap_seconds > ENVELOPE.window_maximum_gap_seconds


def test_window_decision_passes_inside_the_envelope(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    store, clock = manual_store(tmp_path)
    series = ReadingSeries(store, clock, "window")
    feed(series, clock, [136.5, 137.0, 137.4, 136.8])
    outcome = runtime.control.window.evaluate("TS-STERILE", series.window(4))
    assert outcome.verdict == "pass"
    assert outcome.minimum == 136.5
    assert outcome.mean == pytest.approx(136.925, abs=1e-3)


def test_evaluated_window_is_recorded_with_the_current_generation(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.record_temperature("TS-STERILE", 137.0, reason="test")
    runtime.control.record_temperature("TS-STERILE", 137.1, reason="test")
    runtime.control.record_temperature("TS-STERILE", 137.2, reason="test")
    outcome = runtime.control.evaluate_window("TS-STERILE", 3, reason="test")
    recorded = runtime.control.decision_history(kind="sterilization-window")
    assert outcome["verdict"] == "pass"
    assert recorded[-1]["generation"] == runtime.generations.generation("sensors")


def test_current_state_revision_differs_from_the_pinned_revision(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    first = runtime.control.record_state(reason="test")
    runtime.control.start_intake(reason="test")
    second = runtime.control.record_state(reason="test")
    assert second["revision"] > first["revision"]
    assert runtime.control.state_as_of(first["revision"])["payload"]["stage"] == "idle"
    assert runtime.control.timeline.current().payload["stage"] == "intake"


def test_state_as_of_returns_the_latest_revision_at_or_before_the_request(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.record_state(reason="test")
    runtime.control.record_state(reason="test")
    assert runtime.control.state_as_of(1)["revision"] == 1
    assert runtime.control.state_as_of(2)["revision"] == 2


def test_state_as_of_an_unknown_revision_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(NotFoundError):
        runtime.control.state_as_of(42)


def test_batch_identifier_cannot_be_reused(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.open_batch("B-0001", "milk", reason="test")
    runtime.control.close_batch("B-0001", "released", reason="test")
    with pytest.raises(DuplicateError):
        runtime.control.open_batch("B-0001", "milk", reason="test")


def test_a_second_batch_cannot_open_while_one_is_live(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.open_batch("B-0001", "milk", reason="test")
    with pytest.raises(StateError):
        runtime.control.open_batch("B-0002", "milk", reason="test")


def test_closing_an_unregistered_batch_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    with pytest.raises(StateError):
        runtime.control.close_batch("B-0404", "released", reason="test")


def test_an_unknown_batch_outcome_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.open_batch("B-0001", "milk", reason="test")
    with pytest.raises(ValidationError):
        runtime.control.close_batch("B-0001", "maybe", reason="test")


def test_closing_a_batch_that_is_already_closed_is_rejected(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.control.open_batch("B-0001", "milk", reason="test")
    runtime.control.close_batch("B-0001", "released", reason="test")
    with pytest.raises(StateError):
        runtime.control.close_batch("B-0001", "released", reason="test")


def test_decision_log_filters_by_kind_and_verdict(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.decisions.record("dwell", "bsl-1", "pass", batch_id="B-0001")
    runtime.decisions.record("dwell", "bsl-2", "short", batch_id="B-0001")
    runtime.decisions.record("window", "TS-STERILE", "fail", batch_id="B-0002")
    assert len(runtime.control.decision_history(kind="dwell")) == 2
    assert len(runtime.control.decision_history(verdict="short")) == 1
    assert runtime.control.decision_history(batch_id="B-0002")[0]["kind"] == "window"


def test_decision_counts_group_by_kind_and_verdict(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.decisions.record("dwell", "bsl-1", "pass")
    runtime.decisions.record("dwell", "bsl-2", "pass")
    runtime.decisions.record("window", "TS-STERILE", "fail")
    counts = runtime.decisions.counts()
    assert counts["total"] == 3
    assert counts["by_kind"] == {"dwell": 2, "window": 1}
    assert counts["by_verdict"] == {"pass": 2, "fail": 1}


def test_batch_query_filters_by_state_and_product(tmp_path: Path) -> None:
    runtime = manual_runtime(tmp_path)
    runtime.batches.open("B-0001", "milk", reason="test")
    runtime.batches.close("B-0001", "released", reason="test")
    runtime.batches.open("B-0002", "cream", reason="test")
    assert [record.batch_id for record in runtime.batches.batches(state="open")] == ["B-0002"]
    assert [record.batch_id for record in runtime.batches.batches(product="milk")] == ["B-0001"]
    assert runtime.batches.active().batch_id == "B-0002"


def test_reading_filter_queries_return_only_matching_samples(tmp_path: Path) -> None:
    store, clock = manual_store(tmp_path)
    series = ReadingSeries(store, clock, "window")
    feed(series, clock, [130.0, 137.0, 138.0, 120.0])
    assert len(series.filtered(minimum=136.0)) == 2
    assert len(series.filtered(maximum=131.0)) == 2
    assert len(series.filtered(generation=1, limit=2)) == 2
    assert series.statistics()["count"] == 4
