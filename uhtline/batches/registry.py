"""Batch registry: one live batch, unique identifiers, explicit outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.clock import Clock
from ..core.ids import validate_token
from ..errors import DuplicateError, StateError, ValidationError
from ..persistence.audit import AuditLedger
from ..persistence.store import DurableStore

OPEN = "open"
CLOSED = "closed"


@dataclass(frozen=True)
class BatchRecord:
    batch_id: str
    product: str
    state: str
    opened_at: str
    closed_at: str | None
    outcome: str | None
    reason: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BatchRecord":
        return cls(
            batch_id=str(value["batch_id"]),
            product=str(value.get("product", "")),
            state=str(value.get("state", OPEN)),
            opened_at=str(value.get("opened_at", "")),
            closed_at=None if value.get("closed_at") is None else str(value["closed_at"]),
            outcome=None if value.get("outcome") is None else str(value["outcome"]),
            reason=str(value.get("reason", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "product": self.product,
            "state": self.state,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "outcome": self.outcome,
            "reason": self.reason,
        }


class BatchRegistry:
    """Refuses a repeated identifier and refuses two simultaneous open batches."""

    document = "batches"

    def __init__(self, store: DurableStore, clock: Clock, audit: AuditLedger) -> None:
        self.store = store
        self.clock = clock
        self.audit = audit
        self._batches: dict[str, BatchRecord] = {}
        self._load()

    def _load(self) -> None:
        stored = self.store.try_read(self.document)
        if stored is None:
            return
        for item in stored.payload.get("batches", []):
            record = BatchRecord.from_dict(item)
            self._batches[record.batch_id] = record

    def persist(self) -> None:
        self.store.write(
            self.document,
            {"batches": [self._batches[key].as_dict() for key in sorted(self._batches)]},
        )

    def open(self, batch_id: str, product: str, *, reason: str) -> BatchRecord:
        label = validate_token(batch_id, field_name="batch id")
        if label in self._batches:
            raise DuplicateError(
                "batch identifier was already used",
                batch=label,
                state=self._batches[label].state,
                opened_at=self._batches[label].opened_at,
            )
        live = self.active()
        if live is not None:
            raise StateError("another batch is still open", batch=live.batch_id, state=live.state)
        record = BatchRecord(
            batch_id=label,
            product=validate_token(product, field_name="product"),
            state=OPEN,
            opened_at=self.clock.timestamp(),
            closed_at=None,
            outcome=None,
            reason=str(reason),
        )
        self._batches[label] = record
        self.persist()
        self.audit.record("batch-open", label, str(reason), cause=None)
        return record

    def close(self, batch_id: str, outcome: str, *, reason: str) -> BatchRecord:
        label = str(batch_id)
        current = self._batches.get(label)
        if current is None:
            raise StateError("batch is not registered", batch=label)
        if current.state != OPEN:
            raise StateError("batch is already closed", batch=label, state=current.state)
        record = BatchRecord(
            batch_id=current.batch_id,
            product=current.product,
            state=CLOSED,
            opened_at=current.opened_at,
            closed_at=self.clock.timestamp(),
            outcome=validate_token(outcome, field_name="outcome"),
            reason=str(reason),
        )
        self._batches[label] = record
        self.persist()
        self.audit.record("batch-close", label, str(outcome), cause=None)
        return record

    def get(self, batch_id: str) -> BatchRecord | None:
        return self._batches.get(str(batch_id))

    def active(self) -> BatchRecord | None:
        for key in sorted(self._batches):
            if self._batches[key].state == OPEN:
                return self._batches[key]
        return None

    def batches(self, *, state: str | None = None, product: str | None = None, limit: int = 50) -> list[BatchRecord]:
        selected = [self._batches[key] for key in sorted(self._batches)]
        if state is not None:
            selected = [record for record in selected if record.state == str(state)]
        if product is not None:
            selected = [record for record in selected if record.product == str(product)]
        return selected[-max(0, int(limit)) :]

    def count(self) -> int:
        return len(self._batches)

    def snapshot(self) -> dict[str, Any]:
        active = self.active()
        return {
            "batches": len(self._batches),
            "active": None if active is None else active.as_dict(),
            "open_count": sum(1 for record in self._batches.values() if record.state == OPEN),
            "by_product": {
                product: sum(1 for record in self._batches.values() if record.product == product)
                for product in sorted({record.product for record in self._batches.values()})
            },
        }

    def validate_outcome(self, outcome: str) -> str:
        value = validate_token(outcome, field_name="outcome")
        if value not in {"released", "held", "rejected"}:
            raise ValidationError("unknown batch outcome", outcome=value)
        return value


__all__ = ["CLOSED", "OPEN", "BatchRecord", "BatchRegistry"]
