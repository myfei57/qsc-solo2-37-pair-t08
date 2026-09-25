"""Preheat section: hold a target and hand over only once the value is durable."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig, require_within
from ..errors import StateError
from ..instrumentation.thermometry import Thermometry
from ..persistence.audit import AuditLedger
from ..persistence.store import DurableStore
from ..stages import gates as gate_names
from ..stages.gates import GateBoard


class PreheatSection:
    """Tracks the heating target and persists the temperature before any ramp."""

    document = "preheat"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        gates: GateBoard,
        thermometry: Thermometry,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.gates = gates
        self.thermometry = thermometry
        self.audit = audit
        self._target_c = config.temperature.preheat_target_c
        self._durable: dict[str, Any] | None = None
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._target_c = float(stored.payload.get("target_c", self.config.temperature.preheat_target_c))
        durable = stored.payload.get("durable")
        self._durable = None if durable is None else dict(durable)
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {"target_c": self._target_c, "durable": self._durable, "history": self._history},
        )

    @property
    def target_c(self) -> float:
        return self._target_c

    def set_target(self, target_c: float, *, reason: str) -> dict[str, Any]:
        envelope = self.config.temperature
        target = require_within(
            target_c,
            envelope.preheat_target_c - envelope.preheat_tolerance_c,
            envelope.preheat_target_c + envelope.preheat_tolerance_c,
            field_name="target_c",
            scope="temperature",
        )
        self._target_c = target
        entry = {"action": "target", "target_c": target, "reason": str(reason), "timestamp": self.clock.timestamp()}
        self._history.append(entry)
        self.persist()
        self.audit.record("preheat-target", "preheat", f"target {target:g} C", cause=None)
        return dict(entry)

    def measure(self, sensor_id: str, raw_c: float, *, reason: str) -> dict[str, Any]:
        reading = self.thermometry.record_reading(sensor_id, raw_c)
        return {
            "sensor": sensor_id,
            "value_c": reading.value,
            "generation": reading.generation,
            "in_spec": self.config.temperature.preheat_in_spec(reading.value),
            "reason": str(reason),
        }

    def persist_temperature(self, sensor_id: str, raw_c: float, *, reason: str) -> dict[str, Any]:
        """Durably record the preheat temperature and open the ramp permit."""

        measured = self.measure(sensor_id, raw_c, reason=reason)
        record = {
            "sensor": measured["sensor"],
            "value_c": measured["value_c"],
            "generation": measured["generation"],
            "in_spec": measured["in_spec"],
            "reason": str(reason),
            "recorded_at": self.clock.timestamp(),
        }
        self._durable = record
        self._history.append({"action": "persist", **record})
        self.persist()
        if measured["in_spec"]:
            self.gates.open(
                gate_names.TEMPERATURE_DURABLE,
                reason=str(reason),
                evidence=f"{measured['value_c']:g}C",
            )
        else:
            self.gates.close(gate_names.TEMPERATURE_DURABLE, reason="preheat temperature outside the window")
        self.audit.record(
            "preheat-persist",
            str(measured["sensor"]),
            f"{measured['value_c']:g} C durable={measured['in_spec']}",
            cause=None,
        )
        return dict(record)

    def durable_temperature(self) -> dict[str, Any]:
        if self._durable is None:
            raise StateError("no preheat temperature has been made durable", section="preheat")
        return dict(self._durable)

    def is_durable(self) -> bool:
        return self._durable is not None

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def snapshot(self) -> dict[str, Any]:
        return {
            "target_c": self.target_c,
            "durable": self._durable,
            "permit_open": self.gates.is_open(gate_names.TEMPERATURE_DURABLE),
            "history": self.history(5),
        }


__all__ = ["PreheatSection"]
