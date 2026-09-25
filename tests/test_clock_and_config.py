"""Deterministic clock, identifier helpers and envelope validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from uhtline.core.clock import EPOCH, ManualClock, parse_stamp
from uhtline.core.config import (
    CleaningEnvelope,
    ControlConfig,
    FlowEnvelope,
    TemperatureEnvelope,
    default_config,
    diff_configs,
    envelope_report,
    fast_test_config,
    require_within,
)
from uhtline.core.ids import IdentifierFactory, format_identifier, parse_sequence, validate_token
from uhtline.errors import RangeError, ValidationError


def test_manual_clock_only_moves_forward() -> None:
    clock = ManualClock()
    clock.advance_by(30.0)
    assert clock.monotonic() == 30.0
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))


def test_manual_clock_can_be_set_to_a_later_instant() -> None:
    clock = ManualClock()
    target = EPOCH + timedelta(minutes=5)
    clock.set(target)
    assert clock.now() == target
    assert clock.monotonic() == 300.0
    with pytest.raises(ValueError):
        clock.set(EPOCH)


def test_timestamps_and_ages_use_the_deterministic_epoch() -> None:
    clock = ManualClock()
    stamp = clock.timestamp()
    clock.advance_by(12.5)
    assert stamp == "2026-01-01T00:00:00.000+00:00"
    assert clock.age_seconds(stamp) == 12.5
    assert clock.age_seconds(None) == float("inf")


def test_expiry_helper_adds_the_lifetime_to_the_stamp() -> None:
    clock = ManualClock()
    expiry = clock.expires_at(clock.timestamp(), 90.0)
    assert expiry == parse_stamp("2026-01-01T00:01:30.000+00:00")


def test_naive_timestamps_are_normalised_to_utc() -> None:
    assert parse_stamp(datetime(2026, 1, 1, 0, 0, 0)).tzinfo is timezone.utc
    assert parse_stamp(None) is None


def test_identifier_factory_is_deterministic_across_instances() -> None:
    first = IdentifierFactory()
    second = IdentifierFactory()
    assert first.next("REC") == second.next("REC") == "REC-00001"
    assert first.next("REC") == "REC-00002"
    assert second.snapshot() == {"REC": 2}


def test_identifier_factory_can_resume_from_a_seed() -> None:
    factory = IdentifierFactory({"REC": 7}, width=3)
    assert factory.peek("REC") == 7
    assert factory.next("REC") == "REC-007"


def test_identifier_validation_rejects_empty_and_unsafe_tokens() -> None:
    with pytest.raises(ValidationError):
        validate_token("   ")
    with pytest.raises(ValidationError):
        validate_token("bad token")
    with pytest.raises(ValidationError):
        validate_token("x" * 70)


def test_format_and_parse_identifier_round_trip() -> None:
    identifier = format_identifier("BSL", 12, width=4)
    assert identifier == "BSL-0012"
    assert parse_sequence(identifier, "BSL") == 12
    with pytest.raises(ValidationError):
        parse_sequence(identifier, "SNS")
    with pytest.raises(ValidationError):
        format_identifier("BSL", 0)


def test_require_within_returns_values_inside_the_envelope() -> None:
    assert require_within(5, 1, 10, field_name="value", scope="test") == 5.0
    with pytest.raises(RangeError) as failure:
        require_within(11, 1, 10, field_name="value", scope="test")
    assert failure.value.details["maximum"] == 10


def test_require_within_rejects_non_numeric_values() -> None:
    with pytest.raises(ValidationError):
        require_within("warm", 1, 10, field_name="value", scope="test")


def test_temperature_envelope_validation_rejects_an_inverted_window() -> None:
    with pytest.raises(ValidationError):
        TemperatureEnvelope(sterilization_minimum_c=140.0, sterilization_maximum_c=135.0).validate()


def test_temperature_envelope_validation_rejects_an_invalid_ratio() -> None:
    with pytest.raises(ValidationError):
        TemperatureEnvelope(window_in_spec_ratio=1.5).validate()


def test_flow_envelope_validation_rejects_an_invalid_gain_band() -> None:
    with pytest.raises(ValidationError):
        FlowEnvelope(gain_minimum=1.4, gain_maximum=1.2).validate()
    assert FlowEnvelope().gain_in_band(1.0) is True
    assert FlowEnvelope().gain_in_band(3.0) is False


def test_cleaning_and_throughput_envelopes_reject_inconsistent_sizes() -> None:
    with pytest.raises(ValidationError):
        CleaningEnvelope(minimum_cycle_seconds=0.0).validate()
    config = ControlConfig()
    with pytest.raises(ValidationError):
        ControlConfig(line_code="   ").validate()
    assert config.validate().line_code == "LN-01"


def test_envelope_report_exposes_the_window_bounds() -> None:
    report = envelope_report(default_config())
    assert report["temperature_window_c"] == [135.0, 142.0]
    assert report["flow_window_lph"] == [6000.0, 18000.0]
    assert report["aseptic_pressure_kpa"] == [35.0, 95.0]


def test_diff_configs_lists_only_the_changed_keys() -> None:
    changed = default_config().with_flow(gain_maximum=1.30)
    differences = diff_configs(default_config(), changed)
    assert differences == [{"scope": "flow", "key": "gain_maximum", "left": 1.25, "right": 1.30}]


def test_fast_config_shortens_the_pacing_values_only() -> None:
    fast = fast_test_config()
    assert fast.cleaning.minimum_cycle_seconds < default_config().cleaning.minimum_cycle_seconds
    assert fast.temperature.hold_minimum_seconds == default_config().temperature.hold_minimum_seconds
