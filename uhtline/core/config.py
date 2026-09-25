"""Configured operating envelope, one block per controlled process scope."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..errors import RangeError, ValidationError


def require_within(
    value: float,
    minimum: float,
    maximum: float,
    *,
    field_name: str,
    scope: str,
) -> float:
    """Return ``value`` when it sits inside the closed envelope, else raise."""

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a number", field=field_name, scope=scope) from exc
    if number < minimum or number > maximum:
        raise RangeError(
            f"{field_name} is outside the {scope} envelope",
            field=field_name,
            scope=scope,
            value=number,
            minimum=minimum,
            maximum=maximum,
        )
    return number


@dataclass(frozen=True)
class TemperatureEnvelope:
    """Preheat, sterilization and hold window limits."""

    preheat_target_c: float = 88.0
    preheat_tolerance_c: float = 2.5
    sterilization_target_c: float = 137.0
    sterilization_minimum_c: float = 135.0
    sterilization_maximum_c: float = 142.0
    hold_minimum_seconds: float = 6.0
    hold_maximum_seconds: float = 30.0
    window_minimum_samples: int = 4
    window_in_spec_ratio: float = 0.75
    window_maximum_gap_seconds: float = 45.0

    def validate(self) -> None:
        if self.sterilization_minimum_c >= self.sterilization_maximum_c:
            raise ValidationError("sterilization window is inverted", scope="temperature")
        if not self.sterilization_minimum_c <= self.sterilization_target_c <= self.sterilization_maximum_c:
            raise ValidationError("sterilization target sits outside its own window", scope="temperature")
        if self.hold_minimum_seconds >= self.hold_maximum_seconds or self.hold_minimum_seconds <= 0:
            raise ValidationError("hold duration window is invalid", scope="temperature")
        if not 0.0 < self.window_in_spec_ratio <= 1.0:
            raise ValidationError("in-spec ratio must sit inside (0, 1]", scope="temperature")
        if self.window_minimum_samples < 1:
            raise ValidationError("window requires at least one sample", scope="temperature")
        if self.window_maximum_gap_seconds <= 0:
            raise ValidationError("window gap limit must be positive", scope="temperature")

    def sterilization_in_spec(self, value: float) -> bool:
        return self.sterilization_minimum_c <= float(value) <= self.sterilization_maximum_c

    def preheat_in_spec(self, value: float) -> bool:
        low = self.preheat_target_c - self.preheat_tolerance_c
        high = self.preheat_target_c + self.preheat_tolerance_c
        return low <= float(value) <= high


@dataclass(frozen=True)
class FlowEnvelope:
    """Throughput limits and the permitted calibration gain band."""

    nominal_litres_per_hour: float = 12000.0
    minimum_litres_per_hour: float = 6000.0
    maximum_litres_per_hour: float = 18000.0
    gain_minimum: float = 0.80
    gain_maximum: float = 1.25
    baseline_tolerance_ratio: float = 0.05
    minimum_samples: int = 3

    def validate(self) -> None:
        if self.minimum_litres_per_hour >= self.maximum_litres_per_hour:
            raise ValidationError("flow window is inverted", scope="flow")
        if self.gain_minimum <= 0 or self.gain_minimum >= self.gain_maximum:
            raise ValidationError("gain band is invalid", scope="flow")
        if not 0.0 < self.baseline_tolerance_ratio < 1.0:
            raise ValidationError("baseline tolerance must sit inside (0, 1)", scope="flow")
        if self.minimum_samples < 1:
            raise ValidationError("flow requires at least one sample", scope="flow")

    def gain_in_band(self, gain: float) -> bool:
        return self.gain_minimum <= float(gain) <= self.gain_maximum


@dataclass(frozen=True)
class PressureEnvelope:
    """Overpressure band protecting the sterile boundary."""

    aseptic_minimum_kpa: float = 35.0
    aseptic_maximum_kpa: float = 95.0
    cip_minimum_kpa: float = 20.0
    cip_maximum_kpa: float = 90.0

    def validate(self) -> None:
        if self.aseptic_minimum_kpa >= self.aseptic_maximum_kpa:
            raise ValidationError("aseptic pressure window is inverted", scope="pressure")
        if self.cip_minimum_kpa >= self.cip_maximum_kpa:
            raise ValidationError("cleaning pressure window is inverted", scope="pressure")

    def aseptic_in_spec(self, value: float) -> bool:
        return self.aseptic_minimum_kpa <= float(value) <= self.aseptic_maximum_kpa


@dataclass(frozen=True)
class CleaningEnvelope:
    """Cleaning temperature confirmation and cycle pacing."""

    wash_target_c: float = 80.0
    wash_minimum_c: float = 76.0
    wash_maximum_c: float = 88.0
    minimum_cycle_seconds: float = 300.0
    rinse_seconds: float = 60.0
    pump_minimum_lph: float = 5000.0

    def validate(self) -> None:
        if not self.wash_minimum_c <= self.wash_target_c <= self.wash_maximum_c:
            raise ValidationError("wash target sits outside its window", scope="cleaning")
        if self.minimum_cycle_seconds <= 0 or self.rinse_seconds < 0:
            raise ValidationError("cycle pacing must be positive", scope="cleaning")
        if self.pump_minimum_lph <= 0:
            raise ValidationError("pump minimum flow must be positive", scope="cleaning")

    def wash_in_spec(self, value: float) -> bool:
        return self.wash_minimum_c <= float(value) <= self.wash_maximum_c


@dataclass(frozen=True)
class ThroughputEnvelope:
    """Volumetric limits for intake, balance tank and aseptic tank."""

    intake_minimum_litres: float = 200.0
    intake_maximum_litres: float = 1200.0
    balance_minimum_litres: float = 150.0
    balance_maximum_litres: float = 900.0
    balance_capacity_litres: float = 1500.0
    aseptic_minimum_fill_litres: float = 100.0
    aseptic_capacity_litres: float = 2000.0

    def validate(self) -> None:
        if self.intake_minimum_litres >= self.intake_maximum_litres:
            raise ValidationError("intake window is inverted", scope="throughput")
        if self.balance_maximum_litres > self.balance_capacity_litres:
            raise ValidationError("balance window exceeds tank capacity", scope="throughput")
        if self.aseptic_minimum_fill_litres <= 0 or self.aseptic_capacity_litres <= 0:
            raise ValidationError("aseptic volumes must be positive", scope="throughput")


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Lifetime of confirmations, snapshots and baselines."""

    confirmation_ttl_seconds: float = 900.0
    snapshot_ttl_seconds: float = 1800.0
    baseline_ttl_seconds: float = 3600.0

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if float(value) <= 0:
                raise ValidationError(f"{name} must be positive", scope="evidence")


