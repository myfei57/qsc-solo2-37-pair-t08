"""Cleaning cycle: the pump starts only on a valid temperature confirmation."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig, require_within
from ..errors import RangeError, StateError
from ..persistence.audit import AuditLedger
from ..persistence.journal import RecordJournal
from ..persistence.store import DurableStore
from ..stages import gates as gate_names
from ..stages import latches as latch_names
from ..stages.gates import GateBoard
from ..stages.latches import LatchBoard
from ..versioning.warranties import Confirmation, WarrantyBook


class CipCycle:
    """Keeps the cleaning cycle behind its confirmation and its alarm latch."""

    document = "cip"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        gates: GateBoard,
        latches: LatchBoard,
        warranties: WarrantyBook,
        events: RecordJournal,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.gates = gates
        self.latches = latches
        self.warranties = warranties
        self.events = events
        self.audit = audit
        self._running = False
        self._temperature_c = 0.0
        self._confirmation_id: str | None = None
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._running = bool(stored.payload.get("running", False))
        self._temperature_c = float(stored.payload.get("temperature_c", 0.0))
        self._confirmation_id = stored.payload.get("confirmation_id")
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "running": self._running,
                "temperature_c": self._temperature_c,
                "confirmation_id": self._confirmation_id,
                "history": self._history,
            },
        )

    def is_running(self) -> bool:
        return self._running

    def confirm_temperature(self, value_c: float, *, reason: str, ttl_seconds: float | None = None) -> dict[str, Any]:
        envelope = self.config.cleaning
        value = require_within(
            value_c,
            envelope.wash_minimum_c,
            envelope.wash_maximum_c,
            field_name="temperature_c",
            scope="cleaning",
        )
        confirmation: Confirmation = self.warranties.issue_confirmation(
            "sensors",
            "cip-temperature",
            ttl_seconds=ttl_seconds,
            reason=reason,
        )
        self._temperature_c = value
        self._confirmation_id = confirmation.confirmation_id
        self.gates.open(
            gate_names.CIP_TEMPERATURE_CONFIRMED,
            reason=str(reason),
            evidence=confirmation.confirmation_id,
        )
        record = self.events.append(
            "cip-temperature-confirm",
            {"temperature_c": value, "confirmation_id": confirmation.confirmation_id, "reason": str(reason)},
        )
        entry = {
            "action": "confirm",
            "temperature_c": value,
            "confirmation_id": confirmation.confirmation_id,
            "record_id": record.record_id,
            "reason": str(reason),
            "timestamp": self.clock.timestamp(),
        }
        self._history.append(entry)
        self.persist()
        self.audit.record("cip-confirm", "cip", f"{value:g} C", cause=None)
        return {"confirmation": confirmation.as_dict(), "event": entry}

    def start_pump(self, *, flow_lph: float, reason: str) -> dict[str, Any]:
        self.latches.require_clear(latch_names.CIP_ALARM, action="cip-pump-start")
        self.gates.require_open(gate_names.CIP_TEMPERATURE_CONFIRMED, action="cip-pump-start")
        if self._confirmation_id is None:
            raise StateError("the cleaning temperature was never confirmed", section="cip")
        self.warranties.require_confirmation(
            self._confirmation_id,
            scope="sensors",
            subject="cip-temperature",
        )
        flow = require_within(
            flow_lph,
            self.config.cleaning.pump_minimum_lph,
            self.config.flow.maximum_litres_per_hour,
            field_name="flow_lph",
            scope="cleaning",
        )
        if self._running:
            raise StateError("the cleaning pump is already running", section="cip")
        self._running = True
        record = self.events.append("cip-pump-start", {"flow_lph": flow, "reason": str(reason)})
        entry = {
            "action": "pump-start",
            "flow_lph": flow,
            "record_id": record.record_id,
            "reason": str(reason),
            "timestamp": self.clock.timestamp(),
        }
        self._history.append(entry)
        self.persist()
        self.audit.record("cip-pump-start", "cip", f"{flow:g} L/h", cause=None)
        return dict(entry)

    def stop_pump(self, *, reason: str) -> dict[str, Any]:
        if not self._running:
            raise StateError("the cleaning pump is already stopped", section="cip")
        self._running = False
        record = self.events.append("cip-pump-stop", {"reason": str(reason)})
        entry = {"action": "pump-stop", "record_id": record.record_id, "reason": str(reason), "timestamp": self.clock.timestamp()}
        self._history.append(entry)
        self.persist()
        self.audit.record("cip-pump-stop", "cip", str(reason), cause=None)
        return dict(entry)

    def raise_alarm(self, *, reason: str, detail: str = "") -> dict[str, Any]:
        latch = self.latches.set(latch_names.CIP_ALARM, reason=reason, detail=detail)
        self.audit.record("cip-alarm", "cip", str(reason), cause=None)
        return latch.as_dict()

    def reset_alarm(self, *, value_c: float, reason: str) -> dict[str, Any]:
        """The alarm latch clears only against a freshly confirmed temperature."""

        envelope = self.config.cleaning
        in_spec = envelope.wash_in_spec(float(value_c))
        latch = self.latches.clear(
            latch_names.CIP_ALARM,
            reason=reason,
            satisfied=in_spec,
            detail=f"temperature_in_spec={in_spec}",
        )
        self._history.append(
            {"action": "alarm-reset", "value_c": float(value_c), "cleared": not latch.active, "reason": str(reason)}
        )
        self.persist()
        self.audit.record("cip-alarm-reset", "cip", f"cleared={not latch.active}", cause=None)
        return {"latch": latch.as_dict(), "temperature_in_spec": in_spec}

    def cycle_complete(self, *, elapsed_seconds: float, reason: str) -> dict[str, Any]:
        minimum = self.config.cleaning.minimum_cycle_seconds
        if float(elapsed_seconds) < minimum:
            raise RangeError(
                "cleaning cycle is shorter than the configured minimum",
                field="elapsed_seconds",
                value=float(elapsed_seconds),
                minimum=minimum,
            )
        record = self.events.append(
            "cip-cycle-complete",
            {"elapsed_seconds": float(elapsed_seconds), "reason": str(reason)},
        )
        entry = {
            "action": "cycle-complete",
            "elapsed_seconds": float(elapsed_seconds),
            "record_id": record.record_id,
            "reason": str(reason),
            "timestamp": self.clock.timestamp(),
        }
        self._history.append(entry)
        self.persist()
        self.audit.record("cip-complete", "cip", f"{elapsed_seconds:g} s", cause=None)
        return dict(entry)

    def alarm_active(self) -> bool:
        return self.latches.is_active(latch_names.CIP_ALARM)

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "temperature_c": self._temperature_c,
            "confirmation_id": self._confirmation_id,
            "alarm_active": self.alarm_active(),
            "permit_open": self.gates.is_open(gate_names.CIP_TEMPERATURE_CONFIRMED),
            "history": self.history(5),
        }


__all__ = ["CipCycle"]
