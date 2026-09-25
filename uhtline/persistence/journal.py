"""Append-only record stream with a commit watermark, replay and tombstones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.clock import Clock
from ..errors import DuplicateError, NotFoundError, ValidationError, WatermarkError
from .store import DurableStore

TOMBSTONE_KIND = "tombstone"


@dataclass(frozen=True)
class JournalRecord:
    """One immutable entry of a record stream."""

    sequence: int
    record_id: str
    stream: str
    kind: str
    key: str | None
    payload: dict[str, Any]
    timestamp: str
    committed: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, committed: bool) -> "JournalRecord":
        return cls(
            sequence=int(value["sequence"]),
            record_id=str(value["record_id"]),
            stream=str(value["stream"]),
            kind=str(value["kind"]),
            key=None if value.get("key") is None else str(value["key"]),
            payload=dict(value.get("payload") or {}),
            timestamp=str(value["timestamp"]),
            committed=committed,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "record_id": self.record_id,
            "stream": self.stream,
            "kind": self.kind,
            "key": self.key,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }

    @property
    def is_tombstone(self) -> bool:
        return self.kind == TOMBSTONE_KIND

@dataclass(frozen=True)
class CommitState:
    """Watermark plus the visible, pending and superseded record counts."""

    stream: str
    watermark: int
    appended: int
    visible: int
    pending: int
    superseded: int
    committed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "watermark": self.watermark,
            "appended": self.appended,
            "visible": self.visible,
            "pending": self.pending,
            "superseded": self.superseded,
            "committed_at": self.committed_at,
        }


class RecordJournal:
    """A record stream that only becomes readable once its watermark covers it."""

    def __init__(self, store: DurableStore, clock: Clock, stream: str) -> None:
        self.store = store
        self.clock = clock
        self.stream = str(stream).strip()
        if not self.stream:
            raise ValidationError("record stream needs a name", stream=stream)
        self._journal = f"{self.stream}-records"
        self._records: list[dict[str, Any]] = []
        self._committed: set[int] = set()
        self.reload()

    # -- durable plumbing -------------------------------------------------

    def reload(self) -> tuple[int, int]:
        """Replay the durable file and rebuild the visibility index."""

        self._records = [dict(item) for item in self.store.read_journal(self._journal)]
        for entry in self._records:
            entry.pop("committed", None)
        # Records written by an earlier build carry no commit marker, so they are
        # taken as already committed instead of being replayed as staged work.
        self._committed = {int(entry["sequence"]) for entry in self._records}
        return self.watermark(), len(self._records)

    def _persist(self) -> None:
        self.store.write_journal(self._journal, self._records)

    # -- writes -----------------------------------------------------------

    def append(self, kind: str, payload: dict[str, Any], *, key: str | None = None) -> JournalRecord:
        """Stage one record; it stays invisible until the watermark covers it."""

        if not isinstance(payload, dict):
            raise ValidationError("record payload must be an object", stream=self.stream)
        label = str(kind).strip()
        if not label:
            raise ValidationError("record kind must not be empty", stream=self.stream)
        if key is not None and self.find_by_key(str(key)) is not None:
            raise DuplicateError("record key already present in the stream", stream=self.stream, key=str(key))
        sequence = len(self._records) + 1
        entry = {
            "sequence": sequence,
            "record_id": f"{_prefix(self.stream)}-{sequence:05d}",
            "stream": self.stream,
            "kind": label,
            "key": None if key is None else str(key),
            "payload": payload,
            "timestamp": self.clock.timestamp(),
        }
        self._records.append(entry)
        self._persist()
        return JournalRecord.from_dict(entry, committed=False)

    def tombstone(self, target: str, reason: str, *, key: str | None = None) -> JournalRecord:
        """Append a rollback record; the target disappears once the tombstone commits."""

        record = self.lookup(target)
        if record is None:
            raise NotFoundError("record to tombstone does not exist", stream=self.stream, record=target)
        if record.is_tombstone:
            raise ValidationError("a tombstone record cannot be tombstoned", stream=self.stream, record=target)
        return self.append(TOMBSTONE_KIND, {"target": target, "reason": reason}, key=key)

    def commit(self, through: int | None = None) -> CommitState:
        """Advance the watermark, never past the staged tail and never backwards."""

        latest = len(self._records)
        target = latest if through is None else int(through)
        if target > latest:
            raise WatermarkError(
                "watermark runs past the staged tail",
                stream=self.stream,
                requested=target,
                staged=latest,
            )
        current = self.watermark()
        if target < current:
            raise WatermarkError(
                "watermark cannot move backwards",
                stream=self.stream,
                requested=target,
                current=current,
            )
        for entry in self._records:
            if int(entry["sequence"]) <= target:
                self._committed.add(int(entry["sequence"]))
        return self.state()

    def rollback(self) -> int:
        """Discard the staged tail; the committed prefix is never rewritten."""

        watermark = self.watermark()
        discarded = len(self._records) - watermark
        if discarded > 0:
            self._records = self._records[:watermark]
            self._committed = {sequence for sequence in self._committed if sequence <= watermark}
            self._persist()
        return discarded

    # -- reads ------------------------------------------------------------

    def _record(self, entry: dict[str, Any]) -> JournalRecord:
        return JournalRecord.from_dict(entry, committed=int(entry["sequence"]) in self._committed)

    def watermark(self) -> int:
        return max(self._committed) if self._committed else 0

    def committed(self) -> list[JournalRecord]:
        return [self._record(entry) for entry in self._records if int(entry["sequence"]) in self._committed]

    def pending(self) -> list[JournalRecord]:
        return [self._record(entry) for entry in self._records if int(entry["sequence"]) not in self._committed]

    def superseded_ids(self) -> set[str]:
        covered: set[str] = set()
        for entry in self._records:
            if entry["kind"] != TOMBSTONE_KIND:
                continue
            target = (entry.get("payload") or {}).get("target")
            if target is not None:
                covered.add(str(target))
        return covered

    def visible(self) -> list[JournalRecord]:
        """Committed records that are neither tombstones nor superseded by one."""

        covered = self.superseded_ids()
        return [
            record
            for record in self.committed()
            if not record.is_tombstone and record.record_id not in covered
        ]

    def lookup(self, record_id: str) -> JournalRecord | None:
        for entry in self._records:
            if entry["record_id"] == record_id:
                return self._record(entry)
        return None

    def find_by_key(self, key: str) -> JournalRecord | None:
        for entry in self._records:
            if entry.get("key") == key:
                return self._record(entry)
        return None

    def of_kind(self, kind: str) -> list[JournalRecord]:
        return [record for record in self.visible() if record.kind == kind]

    def state(self) -> CommitState:
        return CommitState(
            stream=self.stream,
            watermark=self.watermark(),
            appended=len(self._records),
            visible=len(self.visible()),
            pending=len(self._records) - self.watermark(),
            superseded=len(self.superseded_ids()),
            committed_at=self.clock.timestamp(),
        )


def _prefix(stream: str) -> str:
    letters = "".join(character for character in stream.upper() if character.isalnum())
    return (letters or "REC")[:6]


__all__ = ["CommitState", "JournalRecord", "RecordJournal", "TOMBSTONE_KIND"]
