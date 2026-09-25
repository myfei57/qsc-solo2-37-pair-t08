"""Deterministic clock used to drive every time dependent decision."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def parse_stamp(value: str | datetime | None) -> datetime | None:
    """Return an aware timestamp, normalising naive values to UTC."""

    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class Clock:
    """Base clock: UTC timestamps plus a monotonic elapsed counter."""

    epoch: datetime = EPOCH
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def timestamp(self) -> str:
        return self.now().isoformat(timespec="milliseconds")

    def age_seconds(self, stamp: str | datetime | None) -> float:
        value = parse_stamp(stamp)
        if value is None:
            return float("inf")
        return max(0.0, (self.now() - value).total_seconds())

    def expires_at(self, stamp: str | datetime, ttl_seconds: float) -> datetime:
        return parse_stamp(stamp) + timedelta(seconds=ttl_seconds)  # type: ignore[operator]

class ManualClock(Clock):
    """Simulation clock: time only moves when the caller asks it to."""

    def __init__(self, start: datetime | None = None) -> None:
        super().__init__(epoch=start or EPOCH)
        self._current = self.epoch
        self._elapsed = 0.0

    def now(self) -> datetime:
        with self._lock:
            return self._current

    def monotonic(self) -> float:
        with self._lock:
            return self._elapsed

    def advance(self, delta: timedelta) -> datetime:
        seconds = delta.total_seconds()
        if seconds < 0.0:
            raise ValueError("manual clock cannot move backwards")
        with self._lock:
            self._current += delta
            self._elapsed += seconds
            return self._current

    def advance_by(self, seconds: float) -> None:
        self.advance(timedelta(seconds=seconds))

    def set(self, value: datetime) -> datetime:
        normalised = parse_stamp(value)
        with self._lock:
            delta = normalised - self._current  # type: ignore[operator]
            if delta.total_seconds() < 0.0:
                raise ValueError("manual clock cannot move backwards")
            self._current = normalised  # type: ignore[assignment]
            self._elapsed += delta.total_seconds()
            return self._current


__all__ = ["EPOCH", "Clock", "ManualClock", "parse_stamp"]
