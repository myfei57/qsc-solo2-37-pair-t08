"""Deterministic counters and gauges exposed by the health endpoint."""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError


class MetricsRegistry:
    """Counters and gauges with no wall-clock or random component."""

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}

    def increment(self, name: str, amount: float = 1.0) -> float:
        label = self._label(name)
        self._counters[label] = self._counters.get(label, 0.0) + float(amount)
        return self._counters[label]

    def count(self, name: str) -> float:
        return self._counters.get(self._label(name), 0.0)

    def gauge(self, name: str, value: float) -> float:
        label = self._label(name)
        self._gauges[label] = float(value)
        return self._gauges[label]

    def peek(self, name: str) -> float | None:
        return self._gauges.get(self._label(name))

    def reset(self) -> None:
        self._counters.clear()
        self._gauges.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": {key: self._counters[key] for key in sorted(self._counters)},
            "gauges": {key: self._gauges[key] for key in sorted(self._gauges)},
        }

    @staticmethod
    def _label(name: str) -> str:
        label = str(name).strip()
        if not label:
            raise ValidationError("metric name must not be empty")
        return label


__all__ = ["MetricsRegistry"]
