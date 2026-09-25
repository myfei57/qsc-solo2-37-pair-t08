"""Decision semantics: window comparison, current versus historical state, queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..core.clock import Clock, parse_stamp
from ..core.config import TemperatureEnvelope
from ..errors import ValidationError
from ..persistence.store import DurableStore
from ..telemetry.readings import Reading

PASS = "pass"
FAIL = "fail"
HOLD = "hold"


@dataclass(frozen=True)
class WindowOutcome:
    """Result of comparing a bounded reading window against the envelope."""

    label: str
    verdict: str
    samples: int
    in_spec: int
    ratio: float
    required_ratio: float
    minimum: float | None
    maximum: float | None
    mean: float | None
    maximum_gap_seconds: float
    observed_gap_seconds: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def accepted(self) -> bool:
        return self.verdict == PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "verdict": self.verdict,
            "samples": self.samples,
            "in_spec": self.in_spec,
            "ratio": self.ratio,
            "required_ratio": self.required_ratio,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "maximum_gap_seconds": self.maximum_gap_seconds,
            "observed_gap_seconds": self.observed_gap_seconds,
            "reasons": list(self.reasons),
            "accepted": self.accepted,
        }


class WindowDecision:
    """Threshold and window comparison with an explicit verdict trail."""

    def __init__(self, envelope: TemperatureEnvelope) -> None:
        self.envelope = envelope

    def evaluate(self, label: str, readings: Sequence[Reading]) -> WindowOutcome:
        samples = [reading for reading in readings]
        values = [reading.value for reading in samples]
        required = self.envelope.window_minimum_samples
        reasons: list[str] = []
        enough = len(samples) >= required
        if not enough:
            reasons.append(f"only {len(samples)} samples, {required} required")
        gap = self._largest_gap(samples)
        if enough and gap > self.envelope.window_maximum_gap_seconds:
            reasons.append(f"sample gap {gap:g}s exceeds {self.envelope.window_maximum_gap_seconds:g}s")
        in_spec = sum(1 for value in values if self.envelope.sterilization_in_spec(value))
        ratio = 0.0 if not values else round(in_spec / len(values), 6)
        if enough and ratio < self.envelope.window_in_spec_ratio:
            reasons.append(f"in-spec ratio {ratio:g} below {self.envelope.window_in_spec_ratio:g}")
        if not enough:
            verdict = HOLD
        elif reasons:
            verdict = FAIL
        else:
            verdict = PASS
        return WindowOutcome(
            label=str(label),
            verdict=verdict,
            samples=len(values),
            in_spec=in_spec,
            ratio=ratio,
            required_ratio=self.envelope.window_in_spec_ratio,
            minimum=None if not values else min(values),
            maximum=None if not values else max(values),
            mean=None if not values else round(sum(values) / len(values), 4),
            maximum_gap_seconds=self.envelope.window_maximum_gap_seconds,
            observed_gap_seconds=gap,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _largest_gap(readings: Sequence[Reading]) -> float:
        if len(readings) < 2:
            return 0.0
        stamps = [parse_stamp(reading.timestamp) for reading in readings]
        gaps = [
            (stamps[index] - stamps[index - 1]).total_seconds()  # type: ignore[operator]
            for index in range(1, len(stamps))
        ]
        return round(max(gaps), 6) if gaps else 0.0


@dataclass(frozen=True)
class StateEntry:
    revision: int
    label: str
    payload: dict[str, Any]
    generation: int
    timestamp: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StateEntry":
        return cls(
            revision=int(value["revision"]),
            label=str(value["label"]),
            payload=dict(value.get("payload") or {}),
            generation=int(value.get("generation", 0)),
            timestamp=str(value.get("timestamp", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "label": self.label,
            "payload": self.payload,
            "generation": self.generation,
            "timestamp": self.timestamp,
        }


class StateTimeline:
    """Keeps every published state so a query can pin a revision or follow the head."""

    document = "state-timeline"

    def __init__(self, store: DurableStore, clock: Clock, *, limit: int = 128) -> None:
        self.store = store
        self.clock = clock
        self.limit = int(limit)
        self._entries: list[StateEntry] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._entries = [StateEntry.from_dict(item) for item in stored.payload.get("entries", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {"limit": self.limit, "entries": [entry.as_dict() for entry in self._entries[-self.limit :]]},
        )

    def record(self, label: str, payload: dict[str, Any], *, generation: int = 0) -> StateEntry:
        if not isinstance(payload, dict):
            raise ValidationError("state payload must be an object", label=label)
        entry = StateEntry(
            revision=len(self._entries) + 1,
            label=str(label),
            payload=dict(payload),
            generation=int(generation),
            timestamp=self.clock.timestamp(),
        )
        self._entries.append(entry)
        if len(self._entries) > self.limit:
            self._entries = self._entries[-self.limit :]
        self.persist()
        return entry

    def current(self) -> StateEntry | None:
        return None if not self._entries else self._entries[-1]

    def as_of(self, revision: int) -> StateEntry | None:
        for entry in reversed(self._entries):
            if entry.revision <= int(revision):
                return entry
        return None

    def labels(self) -> list[str]:
        return sorted({entry.label for entry in self._entries})

    def snapshot(self) -> dict[str, Any]:
        current = self.current()
        return {
            "revisions": 0 if not self._entries else self._entries[-1].revision,
            "current": None if current is None else current.as_dict(),
            "labels": self.labels(),
        }


class DecisionLog:
    """Append-only verdict log with filters over kind, verdict, batch and time."""

    document = "decisions"

    def __init__(self, store: DurableStore, clock: Clock, *, limit: int = 256) -> None:
        self.store = store
        self.clock = clock
        self.limit = int(limit)
        self._decisions: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._decisions = [dict(item) for item in stored.payload.get("decisions", [])]

    def persist(self) -> None:
        self.store.write(self.document, {"limit": self.limit, "decisions": self._decisions[-self.limit :]})

    def record(
        self,
        kind: str,
        subject: str,
        verdict: str,
        *,
        detail: str = "",
        batch_id: str | None = None,
        generation: int = 0,
    ) -> dict[str, Any]:
        label = str(kind).strip()
        if not label:
            raise ValidationError("decision kind must not be empty")
        entry = {
            "decision_id": f"dec-{len(self._decisions) + 1:05d}",
            "kind": label,
            "subject": str(subject),
            "verdict": str(verdict),
            "detail": str(detail),
            "batch_id": None if batch_id is None else str(batch_id),
            "generation": int(generation),
            "timestamp": self.clock.timestamp(),
        }
        self._decisions.append(entry)
        self.persist()
        return dict(entry)

    def query(
        self,
        *,
        kind: str | None = None,
        verdict: str | None = None,
        batch_id: str | None = None,
        subject: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        selected = self._decisions
        if kind is not None:
            selected = [item for item in selected if item["kind"] == str(kind)]
        if verdict is not None:
            selected = [item for item in selected if item["verdict"] == str(verdict)]
        if batch_id is not None:
            selected = [item for item in selected if item["batch_id"] == str(batch_id)]
        if subject is not None:
            selected = [item for item in selected if item["subject"] == str(subject)]
        if since is not None:
            selected = [item for item in selected if item["timestamp"] >= str(since)]
        return [dict(item) for item in selected[-max(0, int(limit)) :]]

    def counts(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_verdict: dict[str, int] = {}
        for item in self._decisions:
            by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
            by_verdict[item["verdict"]] = by_verdict.get(item["verdict"], 0) + 1
        return {"total": len(self._decisions), "by_kind": by_kind, "by_verdict": by_verdict}

__all__ = ["FAIL", "HOLD", "PASS", "DecisionLog", "StateEntry", "StateTimeline", "WindowDecision", "WindowOutcome"]
