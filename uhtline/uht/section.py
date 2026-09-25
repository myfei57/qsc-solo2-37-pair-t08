"""Sterilization section: ramp only on durable temperature, confirm before fill."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig, require_within
from ..errors import StateError
from ..persistence.audit import AuditLedger
from ..persistence.journal import RecordJournal
from ..persistence.store import DurableStore
from ..preheat.heater import PreheatSection
from ..stages import gates as gate_names
from ..stages import latches as latch_names
from ..stages.gates import GateBoard
from ..stages.latches import LatchBoard
from ..versioning.warranties import Confirmation, WarrantyBook


class UhtSection:
    """Owns the sterilization ramp, its permit gate and its confirmation."""

    document = "uht"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        gates: GateBoard,
        latches: LatchBoard,
        warranties: WarrantyBook,
        preheat: PreheatSection,
        events: RecordJournal,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.gates = gates
        self.latches = latches
        self.warranties = warranties
        self.preheat = preheat
        self.events = events
        self.audit = audit
        self._running = False
        self._target_c = config.temperature.sterilization_target_c
        self._confirmation_id: str | None = None
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._running = bool(stored.payload.get("running", False))
        self._target_c = float(stored.payload.get("target_c", self.config.temperature.sterilization_target_c))
        self._confirmation_id = stored.payload.get("confirmation_id")
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "running": self._running,
                "target_c": self._target_c,
                "confirmation_id": self._confirmation_id,
                "history": self._history,
            },
        )

    def is_running(self) -> bool:
        return self._running

    @property
    def target_c(self) -> float:
        return self._target_c

    @property
    def confirmation_id(self) -> str | None:
        return self._confirmation_id

    def start_ramp(self, target_c: float, *, reason: str) -> dict[str, Any]:
        """Refuse to ramp until the preheat temperature is durable on disk."""

        self.gates.require_open(gate_names.TEMPERATURE_DURABLE, action="sterilization-ramp")
        self.latches.require_clear(latch_names.STERILIZATION_INTERLOCK, action="sterilization-ramp")
        envelope = self.config.temperature
        target = require_within(
            target_c,
            envelope.sterilization_minimum_c,
            envelope.sterilization_maximum_c,
            field_name="target_c",
            scope="temperature",
        )
        durable = self.preheat.durable_temperature()
        record = self.events.append(
            "sterilization-ramp",
            {
                "target_c": target,
                "handover_c": durable["value_c"],
                "handover_generation": durable["generation"],
                "reason": str(reason),
            },
        )
        self._running = True
        self._target_c = target
        entry = {
            "action": "ramp",
            "target_c": target,
            "handover_c": durable["value_c"],
            "record_id": record.record_id,
            "reason": str(reason),
            "timestamp": self.clock.timestamp(),
        }
        self._history.append(entry)
        self.persist()
        self.audit.record("uht-ramp", "uht", f"target {target:g} C", cause=None)
        return dict(entry)

    def confirm_sterilization(self, *, reason: str, ttl_seconds: float | None = None) -> Confirmation:
        """Issue the confirmation that unlocks the aseptic fill."""

        if not self._running:
            raise StateError("the sterilization section is not running", section="uht")
        confirmation = self.warranties.issue_confirmation(
            "sensors",
            "sterilization",
            ttl_seconds=ttl_seconds,
            reason=reason,
        )
        self._confirmation_id = confirmation.confirmation_id
        self.gates.open(
            gate_names.STERILIZATION_CONFIRMED,
            reason=str(reason),
            evidence=confirmation.confirmation_id,
        )
        self.persist()
        self.audit.record("uht-confirm", "uht", confirmation.confirmation_id, cause=None)
        return confirmation

    def require_confirmation(self) -> Confirmation:
        if self._confirmation_id is None:
            self.gates.require_open(gate_names.STERILIZATION_CONFIRMED, action="aseptic-fill")
            raise StateError("the sterilization section has never been confirmed", section="uht")
        return self.warranties.require_confirmation(
            self._confirmation_id,
            scope="sensors",
            subject="sterilization",
        )

    def stop(self, *, reason: str) -> dict[str, Any]:
        if not self._running:
            raise StateError("the sterilization section is already stopped", section="uht")
        self._running = False
        self.gates.open(gate_names.STERILIZATION_STOPPED, reason=str(reason), evidence="stopped")
        record = self.events.append("sterilization-stop", {"reason": str(reason)})
        entry = {"action": "stop", "record_id": record.record_id, "reason": str(reason), "timestamp": self.clock.timestamp()}
        self._history.append(entry)
        self.persist()
        self.audit.record("uht-stop", "uht", str(reason), cause=None)
        return dict(entry)

    def trip(self, *, reason: str, detail: str = "") -> dict[str, Any]:
        latch = self.latches.set(latch_names.STERILIZATION_INTERLOCK, reason=reason, detail=detail)
        self.audit.record("uht-trip", "uht", str(reason), cause=None)
        return latch.as_dict()

    def recover(self, *, reason: str, confirmed: bool) -> dict[str, Any]:
        latch = self.latches.clear(
            latch_names.STERILIZATION_INTERLOCK,
            reason=reason,
            satisfied=bool(confirmed),
            detail="sterilization section re-verified",
        )
        self.audit.record("uht-recover", "uht", str(reason), cause=None)
        return latch.as_dict()

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "target_c": self.target_c,
            "confirmation_id": self._confirmation_id,
            "permit_open": self.gates.is_open(gate_names.STERILIZATION_CONFIRMED),
            "interlock": self.latches.is_active(latch_names.STERILIZATION_INTERLOCK),
            "history": self.history(5),
        }


__all__ = ["UhtSection"]
