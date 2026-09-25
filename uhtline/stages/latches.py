"""Latches that stay set until their clear condition is satisfied."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..core.clock import Clock
from ..core.ids import validate_token
from ..errors import LatchActiveError, NotFoundError
from ..persistence.store import DurableStore

STERILIZATION_INTERLOCK = "sterilization-interlock"
HOLD_BYPASS = "hold-bypass"
CIP_ALARM = "cip-alarm"
ASEPTIC_PRESSURE = "aseptic-pressure"


@dataclass(frozen=True)
class LatchState:
    name: str
    description: str
    clear_condition: str
    active: bool
    reason: str
    detail: str
    set_at: str | None
    cleared_at: str | None
    set_count: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LatchState":
        return cls(
            name=str(value["name"]),
            description=str(value.get("description", "")),
            clear_condition=str(value.get("clear_condition", "")),
            active=bool(value.get("active", False)),
            reason=str(value.get("reason", "")),
            detail=str(value.get("detail", "")),
            set_at=None if value.get("set_at") is None else str(value["set_at"]),
            cleared_at=None if value.get("cleared_at") is None else str(value["cleared_at"]),
            set_count=int(value.get("set_count", 0)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "clear_condition": self.clear_condition,
            "active": self.active,
            "reason": self.reason,
            "detail": self.detail,
            "set_at": self.set_at,
            "cleared_at": self.cleared_at,
            "set_count": self.set_count,
        }


class LatchBoard:
    """A latch remembers why it tripped and refuses to clear without evidence."""

    document = "latches"

    def __init__(self, store: DurableStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self._latches: dict[str, LatchState] = {}
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        for item in stored.payload.get("latches", []):
            latch = LatchState.from_dict(item)
            self._latches[latch.name] = latch

    def persist(self) -> None:
        self.store.write(
            self.document,
            {"latches": [self._latches[key].as_dict() for key in sorted(self._latches)]},
        )

    def define(self, name: str, *, description: str, clear_condition: str) -> LatchState:
        key = validate_token(name, field_name="latch name")
        existing = self._latches.get(key)
        if existing is not None:
            return existing
        latch = LatchState(
            name=key,
            description=str(description),
            clear_condition=str(clear_condition),
            active=False,
            reason="",
            detail="",
            set_at=None,
            cleared_at=None,
            set_count=0,
        )
        self._latches[key] = latch
        self.persist()
        return latch

    def set(self, name: str, *, reason: str, detail: str = "") -> LatchState:
        current = self._require_defined(name)
        if current.active:
            return current
        latch = LatchState(
            name=current.name,
            description=current.description,
            clear_condition=current.clear_condition,
            active=True,
            reason=str(reason),
            detail=str(detail),
            set_at=self.clock.timestamp(),
            cleared_at=current.cleared_at,
            set_count=current.set_count + 1,
        )
        self._latches[latch.name] = latch
        self.persist()
        return latch

    def clear(self, name: str, *, reason: str, satisfied: bool, detail: str = "") -> LatchState:
        current = self._require_defined(name)
        if not current.active:
            return current
        if not satisfied:
            raise LatchActiveError(
                "the clear condition is not satisfied",
                latch=current.name,
                clear_condition=current.clear_condition,
                reason=str(reason),
            )
        latch = LatchState(
            name=current.name,
            description=current.description,
            clear_condition=current.clear_condition,
            active=False,
            reason=str(reason),
            detail=str(detail),
            set_at=current.set_at,
            cleared_at=self.clock.timestamp(),
            set_count=current.set_count,
        )
        self._latches[latch.name] = latch
        self.persist()
        return latch

    def require_clear(self, name: str, *, action: str = "") -> LatchState:
        latch = self._require_defined(name)
        if latch.active:
            raise LatchActiveError(
                "a latch is still set",
                latch=latch.name,
                action=str(action),
                reason=latch.reason,
                clear_condition=latch.clear_condition,
            )
        return latch

    def is_active(self, name: str) -> bool:
        latch = self._latches.get(str(name))
        return bool(latch is not None and latch.active)

    def _require_defined(self, name: str) -> LatchState:
        key = str(name)
        latch = self._latches.get(key)
        if latch is None:
            raise NotFoundError("latch is not defined on this line", latch=key)
        return latch

    def active(self) -> list[dict[str, Any]]:
        return [self._latches[key].as_dict() for key in sorted(self._latches) if self._latches[key].active]

    def inventory(self) -> list[dict[str, Any]]:
        return [self._latches[key].as_dict() for key in sorted(self._latches)]

    def state(self) -> dict[str, bool]:
        return {key: self._latches[key].active for key in sorted(self._latches)}


__all__ = ["LatchBoard", "LatchState"]
