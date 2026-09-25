"""Tamper-evident ledger for operator commands and control decisions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ..core.clock import Clock
from ..errors import ValidationError
from .store import DurableStore, canonical_json

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    entry_id: str
    action: str
    target: str
    detail: str
    cause: str | None
    timestamp: str
    previous_hash: str
    entry_hash: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(value["sequence"]),
            entry_id=str(value["entry_id"]),
            action=str(value["action"]),
            target=str(value["target"]),
            detail=str(value["detail"]),
            cause=None if value.get("cause") is None else str(value["cause"]),
            timestamp=str(value["timestamp"]),
            previous_hash=str(value["previous_hash"]),
            entry_hash=str(value["entry_hash"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "entry_id": self.entry_id,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "cause": self.cause,
            "timestamp": self.timestamp,
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }


class AuditLedger:
    """Append-only hash chain persisted as a JSON-lines journal."""

    journal = "audit-ledger"

    def __init__(self, store: DurableStore, clock: Clock, limit: int = 5000) -> None:
        self.store = store
        self.clock = clock
        self.limit = limit
        self._entries = [AuditEntry.from_dict(item) for item in store.read_journal(self.journal)]

    @staticmethod
    def digest(entry: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()

    def record(
        self,
        action: str,
        target: str,
        detail: str,
        *,
        cause: str | None = None,
    ) -> AuditEntry:
        if not str(action).strip():
            raise ValidationError("audit action must not be empty")
        sequence = len(self._entries) + 1
        previous_hash = GENESIS_HASH if not self._entries else self._entries[-1].entry_hash
        body = {
            "sequence": sequence,
            "entry_id": f"aud-{sequence:08d}",
            "action": str(action),
            "target": str(target),
            "detail": str(detail),
            "cause": cause,
            "timestamp": self.clock.timestamp(),
            "previous_hash": previous_hash,
        }
        entry = AuditEntry(entry_hash=self.digest(body), **body)
        self._entries.append(entry)
        self.store.append_journal(self.journal, entry.as_dict(), limit=self.limit)
        if len(self._entries) > self.limit:
            self._entries = self._entries[-self.limit :]
        return entry

    def entries(
        self,
        *,
        target: str | None = None,
        action: str | None = None,
        since: str | None = None,
        limit: int = 200,
    ) -> list[AuditEntry]:
        selected = self._entries
        if target is not None:
            selected = [entry for entry in selected if entry.target == target]
        if action is not None:
            selected = [entry for entry in selected if entry.action == action]
        if since is not None:
            selected = [entry for entry in selected if entry.timestamp >= since]
        return selected[-max(0, limit) :]

    def count(self, action: str | None = None) -> int:
        if action is None:
            return len(self._entries)
        return sum(1 for entry in self._entries if entry.action == action)

    def get(self, entry_id: str) -> AuditEntry | None:
        for entry in reversed(self._entries):
            if entry.entry_id == entry_id:
                return entry
        return None

    def trail(self, entry_id: str) -> list[AuditEntry]:
        trail: list[AuditEntry] = []
        current = self.get(entry_id)
        seen: set[str] = set()
        while current is not None and current.entry_id not in seen:
            trail.append(current)
            seen.add(current.entry_id)
            current = None if current.cause is None else self.get(current.cause)
        return list(reversed(trail))

    def targets(self) -> dict[str, dict[str, Any]]:
        counts: dict[str, dict[str, Any]] = {}
        for entry in self._entries:
            item = counts.setdefault(entry.target, {"count": 0, "actions": {}, "latest": entry.timestamp})
            item["count"] += 1
            item["actions"][entry.action] = item["actions"].get(entry.action, 0) + 1
            item["latest"] = entry.timestamp
        return counts

    def verify(self) -> dict[str, Any]:
        previous = GENESIS_HASH
        for index, entry in enumerate(self._entries, start=1):
            body = entry.as_dict()
            stored_hash = body.pop("entry_hash")
            expected = self.digest(body)
            if entry.sequence != index:
                return {
                    "valid": False,
                    "reason": "sequence-gap",
                    "at": entry.entry_id,
                    "expected_sequence": index,
                    "actual_sequence": entry.sequence,
                }
            if entry.previous_hash != previous:
                return {
                    "valid": False,
                    "reason": "previous-hash-mismatch",
                    "at": entry.entry_id,
                    "expected": previous,
                    "actual": entry.previous_hash,
                }
            if stored_hash != expected:
                return {
                    "valid": False,
                    "reason": "entry-hash-mismatch",
                    "at": entry.entry_id,
                    "expected": expected,
                    "actual": stored_hash,
                }
            previous = stored_hash
        return {"valid": True, "entries": len(self._entries), "head": previous}

    def size(self) -> int:
        return len(self._entries)

    def latest(self) -> AuditEntry | None:
        return None if not self._entries else self._entries[-1]


__all__ = ["GENESIS_HASH", "AuditEntry", "AuditLedger"]