@dataclass(frozen=True)
class AlarmEnvelope:
    """Alarm board pacing and retention."""

    deviation_hold_seconds: float = 15.0
    maximum_active: int = 32
    history_limit: int = 200

    def validate(self) -> None:
        if self.deviation_hold_seconds <= 0:
            raise ValidationError("deviation hold must be positive", scope="alarm")
        if self.maximum_active < 1 or self.history_limit < self.maximum_active:
            raise ValidationError("alarm retention sizes are inconsistent", scope="alarm")


@dataclass(frozen=True)
class ControlConfig:
    """Aggregate envelope plus the generation scopes published to the registry."""

    line_code: str = "LN-01"
    temperature: TemperatureEnvelope = field(default_factory=TemperatureEnvelope)
    flow: FlowEnvelope = field(default_factory=FlowEnvelope)
    pressure: PressureEnvelope = field(default_factory=PressureEnvelope)
    cleaning: CleaningEnvelope = field(default_factory=CleaningEnvelope)
    throughput: ThroughputEnvelope = field(default_factory=ThroughputEnvelope)
    evidence: EvidenceEnvelope = field(default_factory=EvidenceEnvelope)
    alarm: AlarmEnvelope = field(default_factory=AlarmEnvelope)

    def validate(self) -> "ControlConfig":
        if not self.line_code.strip():
            raise ValidationError("line code must not be empty", scope="line")
        for block in (
            self.temperature,
            self.flow,
            self.pressure,
            self.cleaning,
            self.throughput,
            self.evidence,
            self.alarm,
        ):
            block.validate()
        return self

    def scopes(self) -> dict[str, dict[str, Any]]:
        """Publish every tunable block under a stable generation scope name."""

        return {
            "temperature": asdict(self.temperature),
            "flow": asdict(self.flow),
            "pressure": asdict(self.pressure),
            "cleaning": asdict(self.cleaning),
            "throughput": asdict(self.throughput),
            "evidence": asdict(self.evidence),
            "alarm": asdict(self.alarm),
        }

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["line_code"] = self.line_code
        return payload

    def with_temperature(self, **changes: Any) -> "ControlConfig":
        return replace(self, temperature=replace(self.temperature, **changes))

    def with_flow(self, **changes: Any) -> "ControlConfig":
        return replace(self, flow=replace(self.flow, **changes))

