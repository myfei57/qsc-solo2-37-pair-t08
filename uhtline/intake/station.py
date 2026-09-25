"""Intake station: screen a delivery, meter it and stage a receipt record."""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock
from ..core.config import ControlConfig, require_within
from ..core.ids import validate_token
from ..errors import RangeError, StateError
from ..persistence.audit import AuditLedger
from ..persistence.journal import RecordJournal
from ..persistence.store import DurableStore


class IntakeStation:
    """Validates one delivery and stages it on the production record stream."""

    document = "intake"

    def __init__(
        self,
        store: DurableStore,
        clock: Clock,
        config: ControlConfig,
        events: RecordJournal,
        audit: AuditLedger,
    ) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.events = events
        self.audit = audit
        self._total_litres = 0.0
        self._receipts: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        self._total_litres = float(stored.payload.get("total_litres", 0.0))
        self._receipts = [dict(item) for item in stored.payload.get("receipts", [])]

    def persist(self) -> None:
        self.store.write(self.document, {"total_litres": self._total_litres, "receipts": self._receipts})

    def screen(self, volume_litres: float, temperature_c: float) -> dict[str, Any]:
        """Report whether a delivery would be accepted, without changing state."""

        envelope = self.config.throughput
        reasons: list[str] = []
        try:
            volume = float(volume_litres)
        except (TypeError, ValueError):
            return {"accepted": False, "reasons": ["volume is not a number"]}
        if volume < envelope.intake_minimum_litres or volume > envelope.intake_maximum_litres:
            reasons.append("volume outside the intake window")
        if not 0.0 <= float(temperature_c) <= 25.0:
            reasons.append("receiving temperature outside the cold chain window")
        return {
            "accepted": not reasons,
            "reasons": reasons,
            "volume_litres": volume,
            "temperature_c": float(temperature_c),
            "window_litres": [envelope.intake_minimum_litres, envelope.intake_maximum_litres],
        }

    def register(
        self,
        volume_litres: float,
        temperature_c: float,
        *,
        batch_id: str,
        reason: str,
        key: str | None = None,
    ) -> dict[str, Any]:
        envelope = self.config.throughput
        volume = require_within(
            volume_litres,
            envelope.intake_minimum_litres,
            envelope.intake_maximum_litres,
            field_name="volume_litres",
            scope="throughput",
        )
        batch = validate_token(batch_id, field_name="batch id")
        temperature = float(temperature_c)
        if not 0.0 <= temperature <= 25.0:
            raise RangeError(
                "receiving temperature sits outside the cold chain window",
                field="temperature_c",
                value=temperature,
                minimum=0.0,
                maximum=25.0,
            )
        record = self.events.append(
            "intake",
            {
                "batch_id": batch,
                "volume_litres": volume,
                "temperature_c": temperature,
                "reason": str(reason),
            },
            key=key,
        )
        self._total_litres = round(self._total_litres + volume, 4)
        receipt = {
            "record_id": record.record_id,
            "batch_id": batch,
            "volume_litres": volume,
            "temperature_c": temperature,
            "reason": str(reason),
            "recorded_at": record.timestamp,
        }
        self._receipts.append(receipt)
        self.persist()
        self.audit.record("intake", batch, f"{volume:g} L accepted", cause=None)
        return {"receipt": receipt, "staged": record.as_dict(), "total_litres": self._total_litres}

    def require_receipt(self, batch_id: str) -> dict[str, Any]:
        for receipt in reversed(self._receipts):
            if receipt["batch_id"] == str(batch_id):
                return dict(receipt)
        raise StateError("no intake receipt covers this batch", batch=str(batch_id))

    def total_litres(self) -> float:
        return self._total_litres

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_litres": self._total_litres,
            "receipts": len(self._receipts),
            "latest": None if not self._receipts else dict(self._receipts[-1]),
            "recent": [dict(item) for item in self._receipts[-5:]],
        }


__all__ = ["IntakeStation"]
