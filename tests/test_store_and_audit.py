"""Durable documents, journals and the tamper-evident audit chain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uhtline.errors import ConcurrencyConflictError, PersistenceError, ValidationError
from uhtline.persistence.audit import AuditLedger
from uhtline.persistence.store import canonical_json

from .support import manual_store


def test_writing_a_document_bumps_its_revision(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    first = store.write("sample", {"value": 1})
    second = store.write("sample", {"value": 2})
    assert (first.revision, second.revision) == (1, 2)
    assert store.revision("sample") == 2
    assert store.exists("sample") is True
    assert store.read("sample").payload == {"value": 2}


def test_writing_with_a_stale_revision_is_rejected(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    store.write("sample", {"value": 1})
    store.write("sample", {"value": 2})
    with pytest.raises(ConcurrencyConflictError):
        store.write_if("sample", 1, {"value": 3})


def test_a_tampered_document_is_detected_on_read(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    store.write("sample", {"value": 1})
    path = tmp_path / "documents" / "sample.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["value"] = 99
    path.write_text(canonical_json(envelope), encoding="utf-8")
    with pytest.raises(PersistenceError):
        store.read("sample")


def test_reading_a_missing_document_is_reported(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    assert store.try_read("absent") is None
    with pytest.raises(PersistenceError):
        store.read("absent")


def test_document_names_must_be_simple_tokens(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    with pytest.raises(ValidationError):
        store.write("../escape", {"value": 1})


def test_journal_records_are_appended_and_can_be_capped(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    for index in range(5):
        store.append_journal("events", {"index": index}, limit=3)
    records = store.read_journal("events")
    assert [record["index"] for record in records] == [2, 3, 4]
    assert [record["journal_sequence"] for record in records] == [1, 2, 3]
    assert store.journal_names() == ["events"]


def test_journal_requires_an_object_payload(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    with pytest.raises(ValidationError):
        store.append_journal("events", ["not", "an", "object"])  # type: ignore[arg-type]


def test_inventory_reports_a_corrupt_document(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    store.write("sample", {"value": 1})
    (tmp_path / "documents" / "broken.json").write_text("{not json", encoding="utf-8")
    inventory = {item["name"]: item for item in store.inventory()}
    assert inventory["sample"]["valid"] is True
    assert inventory["broken"]["valid"] is False
    assert store.stats()["invalid_documents"] == 1


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    store.write("sample", {"value": 1})
    store.append_journal("events", {"index": 0})
    manifest = store.backup(tmp_path / "backup")
    assert "documents/sample.json" in manifest["files"]
    store.write("sample", {"value": 2})
    restored = store.restore(tmp_path / "backup")
    assert restored["restored"] == len(manifest["files"])
    assert store.read("sample").payload == {"value": 1}


def test_restoring_without_a_manifest_is_rejected(tmp_path: Path) -> None:
    store, _ = manual_store(tmp_path)
    (tmp_path / "empty-backup").mkdir()
    with pytest.raises(PersistenceError):
        store.restore(tmp_path / "empty-backup")


def test_audit_chain_links_every_entry(tmp_path: Path) -> None:
    store, clock = manual_store(tmp_path)
    ledger = AuditLedger(store, clock)
    first = ledger.record("intake", "B-0001", "500 L accepted")
    second = ledger.record("ramp", "uht", "target 137 C", cause=first.entry_id)
    assert second.previous_hash == first.entry_hash
    assert ledger.verify() == {"valid": True, "entries": 2, "head": second.entry_hash}
    assert ledger.latest().entry_id == second.entry_id
    assert ledger.count("intake") == 1


def test_audit_detects_a_tampered_entry(tmp_path: Path) -> None:
    store, clock = manual_store(tmp_path)
    ledger = AuditLedger(store, clock)
    ledger.record("intake", "B-0001", "500 L accepted")
    ledger.record("ramp", "uht", "target 137 C")
    path = tmp_path / "journals" / "audit-ledger.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["detail"] = "rewritten"
    lines[0] = canonical_json(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reloaded = AuditLedger(store, clock)
    report = reloaded.verify()
    assert report["valid"] is False
    assert report["reason"] == "entry-hash-mismatch"


def test_audit_trail_walks_back_to_the_cause(tmp_path: Path) -> None:
    store, clock = manual_store(tmp_path)
    ledger = AuditLedger(store, clock)
    first = ledger.record("intake", "B-0001", "500 L accepted")
    second = ledger.record("ramp", "uht", "target 137 C", cause=first.entry_id)
    trail = ledger.trail(second.entry_id)
    assert [entry.action for entry in trail] == ["intake", "ramp"]
    assert ledger.get("aud-00000001") == first
    assert ledger.get("aud-00099999") is None


def test_audit_filters_entries_by_target_and_action(tmp_path: Path) -> None:
    store, clock = manual_store(tmp_path)
    ledger = AuditLedger(store, clock)
    ledger.record("intake", "B-0001", "500 L accepted")
    ledger.record("intake", "B-0002", "400 L accepted")
    ledger.record("ramp", "uht", "target 137 C")
    assert len(ledger.entries(target="B-0001")) == 1
    assert len(ledger.entries(action="intake")) == 2
    assert ledger.targets()["uht"]["count"] == 1
    assert ledger.size() == 3


def test_audit_rejects_an_empty_action(tmp_path: Path) -> None:
    store, clock = manual_store(tmp_path)
    ledger = AuditLedger(store, clock)
    with pytest.raises(ValidationError):
        ledger.record("   ", "B-0001", "detail")
