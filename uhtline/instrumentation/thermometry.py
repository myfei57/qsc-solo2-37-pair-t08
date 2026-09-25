"""Thermometry: sensor map, calibration generations and converted readings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..core.clock import Clock
from ..core.config import ControlConfig
from ..core.ids import validate_token
from ..errors import NotFoundError, StaleGenerationError, ValidationError
from ..persistence.store import DurableStore
from ..telemetry.readings import Reading, ReadingSeries
from ..versioning.generations import GenerationRegistry


@dataclass(frozen=True)
class Sensor:
    sensor_id: str
    position: str
    gain: float
    offset: float
    generation: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Sensor":
        return cls(
            sensor_id=str(value["sensor_id"]),
            position=str(value["position"]),
            gain=float(value["gain"]),
            offset=float(value["offset"]),
            generation=int(value["generation"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "position": self.position,
            "gain": self.gain,
            "offset": self.offset,
            "generation": self.generation,
        }


class Thermometry:
    """Keeps the sensor map and every calibration pinned to one generation scope."""

    scope = "sensors"
    document = "thermometry"

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
        self._series_limit = series_limit
        self._sensors: dict[str, Sensor] = {}
        self._history: dict[str, list[Sensor]] = {}
        self._channels: list[str] = []
        self._series: dict[str, ReadingSeries] = {}
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        for item in stored.payload.get("sensors", []):
            sensor = Sensor.from_dict(item)
            self._sensors[sensor.sensor_id] = sensor
        for key, values in (stored.payload.get("history") or {}).items():
            self._history[str(key)] = [Sensor.from_dict(item) for item in values]
        self._channels = [str(item) for item in stored.payload.get("channels", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "sensors": [self._sensors[key].as_dict() for key in sorted(self._sensors)],
                "history": {key: [item.as_dict() for item in values] for key, values in sorted(self._history.items())},
                "channels": list(self._channels),
            },
        )

    # -- registration and calibration -------------------------------------

    def register_sensor(self, sensor_id: str, position: str, *, reason: str = "commissioning") -> Sensor:
        key = validate_token(sensor_id, field_name="sensor id")
        if key in self._sensors:
            raise ValidationError("sensor is already registered", sensor=key)
        return self._publish(key, position, gain=1.0, offset=0.0, reason=reason)

    def remap(self, sensor_id: str, position: str, *, reason: str = "sensor replacement") -> Sensor:
        existing = self.sensor(sensor_id)
        return self._publish(existing.sensor_id, position, gain=existing.gain, offset=existing.offset, reason=reason)

    def calibrate(self, sensor_id: str, gain: float, offset: float = 0.0, *, reason: str = "calibration") -> Sensor:
        existing = self.sensor(sensor_id)
        if float(gain) <= 0:
            raise ValidationError("calibration gain must be positive", sensor=existing.sensor_id)
        return self._publish(existing.sensor_id, existing.position, gain=float(gain), offset=float(offset), reason=reason)

    def _publish(self, sensor_id: str, position: str, *, gain: float, offset: float, reason: str) -> Sensor:
        label = validate_token(position, field_name="sensor position")
        sensor_map = {key: value.position for key, value in sorted(self._sensors.items())}
        sensor_map[sensor_id] = label
        revision = self.generations.publish(
            self.scope,
            {
                **asdict(self.config.temperature),
                "sensor_map": sensor_map,
                "sensor": sensor_id,
                "gain": gain,
                "offset": offset,
            },
            reason=reason,
        )
        sensor = Sensor(sensor_id=sensor_id, position=label, gain=gain, offset=offset, generation=revision.generation)
        self._sensors[sensor_id] = sensor
        self._history.setdefault(sensor_id, []).append(sensor)
        if sensor_id not in self._channels:
            self._channels.append(sensor_id)
        self.persist()
        return sensor

    def sensor(self, sensor_id: str) -> Sensor:
        key = str(sensor_id)
        if key not in self._sensors:
            raise NotFoundError("sensor is not registered", sensor=key)
        return self._sensors[key]

    def sensors(self) -> list[Sensor]:
        return [self._sensors[key] for key in sorted(self._sensors)]

    def sensor_history(self, sensor_id: str) -> list[Sensor]:
        return list(self._history.get(str(sensor_id), []))

    def current_map(self) -> dict[str, str]:
        return {sensor.sensor_id: sensor.position for sensor in self.sensors()}

    def map_at(self, generation: int) -> dict[str, str]:
        """Reconstruct the sensor map as it stood at a published generation."""

        mapping: dict[str, str] = {}
        for sensor_id, values in self._history.items():
            for sensor in values:
                if sensor.generation <= int(generation):
                    mapping[sensor_id] = sensor.position
        return mapping

    def position_of(self, sensor_id: str) -> str:
        return self.sensor(sensor_id).position

    # -- readings ----------------------------------------------------------

    def series(self, channel: str) -> ReadingSeries:
        key = str(channel)
        if key not in self._series:
            self._series[key] = ReadingSeries(self.store, self.clock, key, limit=self._series_limit)
        return self._series[key]

    def convert(self, sensor_id: str, raw_c: float, *, generation: int | None = None) -> float:
        """Apply the calibration of the requested generation to a raw value."""

        self.sensor(sensor_id)
        selected = self.sensor(sensor_id)
        if generation is not None:
            candidates = [item for item in self.sensor_history(sensor_id) if item.generation <= int(generation)]
            if not candidates:
                raise StaleGenerationError(
                    "sensor had no calibration at that generation",
                    sensor=sensor_id,
                    generation=int(generation),
                )
            selected = candidates[-1]
        return round(float(raw_c) * selected.gain + selected.offset, 6)

    def record_reading(self, sensor_id: str, raw_c: float, *, unit: str = "degC") -> Reading:
        sensor = self.sensor(sensor_id)
        value = self.convert(sensor_id, raw_c)
        return self.series(sensor.sensor_id).append(value, unit=unit, generation=sensor.generation)

    def latest(self, sensor_id: str) -> Reading | None:
        return self.series(str(sensor_id)).latest()

    def history(self, sensor_id: str, *, limit: int | None = None) -> list[Reading]:
        return self.series(str(sensor_id)).all(limit)

__all__ = ["Sensor", "Thermometry"]
