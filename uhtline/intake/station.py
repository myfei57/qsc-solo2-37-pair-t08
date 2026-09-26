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

PENDING = "pending"
COMMITTED = "committed"


class IntakeStation:
    """Validates one delivery and stages it on the production record stream.

    A receipt is visible in the station as soon as it is recorded, but it only
    joins the official production total once its record passes the commit
    watermark (operator signature). Records staged but never signed survive a
    restart as pending receipts and wait for the next signature.
    """

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
        self._receipts: list[dict[str, Any]] = []
        self._load()

    # -- projection --------------------------------------------------------

    def _load(self) -> None:
        """Rebuild every receipt from the record stream, not the snapshot."""

        visible_ids = {record.record_id for record in self.events.of_kind("intake")}
        self._receipts = []
        for record in self.events.committed():
            if record.kind != "intake":
                continue
            receipt = self._receipt_from_record(record)
            receipt["status"] = COMMITTED if record.record_id in visible_ids else "voided"
            self._receipts.append(receipt)
        for record in self.events.pending():
            if record.kind != "intake":
                continue
            receipt = self._receipt_from_record(record)
            receipt["status"] = PENDING
            self._receipts.append(receipt)

    def reconcile(self) -> dict[str, Any]:
        """Re-project after the watermark moved (commit, rollback or void)."""

        self._load()
        self.persist()
        return self.snapshot()

    def _receipt_from_record(self, record: Any) -> dict[str, Any]:
        return {
            "record_id": record.record_id,
            "batch_id": str(record.payload["batch_id"]),
            "volume_litres": float(record.payload["volume_litres"]),
            "temperature_c": float(record.payload["temperature_c"]),
            "reason": str(record.payload.get("reason", "")),
            "recorded_at": record.timestamp,
        }

    def _with_status(self, status: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._receipts if item["status"] == status]

    def _litres(self, status: str) -> float:
        return round(sum(item["volume_litres"] for item in self._receipts if item["status"] == status), 4)

    def persist(self) -> None:
        self.store.write(
            self.document,
            {
                "total_litres": self.total_litres(),
                "receipts": [dict(item) for item in self._receipts if item["status"] == COMMITTED],
                "pending": self._with_status(PENDING),
            },
        )

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
        receipt = self._receipt_from_record(record)
        receipt["status"] = PENDING
        self._receipts.append(receipt)
        self.persist()
        self.audit.record("intake", batch, f"{volume:g} L accepted", cause=None)
        return {
            "receipt": dict(receipt),
            "staged": record.as_dict(),
            "total_litres": self.total_litres(),
            "pending_litres": self.pending_litres(),
        }

    def require_receipt(self, batch_id: str) -> dict[str, Any]:
        for receipt in reversed(self._receipts):
            if receipt["status"] == "voided":
                continue
            if receipt["batch_id"] == str(batch_id):
                return dict(receipt)
        raise StateError("no intake receipt covers this batch", batch=str(batch_id))

    def total_litres(self) -> float:
        """Official production total: signed receipts that have not been voided."""

        return self._litres(COMMITTED)

    def pending_litres(self) -> float:
        """Volume recorded but still waiting for the commit signature."""

        return self._litres(PENDING)

    def pending_receipts(self) -> list[dict[str, Any]]:
        return self._with_status(PENDING)

    def snapshot(self) -> dict[str, Any]:
        recent = [dict(item) for item in self._receipts if item["status"] != "voided"][-5:]
        return {
            "total_litres": self.total_litres(),
            "receipts": len(self._with_status(COMMITTED)),
            "latest": None if recent == [] else dict(recent[-1]),
            "recent": recent,
            "pending_receipts": len(self._with_status(PENDING)),
            "pending_litres": self.pending_litres(),
            "pending": self._with_status(PENDING),
        }


__all__ = ["COMMITTED", "PENDING", "IntakeStation"]
