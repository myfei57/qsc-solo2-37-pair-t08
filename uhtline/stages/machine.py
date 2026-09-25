"""Ordered process stages with an explicit transition table."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..core.clock import Clock
from ..errors import StageOrderError, StateError, ValidationError
from ..persistence.audit import AuditLedger
from ..persistence.store import DurableStore


class Stage(str, Enum):
    """Every stage the line can occupy."""

    IDLE = "idle"
    INTAKE = "intake"
    BALANCE = "balance"
    PREHEAT = "preheat"
    STERILIZE = "sterilize"
    HOLD = "hold"
    COOL = "cool"
    ASEPTIC_FILL = "aseptic-fill"
    COMPLETE = "complete"
    CIP = "cip"


SEQUENCE: tuple[Stage, ...] = (
    Stage.IDLE,
    Stage.INTAKE,
    Stage.BALANCE,
    Stage.PREHEAT,
    Stage.STERILIZE,
    Stage.HOLD,
    Stage.COOL,
    Stage.ASEPTIC_FILL,
    Stage.COMPLETE,
)

CIP_ENTRY_STAGES: frozenset[Stage] = frozenset({Stage.IDLE, Stage.INTAKE, Stage.BALANCE, Stage.COOL, Stage.COMPLETE})


def stage_index(stage: Stage) -> int:
    if stage not in SEQUENCE:
        raise ValidationError("stage sits outside the production sequence", stage=stage.value)
    return SEQUENCE.index(stage)


@dataclass(frozen=True)
class StageTransition:
    sequence: int
    from_stage: str
    to_stage: str
    reason: str
    timestamp: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StageTransition":
        return cls(
            sequence=int(value["sequence"]),
            from_stage=str(value["from_stage"]),
            to_stage=str(value["to_stage"]),
            reason=str(value["reason"]),
            timestamp=str(value["timestamp"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


class StageMachine:
    """Only the next stage in the sequence may be entered, or the cleaning branch."""

    document = "stage-machine"

    def __init__(self, store: DurableStore, clock: Clock, audit: AuditLedger) -> None:
        self.store = store
        self.clock = clock
        self.audit = audit
        self._stage = Stage.IDLE
        self._history: list[StageTransition] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        payload = stored.payload
        self._stage = Stage(str(payload.get("stage", Stage.IDLE.value)))
        self._history = [StageTransition.from_dict(item) for item in payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "stage": self._stage.value,
                "history": [item.as_dict() for item in self._history],
            },
        )

    def current(self) -> Stage:
        return self._stage

    def is_at(self, stage: Stage) -> bool:
        return self._stage is stage

    def index(self) -> int:
        return -1 if self._stage is Stage.CIP else stage_index(self._stage)

    def history(self, limit: int = 50) -> list[StageTransition]:
        return self._history[-max(0, int(limit)) :]

    def enter(self, target: Stage, *, reason: str) -> StageTransition:
        if target is self._stage:
            raise StateError("stage is already active", stage=target.value, reason=str(reason))
        if target is Stage.CIP:
            if self._stage not in CIP_ENTRY_STAGES:
                raise StageOrderError(
                    "cleaning cannot start while the line is running",
                    from_stage=self._stage.value,
                    to_stage=target.value,
                    permitted=sorted(stage.value for stage in CIP_ENTRY_STAGES),
                )
        elif self._stage is Stage.CIP:
            if target is not Stage.IDLE:
                raise StageOrderError(
                    "cleaning must finish before the line restarts",
                    from_stage=self._stage.value,
                    to_stage=target.value,
                )
        elif stage_index(target) != stage_index(self._stage) + 1:
            raise StageOrderError(
                "stage transition skipped or reversed the sequence",
                from_stage=self._stage.value,
                to_stage=target.value,
                expected=SEQUENCE[stage_index(self._stage) + 1].value if stage_index(self._stage) + 1 < len(SEQUENCE) else None,
            )
        return self._commit(self._stage, target, reason)

    def advance(self, *, reason: str) -> StageTransition:
        if self._stage is Stage.CIP:
            return self.enter(Stage.IDLE, reason=reason)
        if self._stage is Stage.COMPLETE:
            raise StateError("the line is complete; reset it before another run", stage=self._stage.value)
        return self.enter(SEQUENCE[stage_index(self._stage) + 1], reason=reason)

    def start_cleaning(self, *, reason: str) -> StageTransition:
        return self.enter(Stage.CIP, reason=reason)

    def reset(self, *, reason: str) -> StageTransition:
        if self._stage is Stage.IDLE:
            raise StateError("the line is already idle", stage=self._stage.value)
        return self._commit(self._stage, Stage.IDLE, reason)

    def _commit(self, source: Stage, target: Stage, reason: str) -> StageTransition:
        transition = StageTransition(
            sequence=len(self._history) + 1,
            from_stage=source.value,
            to_stage=target.value,
            reason=str(reason),
            timestamp=self.clock.timestamp(),
        )
        self._stage = target
        self._history.append(transition)
        self.persist()
        self.audit.record("stage", target.value, str(reason), cause=None)
        return transition

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage": self._stage.value,
            "index": self.index(),
            "transitions": len(self._history),
            "history": [item.as_dict() for item in self.history(10)],
        }


__all__ = ["CIP_ENTRY_STAGES", "SEQUENCE", "Stage", "StageMachine", "StageTransition", "stage_index"]
