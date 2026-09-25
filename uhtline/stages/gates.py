"""Named permits that must be open before a dependent action may run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..core.clock import Clock
from ..core.ids import validate_token
from ..errors import GateClosedError, NotFoundError
from ..persistence.store import DurableStore

OPEN = "open"
CLOSED = "closed"

TEMPERATURE_DURABLE = "preheat-temperature-durable"
STERILIZATION_CONFIRMED = "sterilization-confirmed"
STERILIZATION_STOPPED = "sterilization-stopped"
COOLING_STOPPED = "cooling-stopped"
CIP_TEMPERATURE_CONFIRMED = "cip-temperature-confirmed"
ASEPTIC_STERILE = "aseptic-sterile"


@dataclass(frozen=True)
class Gate:
    name: str
    description: str
    state: str
    evidence: str
    revision: int
    changed_at: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Gate":
        return cls(
            name=str(value["name"]),
            description=str(value.get("description", "")),
            state=str(value["state"]),
            evidence=str(value.get("evidence", "")),
            revision=int(value.get("revision", 0)),
            changed_at=str(value.get("changed_at", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "state": self.state,
            "evidence": self.evidence,
            "revision": self.revision,
            "changed_at": self.changed_at,
        }

    @property
    def is_open(self) -> bool:
        return self.state == OPEN


class GateBoard:
    """Tracks which prerequisites currently permit a dependent action."""

    document = "gates"

    def __init__(self, store: DurableStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self._gates: dict[str, Gate] = {}
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        for item in stored.payload.get("gates", []):
            gate = Gate.from_dict(item)
            self._gates[gate.name] = gate

    def persist(self) -> None:
        self.store.write(self.document, {"gates": [self._gates[key].as_dict() for key in sorted(self._gates)]})

    def define(self, name: str, *, description: str) -> Gate:
        key = validate_token(name, field_name="gate name")
        existing = self._gates.get(key)
        if existing is not None:
            return existing
        gate = Gate(
            name=key,
            description=str(description),
            state=CLOSED,
            evidence="",
            revision=0,
            changed_at=self.clock.timestamp(),
        )
        self._gates[key] = gate
        self.persist()
        return gate

    def open(self, name: str, *, reason: str, evidence: str = "") -> Gate:
        current = self._require_defined(name)
        if current.is_open and current.evidence == str(evidence):
            return current
        gate = Gate(
            name=current.name,
            description=current.description,
            state=OPEN,
            evidence=str(evidence),
            revision=current.revision + 1,
            changed_at=self.clock.timestamp(),
        )
        self._gates[gate.name] = gate
        self.persist()
        return gate

    def close(self, name: str, *, reason: str) -> Gate:
        current = self._require_defined(name)
        gate = Gate(
            name=current.name,
            description=current.description,
            state=CLOSED,
            evidence=str(reason),
            revision=current.revision + 1,
            changed_at=self.clock.timestamp(),
        )
        self._gates[gate.name] = gate
        self.persist()
        return gate

    def is_open(self, name: str) -> bool:
        gate = self._gates.get(str(name))
        return bool(gate is not None and gate.is_open)

    def get(self, name: str) -> Gate:
        return self._require_defined(name)

    def require_open(self, name: str, *, action: str = "") -> Gate:
        gate = self._require_defined(name)
        if not gate.is_open:
            raise GateClosedError(
                "a required permit is still closed",
                gate=gate.name,
                action=str(action),
                state=gate.state,
                evidence=gate.evidence,
            )
        return gate

    def _require_defined(self, name: str) -> Gate:
        key = str(name)
        gate = self._gates.get(key)
        if gate is None:
            raise NotFoundError("gate is not defined on this line", gate=key)
        return gate

    def inventory(self) -> list[dict[str, Any]]:
        return [self._gates[key].as_dict() for key in sorted(self._gates)]

    def state(self) -> dict[str, str]:
        return {key: self._gates[key].state for key in sorted(self._gates)}


__all__ = ["CLOSED", "OPEN", "Gate", "GateBoard"]
