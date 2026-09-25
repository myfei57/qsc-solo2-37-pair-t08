"""Flow meter gain lineage and the corrected throughput it produces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig
from ..errors import ValidationError
from ..persistence.store import DurableStore
from ..telemetry.readings import Reading, ReadingSeries
from ..versioning.generations import GenerationRegistry, ParameterRevision


@dataclass(frozen=True)
class FlowReading:
    """One raw flow sample plus the gain applied to it."""

    raw_lph: float
    gain: float
    generation: int

    @property
    def litres_per_hour(self) -> float:
        return round(self.raw_lph * self.gain, 4)

class FlowMeter:
    """Publishes a new flow generation whenever the meter is recalibrated."""

    scope = "flow-calibration"
    document = "flowmeter"
    channel = "flow-lph"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        generations: GenerationRegistry,
        *,
        series_limit: int = 128,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.generations = generations
        self._gain = 1.0
        self._offset = 0.0
        self._calibrations: list[dict[str, Any]] = []
        self._series = ReadingSeries(store, clock, self.channel, limit=series_limit)
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._gain = float(stored.payload.get("gain", 1.0))
        self._offset = float(stored.payload.get("offset", 0.0))
        self._calibrations = [dict(item) for item in stored.payload.get("calibrations", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "gain": self._gain,
                "offset": self._offset,
                "generation": self.generation,
                "calibrations": self._calibrations,
            },
        )

    @property
    def gain(self) -> float:
        return self._gain

    @property
    def offset(self) -> float:
        return self._offset

    @property
    def generation(self) -> int:
        return self.generations.generation(self.scope) if self.scope in self.generations.scopes() else 0

    def calibrate(self, gain: float, offset: float = 0.0, *, reason: str = "flow calibration") -> ParameterRevision:
        if not self.config.flow.gain_in_band(float(gain)):
            raise ValidationError(
                "calibration gain sits outside the permitted band",
                gain=float(gain),
                minimum=self.config.flow.gain_minimum,
                maximum=self.config.flow.gain_maximum,
            )
        revision = self.generations.publish(
            self.scope,
            {**self.config.flow.__dict__, "gain": float(gain), "offset": float(offset)},
            reason=reason,
        )
        self._gain = float(gain)
        self._offset = float(offset)
        self._calibrations.append(
            {
                "generation": revision.generation,
                "gain": self._gain,
                "offset": self._offset,
                "reason": str(reason),
                "recorded_at": self.clock.timestamp(),
            }
        )
        self.persist()
        return revision

    def calibrations(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._calibrations]

    def ensure_published(self, *, reason: str) -> ParameterRevision:
        """Publish the standing gain as the first generation of this scope."""

        return self.generations.ensure(
            self.scope,
            {**self.config.flow.__dict__, "gain": self._gain, "offset": self._offset},
            reason=reason,
        )

    def measure(self, raw_lph: float, *, gain: float | None = None) -> FlowReading:
        active = self._gain if gain is None else float(gain)
        return FlowReading(raw_lph=float(raw_lph), gain=active, generation=self.generation)

    def record(self, raw_lph: float) -> Reading:
        reading = self.measure(raw_lph)
        return self._series.append(reading.litres_per_hour, unit="L/h", generation=reading.generation)

    def series(self) -> ReadingSeries:
        return self._series

    def within_envelope(self, litres_per_hour: float) -> bool:
        envelope = self.config.flow
        return envelope.minimum_litres_per_hour <= float(litres_per_hour) <= envelope.maximum_litres_per_hour


__all__ = ["FlowMeter", "FlowReading"]