def default_config() -> ControlConfig:
    """Production paced envelope."""

    return ControlConfig()


def fast_test_config() -> ControlConfig:
    """Shorter holds and shorter evidence lifetimes for deterministic scenarios."""

    return ControlConfig(
        temperature=TemperatureEnvelope(
            window_minimum_samples=3,
            window_maximum_gap_seconds=60.0,
        ),
        cleaning=CleaningEnvelope(minimum_cycle_seconds=120.0, rinse_seconds=30.0),
        evidence=EvidenceEnvelope(
            confirmation_ttl_seconds=600.0,
            snapshot_ttl_seconds=900.0,
            baseline_ttl_seconds=1200.0,
        ),
    )


def envelope_report(config: ControlConfig) -> dict[str, Any]:
    """Flatten the envelope into a report the console can print verbatim."""

    report: dict[str, Any] = {"line_code": config.line_code, "scopes": config.scopes()}
    report["temperature_window_c"] = [
        config.temperature.sterilization_minimum_c,
        config.temperature.sterilization_maximum_c,
    ]
    report["hold_seconds"] = [
        config.temperature.hold_minimum_seconds,
        config.temperature.hold_maximum_seconds,
    ]
    report["flow_window_lph"] = [
        config.flow.minimum_litres_per_hour,
        config.flow.maximum_litres_per_hour,
    ]
    report["aseptic_pressure_kpa"] = [
        config.pressure.aseptic_minimum_kpa,
        config.pressure.aseptic_maximum_kpa,
    ]
    return report


def diff_configs(left: ControlConfig, right: ControlConfig) -> list[dict[str, Any]]:
    """List every scope key whose configured value differs between two envelopes."""

    differences: list[dict[str, Any]] = []
    left_scopes = left.scopes()
    right_scopes = right.scopes()
    for scope in sorted(set(left_scopes) | set(right_scopes)):
        left_block = left_scopes.get(scope, {})
        right_block = right_scopes.get(scope, {})
        for key in sorted(set(left_block) | set(right_block)):
            if left_block.get(key) != right_block.get(key):
                differences.append(
                    {
                        "scope": scope,
                        "key": key,
                        "left": left_block.get(key),
                        "right": right_block.get(key),
                    }
                )
    if left.line_code != right.line_code:
        differences.append({"scope": "line", "key": "line_code", "left": left.line_code, "right": right.line_code})
    return differences


__all__ = [
    "AlarmEnvelope",
    "CleaningEnvelope",
    "ControlConfig",
    "EvidenceEnvelope",
    "FlowEnvelope",
    "PressureEnvelope",
    "TemperatureEnvelope",
    "ThroughputEnvelope",
    "default_config",
    "diff_configs",
    "envelope_report",
    "fast_test_config",
    "require_within",
]
