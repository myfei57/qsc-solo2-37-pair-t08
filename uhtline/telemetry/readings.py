"""Bounded, deterministic series of instrument readings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..core.clock import Clock
from ..errors import ValidationError
from ..persistence.store import DurableStore


@dataclass(frozen=True)
class Reading:
    sequence: int
    channel: str
    value: float
    unit: str
    timestamp: str
    generation: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Reading":
        return cls(
            sequence=int(value["sequence"]),
            channel=str(value["channel"]),
            value=float(value["value"]),
            unit=str(value["unit"]),
            timestamp=str(value["timestamp"]),
            generation=int(value["generation"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "channel": self.channel,
            "value": self.value,
            "unit": self.unit,
            "timestamp": self.timestamp,
            "generation": self.generation,
        }


class ReadingSeries:
    """A capped series that keeps the most recent samples of one channel."""

    def __init__(self, store: DurableStore, clock: Clock, channel: str, *, limit: int = 256) -> None:
        self.store = store
        self.clock = clock
        self.channel = str(channel)
        self.limit = int(limit)
        if not self.channel:
            raise ValidationError("reading series needs a channel name")
        if self.limit < 1:
            raise ValidationError("reading series needs a positive capacity", channel=self.channel)
        self.document = f"series-{self.channel}"
        self._readings: list[Reading] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        items = stored.payload.get("readings")
        if not isinstance(items, list):
            raise ValidationError("reading series document is malformed", channel=self.channel)
        self._readings = [Reading.from_dict(item) for item in items]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {"channel": self.channel, "limit": self.limit, "readings": [item.as_dict() for item in self._readings]},
        )

    def append(self, value: float, *, unit: str = "", generation: int = 0) -> Reading:
        reading = Reading(
            sequence=(self._readings[-1].sequence + 1) if self._readings else 1,
            channel=self.channel,
            value=float(value),
            unit=str(unit),
            timestamp=self.clock.timestamp(),
            generation=int(generation),
        )
        self._readings.append(reading)
        if len(self._readings) > self.limit:
            self._readings = self._readings[-self.limit :]
        self.persist()
        return reading

    def all(self, limit: int | None = None) -> list[Reading]:
        if limit is None:
            return list(self._readings)
        return self._readings[-max(0, int(limit)) :]

    def latest(self) -> Reading | None:
        return None if not self._readings else self._readings[-1]

    def window(self, count: int) -> list[Reading]:
        if count < 1:
            raise ValidationError("window needs at least one sample", channel=self.channel, count=count)
        return self._readings[-int(count) :]

    def filtered(
        self,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        generation: int | None = None,
        limit: int | None = None,
    ) -> list[Reading]:
        selected = self._readings
        if minimum is not None:
            selected = [reading for reading in selected if reading.value >= float(minimum)]
        if maximum is not None:
            selected = [reading for reading in selected if reading.value <= float(maximum)]
        if generation is not None:
            selected = [reading for reading in selected if reading.generation == int(generation)]
        if limit is not None:
            selected = selected[-max(0, int(limit)) :]
        return list(selected)

    def statistics(self) -> dict[str, Any]:
        if not self._readings:
            return {"channel": self.channel, "count": 0}
        values = [reading.value for reading in self._readings]
        return {
            "channel": self.channel,
            "count": len(values),
            "minimum": min(values),
            "maximum": max(values),
            "mean": round(sum(values) / len(values), 4),
            "latest": values[-1],
        }


__all__ = ["Reading", "ReadingSeries"]
