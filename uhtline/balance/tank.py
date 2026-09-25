"""Balance tank: charge, drain and stop in the permitted order."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig, require_within
from ..errors import RangeError, StateError
from ..persistence.audit import AuditLedger
from ..persistence.store import DurableStore
from ..stages import gates as gate_names
from ..stages.gates import GateBoard


class BalanceTank:
    """Buffers product between intake and the heating sections."""

    document = "balance-tank"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        gates: GateBoard,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.gates = gates
        self.audit = audit
        self._level_litres = 0.0
        self._charging = False
        self._stopped = False
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._level_litres = float(stored.payload.get("level_litres", 0.0))
        self._charging = bool(stored.payload.get("charging", False))
        self._stopped = bool(stored.payload.get("stopped", False))
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "level_litres": self._level_litres,
                "charging": self._charging,
                "stopped": self._stopped,
                "history": self._history,
            },
        )

    def level_litres(self) -> float:
        return self._level_litres

    def is_charging(self) -> bool:
        return self._charging

    def is_stopped(self) -> bool:
        return self._stopped

    def charge(self, volume_litres: float, *, reason: str) -> dict[str, Any]:
        envelope = self.config.throughput
        if self._stopped:
            raise StateError("the balance tank is stopped; restart it first", tank="balance")
        volume = require_within(
            volume_litres,
            envelope.balance_minimum_litres,
            envelope.balance_maximum_litres,
            field_name="volume_litres",
            scope="throughput",
        )
        projected = round(self._level_litres + volume, 4)
        if projected > envelope.balance_capacity_litres:
            raise RangeError(
                "charge would overflow the balance tank",
                field="volume_litres",
                level_litres=self._level_litres,
                projected_litres=projected,
                capacity_litres=envelope.balance_capacity_litres,
            )
        self._level_litres = projected
        self._charging = True
        return self._commit("charge", volume, reason)

    def drain(self, volume_litres: float, *, reason: str) -> dict[str, Any]:
        volume = float(volume_litres)
        if volume <= 0:
            raise RangeError("drain volume must be positive", field="volume_litres", value=volume)
        if volume > self._level_litres:
            raise RangeError(
                "drain exceeds the current level",
                field="volume_litres",
                value=volume,
                level_litres=self._level_litres,
            )
        self._level_litres = round(self._level_litres - volume, 4)
        self._charging = self._level_litres > 0.0
        return self._commit("drain", volume, reason)

    def stop(self, *, reason: str) -> dict[str, Any]:
        """The tank may only stop once the cooling section has already stopped."""

        self.gates.require_open(gate_names.COOLING_STOPPED, action="balance-stop")
        if self._stopped:
            raise StateError("the balance tank is already stopped", tank="balance")
        self._stopped = True
        self._charging = False
        return self._commit("stop", 0.0, reason)

    def _commit(self, action: str, volume: float, reason: str) -> dict[str, Any]:
        entry = {
            "action": action,
            "volume_litres": round(float(volume), 4),
            "level_litres": self._level_litres,
            "reason": str(reason),
            "timestamp": self.clock.timestamp(),
        }
        self._history.append(entry)
        self.persist()
        self.audit.record(f"balance-{action}", "balance", str(reason), cause=None)
        return dict(entry)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def snapshot(self) -> dict[str, Any]:
        return {
            "level_litres": self._level_litres,
            "charging": self._charging,
            "stopped": self._stopped,
            "history": self.history(5),
        }


__all__ = ["BalanceTank"]
