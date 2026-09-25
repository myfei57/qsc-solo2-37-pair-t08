"""Aseptic tank: no fill before a valid sterilization confirmation."""

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
from ..versioning.warranties import WarrantyBook


class AsepticTank:
    """Holds the sterile boundary: fill is gated and pressure is latched."""

    document = "aseptic-tank"

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
        self._sterile = False
        self._volume_litres = 0.0
        self._pressure_kpa = 0.0
        self._fills: list[dict[str, Any]] = []
        self._history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._sterile = bool(stored.payload.get("sterile", False))
        self._volume_litres = float(stored.payload.get("volume_litres", 0.0))
        self._pressure_kpa = float(stored.payload.get("pressure_kpa", 0.0))
        self._fills = [dict(item) for item in stored.payload.get("fills", [])]
        self._history = [dict(item) for item in stored.payload.get("history", [])]

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "sterile": self._sterile,
                "volume_litres": self._volume_litres,
                "pressure_kpa": self._pressure_kpa,
                "fills": self._fills,
                "history": self._history,
            },
        )

    def is_sterile(self) -> bool:
        return self._sterile

    def volume_litres(self) -> float:
        return self._volume_litres

    def pressure_kpa(self) -> float:
        return self._pressure_kpa

    def sterilize(self, *, confirmation_id: str, reason: str) -> dict[str, Any]:
        """Accept a confirmation; a superseded one is refused here."""

        confirmation = self.warranties.require_confirmation(
            confirmation_id,
            scope="sensors",
            subject="sterilization",
        )
        self._sterile = True
        self.gates.open(
            gate_names.ASEPTIC_STERILE,
            reason=str(reason),
            evidence=confirmation.confirmation_id,
        )
        record = self.events.append(
            "aseptic-sterilize",
            {"confirmation_id": confirmation.confirmation_id, "reason": str(reason)},
        )
        entry = {
            "action": "sterilize",
            "confirmation_id": confirmation.confirmation_id,
            "record_id": record.record_id,
            "reason": str(reason),
            "timestamp": self.clock.timestamp(),
        }
        self._history.append(entry)
        self.persist()
        self.audit.record("aseptic-sterilize", "aseptic", confirmation.confirmation_id, cause=None)
        return {"confirmation": confirmation.as_dict(), "event": entry}

    def fill(self, volume_litres: float, *, reason: str, key: str | None = None) -> dict[str, Any]:
        self.gates.require_open(gate_names.ASEPTIC_STERILE, action="aseptic-fill")
        self.latches.require_clear(latch_names.ASEPTIC_PRESSURE, action="aseptic-fill")
        envelope = self.config.throughput
        volume = require_within(
            volume_litres,
            envelope.aseptic_minimum_fill_litres,
            envelope.aseptic_capacity_litres,
            field_name="volume_litres",
            scope="throughput",
        )
        projected = round(self._volume_litres + volume, 4)
        if projected > envelope.aseptic_capacity_litres:
            raise RangeError(
                "fill would overflow the aseptic tank",
                field="volume_litres",
                volume_litres=self._volume_litres,
                projected_litres=projected,
                capacity_litres=envelope.aseptic_capacity_litres,
            )
        record = self.events.append(
            "aseptic-fill",
            {"volume_litres": volume, "reason": str(reason)},
            key=key,
        )
        self._volume_litres = projected
        fill = {
            "record_id": record.record_id,
            "volume_litres": volume,
            "level_litres": projected,
            "reason": str(reason),
            "recorded_at": record.timestamp,
        }
        self._fills.append(fill)
        self._history.append({"action": "fill", **fill})
        self.persist()
        self.audit.record("aseptic-fill", "aseptic", f"{volume:g} L", cause=None)
        return {"fill": fill, "staged": record.as_dict(), "level_litres": projected}

    def pressurize(self, value_kpa: float, *, reason: str) -> dict[str, Any]:
        value = require_within(value_kpa, 0.0, 150.0, field_name="pressure_kpa", scope="pressure")
        self._pressure_kpa = value
        in_spec = self.config.pressure.aseptic_in_spec(value)
        if in_spec:
            latch = self.latches.clear(
                latch_names.ASEPTIC_PRESSURE,
                reason=reason,
                satisfied=True,
                detail=f"{value:g} kPa recovered",
            )
        else:
            latch = self.latches.set(
                latch_names.ASEPTIC_PRESSURE,
                reason="aseptic pressure outside the sterile band",
                detail=f"{value:g} kPa",
            )
        self._history.append(
            {"action": "pressure", "pressure_kpa": value, "in_spec": in_spec, "reason": str(reason)}
        )
        self.persist()
        self.audit.record("aseptic-pressure", "aseptic", f"{value:g} kPa in_spec={in_spec}", cause=None)
        return {"pressure_kpa": value, "in_spec": in_spec, "latch": latch.as_dict()}

    def cancel_sterile(self, *, reason: str) -> dict[str, Any]:
        if not self.is_sterile():
            raise StateError("the aseptic tank is not marked sterile", tank="aseptic")
        self._sterile = False
        gate = self.gates.close(gate_names.ASEPTIC_STERILE, reason=str(reason))
        self.persist()
        self.audit.record("aseptic-cancel", "aseptic", str(reason), cause=None)
        return gate.as_dict()

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history[-max(0, int(limit)) :]]

    def snapshot(self) -> dict[str, Any]:
        return {
            "sterile": self.is_sterile(),
            "volume_litres": self._volume_litres,
            "pressure_kpa": self.pressure_kpa(),
            "fills": len(self._fills),
            "pressure_latched": self.latches.is_active(latch_names.ASEPTIC_PRESSURE),
            "history": self.history(5),
        }


__all__ = ["AsepticTank"]
