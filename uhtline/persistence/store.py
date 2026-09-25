"""Durable JSON documents, append-only files and revision guards."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.clock import Clock
from ..errors import ConcurrencyConflictError, PersistenceError, ValidationError


def canonical_json(payload: Any) -> str:
    """Encode JSON deterministically so checksums stay stable across runs."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_checksum(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredDocument:
    """A materialised snapshot together with its optimistic revision."""

    name: str
    revision: int
    checksum: str
    updated_at: str
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, name: str, value: dict[str, Any]) -> "StoredDocument":
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise PersistenceError("stored document payload is not an object", document=name)
        revision = int(value.get("revision", 0))
        checksum = str(value.get("checksum", ""))
        expected = payload_checksum(payload)
        if checksum != expected:
            raise PersistenceError(
                "stored document checksum mismatch",
                document=name,
                revision=revision,
                expected=expected,
                actual=checksum,
            )
        return cls(
            name=name,
            revision=revision,
            checksum=checksum,
            updated_at=str(value.get("updated_at", "")),
            payload=payload,
        )

class DurableStore:
    """Filesystem store with atomic snapshots and replayable JSON-lines files."""

    def __init__(self, root: Path, clock: Clock) -> None:
        self.root = Path(root)
        self.clock = clock
        self._documents = self.root / "documents"
        self._journals = self.root / "journals"
        self._tmp = self.root / "tmp"
        self._lock = threading.RLock()
        for directory in (self.root, self._documents, self._journals, self._tmp):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_name(name: str) -> str:
        clean = str(name).strip().replace("\\", "/")
        if not clean or "/" in clean or clean in {".", ".."}:
            raise ValidationError("document name must be a simple non-empty token", name=name)
        return clean

    def _document_path(self, name: str) -> Path:
        return self._documents / f"{self._safe_name(name)}.json"

    def _journal_path(self, name: str) -> Path:
        return self._journals / f"{self._safe_name(name)}.jsonl"

    def read(self, name: str) -> StoredDocument:
        with self._lock:
            path = self._document_path(name)
            if not path.exists():
                raise PersistenceError("document does not exist", document=name)
            return self._read_path(path, name)

    def try_read(self, name: str) -> StoredDocument | None:
        with self._lock:
            path = self._document_path(name)
            if not path.exists():
                return None
            return self._read_path(path, name)

    def _read_path(self, path: Path, name: str) -> StoredDocument:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError("unable to read stored document", document=name, reason=str(exc)) from exc
        if not isinstance(raw, dict):
            raise PersistenceError("stored document is not an object", document=name)
        return StoredDocument.from_dict(name, raw)

    def exists(self, name: str) -> bool:
        return self._document_path(name).exists()

    def revision(self, name: str) -> int:
        current = self.try_read(name)
        return 0 if current is None else current.revision

    def write(self, name: str, payload: dict[str, Any]) -> StoredDocument:
        current = self.try_read(name)
        expected = 0 if current is None else current.revision
        return self.write_if(name, expected, payload)

    def write_if(self, name: str, expected_revision: int, payload: dict[str, Any]) -> StoredDocument:
        """Write only when the caller's revision still matches durable state."""

        if not isinstance(payload, dict):
            raise ValidationError("document payload must be an object", document=name)
        with self._lock:
            current = self.try_read(name)
            actual = 0 if current is None else current.revision
            if actual != expected_revision:
                raise ConcurrencyConflictError(
                    "document revision changed",
                    document=name,
                    expected=expected_revision,
                    actual=actual,
                )
            revision = actual + 1
            checksum = payload_checksum(payload)
            envelope = {
                "name": name,
                "revision": revision,
                "checksum": checksum,
                "updated_at": self.clock.timestamp(),
                "payload": payload,
            }
            self.atomic_write_json(self._document_path(name), envelope)
            return StoredDocument(
                name=name,
                revision=revision,
                checksum=checksum,
                updated_at=envelope["updated_at"],
                payload=payload,
            )

    def append_journal(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Append one immutable record and optionally compact the journal."""

        if not isinstance(payload, dict):
            raise ValidationError("journal payload must be an object", journal=name)
        with self._lock:
            records = self.read_journal(name)
            record = dict(payload)
            record.setdefault("journal_sequence", len(records) + 1)
            record.setdefault("journal_time", self.clock.timestamp())
            records.append(record)
            if limit is not None and limit > 0 and len(records) > limit:
                records = records[-limit:]
                for sequence, item in enumerate(records, start=1):
                    item["journal_sequence"] = sequence
            self.write_journal(name, records)
            return record

    def write_journal(self, name: str, records: list[dict[str, Any]]) -> None:
        text = "".join(canonical_json(item) + "\n" for item in records)
        with self._lock:
            self.atomic_write_text(self._journal_path(name), text)

    def read_journal(self, name: str, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            path = self._journal_path(name)
            if not path.exists():
                return []
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise PersistenceError("unable to read journal", journal=name, reason=str(exc)) from exc
            records: list[dict[str, Any]] = []
            for line_number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PersistenceError(
                        "journal record is not valid JSON",
                        journal=name,
                        line=line_number,
                        reason=str(exc),
                    ) from exc
                if not isinstance(value, dict):
                    raise PersistenceError("journal record is not an object", journal=name, line=line_number)
                records.append(value)
            if limit is None:
                return records
            return records[-max(0, limit) :]

    def journal_names(self) -> list[str]:
        return sorted(path.stem for path in self._journals.glob("*.jsonl"))

    def document_names(self) -> list[str]:
        return sorted(path.stem for path in self._documents.glob("*.json"))

    def atomic_write_json(self, path: Path, payload: Any) -> None:
        self.atomic_write_text(path, canonical_json(payload))

    def atomic_write_text(self, path: Path, text: str) -> None:
        temporary = self._tmp / f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            raise PersistenceError("unable to persist file", path=str(path), reason=str(exc)) from exc
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def inventory(self) -> list[dict[str, Any]]:
        with self._lock:
            inventory: list[dict[str, Any]] = []
            for path in sorted(self._documents.glob("*.json")):
                try:
                    document = self._read_path(path, path.stem)
                except PersistenceError as exc:
                    inventory.append({"name": path.stem, "valid": False, "error": exc.message})
                    continue
                inventory.append(
                    {
                        "name": document.name,
                        "valid": True,
                        "revision": document.revision,
                        "checksum": document.checksum,
                        "updated_at": document.updated_at,
                        "bytes": path.stat().st_size,
                    }
                )
            return inventory

    def stats(self) -> dict[str, Any]:
        documents = self.inventory()
        journals = self.journal_names()
        return {
            "root": str(self.root),
            "documents": len(documents),
            "invalid_documents": sum(1 for item in documents if not item["valid"]),
            "journals": len(journals),
            "journal_records": sum(len(self.read_journal(name)) for name in journals),
            "document_bytes": sum(int(item.get("bytes", 0)) for item in documents),
        }

    def backup(self, destination: Path) -> dict[str, Any]:
        """Copy every durable file into a fresh backup directory."""

        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        with self._lock:
            for source in sorted(self.root.rglob("*")):
                if not source.is_file() or self._tmp in source.parents:
                    continue
                relative = source.relative_to(self.root)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                copied.append(relative.as_posix())
        manifest = {
            "created_at": self.clock.timestamp(),
            "source": str(self.root),
            "destination": str(destination),
            "files": copied,
        }
        (destination / "backup-manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
        return manifest

    def restore(self, source: Path) -> dict[str, Any]:
        """Replace documents and journals with a verified backup image."""

        source = Path(source)
        manifest_path = source / "backup-manifest.json"
        if not manifest_path.exists():
            raise PersistenceError("backup manifest is missing", source=str(source))
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError("backup manifest is unreadable", source=str(source), reason=str(exc)) from exc
        files = manifest.get("files")
        if not isinstance(files, list):
            raise PersistenceError("backup manifest does not list files", source=str(source))
        with self._lock:
            for relative in files:
                if not (source / str(relative)).exists():
                    raise PersistenceError("backup file is missing", file=str(relative))
            for path in list(self._documents.glob("*.json")) + list(self._journals.glob("*.jsonl")):
                path.unlink()
            for relative in files:
                candidate = source / str(relative)
                target = self.root / str(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(candidate.read_bytes())
        return {
            "restored": len(files),
            "source": str(source),
            "manifest_created_at": manifest.get("created_at"),
        }


__all__ = ["DurableStore", "StoredDocument", "canonical_json", "payload_checksum"]
