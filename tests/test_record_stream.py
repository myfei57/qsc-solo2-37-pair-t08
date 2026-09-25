"""Contract: append-only records, commit watermark, replay and tombstones."""

from __future__ import annotations

from pathlib import Path

import pytest

from uhtline.app.runtime import build_runtime
from uhtline.core.clock import ManualClock
from uhtline.core.config import fast_test_config
from uhtline.errors import DuplicateError, NotFoundError, WatermarkError
from uhtline.persistence.journal import RecordJournal
from uhtline.persistence.store import DurableStore

from .support import manual_store


def stock_journal(tmp_path: Path) -> tuple[RecordJournal, DurableStore, ManualClock]:
    store, clock = manual_store(tmp_path)
    return RecordJournal(store, clock, "line-events"), store, clock


def test_staged_record_stays_invisible_until_the_watermark_covers_it(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    record = journal.append("intake", {"volume_litres": 500.0})
    assert journal.visible() == []
    assert [item.record_id for item in journal.pending()] == [record.record_id]
    journal.commit()
    assert [item.record_id for item in journal.visible()] == [record.record_id]
    assert journal.pending() == []


def test_commit_advances_the_watermark_to_the_requested_sequence(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    first = journal.append("intake", {"volume_litres": 500.0})
    journal.append("intake", {"volume_litres": 600.0})
    state = journal.commit(first.sequence)
    assert state.watermark == first.sequence
    assert state.pending == 1
    assert state.visible == 1


def test_watermark_cannot_move_backwards(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    journal.append("intake", {"volume_litres": 500.0})
    journal.append("intake", {"volume_litres": 600.0})
    journal.commit()
    with pytest.raises(WatermarkError):
        journal.commit(1)


def test_commit_beyond_the_staged_tail_is_rejected(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    journal.append("intake", {"volume_litres": 500.0})
    with pytest.raises(WatermarkError):
        journal.commit(9)


def test_restart_replays_only_records_below_the_watermark(tmp_path: Path) -> None:
    journal, store, clock = stock_journal(tmp_path)
    journal.append("intake", {"volume_litres": 500.0})
    journal.commit()
    journal.append("intake", {"volume_litres": 600.0})
    reloaded = RecordJournal(store, clock, "line-events")
    assert [item.payload["volume_litres"] for item in reloaded.visible()] == [500.0]
    assert [item.payload["volume_litres"] for item in reloaded.pending()] == [600.0]


def test_rollback_discards_only_the_staged_tail(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    journal.append("intake", {"volume_litres": 500.0})
    journal.commit()
    journal.append("intake", {"volume_litres": 600.0})
    discarded = journal.rollback()
    assert discarded == 1
    assert len(journal.visible()) == 1
    assert journal.state().appended == 1


def test_tombstone_hides_the_target_once_it_is_committed(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    record = journal.append("intake", {"volume_litres": 500.0})
    journal.commit()
    journal.tombstone(record.record_id, "operator rollback")
    journal.commit()
    assert journal.visible() == []
    assert journal.superseded_ids() == {record.record_id}


def test_uncommitted_tombstone_leaves_the_target_visible(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    record = journal.append("intake", {"volume_litres": 500.0})
    journal.commit()
    journal.tombstone(record.record_id, "operator rollback")
    assert [item.record_id for item in journal.visible()] == [record.record_id]


def test_duplicate_record_key_is_rejected(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    journal.append("intake", {"volume_litres": 500.0}, key="receipt-1")
    with pytest.raises(DuplicateError):
        journal.append("intake", {"volume_litres": 500.0}, key="receipt-1")


def test_tombstoning_an_unknown_record_is_rejected(tmp_path: Path) -> None:
    journal, _, _ = stock_journal(tmp_path)
    with pytest.raises(NotFoundError):
        journal.tombstone("LNE-99999", "operator rollback")


def test_service_replays_committed_records_and_keeps_the_staged_tail_on_restart(tmp_path: Path) -> None:
    first = build_runtime(fast_test_config(), tmp_path, ManualClock())
    first.control.start_intake(reason="test")
    first.control.receive(500.0, 6.0, batch_id="B-0100", reason="test", key="receipt-1")
    first.control.commit_records()
    first.control.receive(600.0, 6.0, batch_id="B-0100", reason="test", key="receipt-2")
    second = build_runtime(fast_test_config(), tmp_path, ManualClock())
    visible = [item["kind"] for item in second.control.visible_records()]
    pending = [item["kind"] for item in second.control.pending_records()]
    assert visible.count("intake") == 1
    assert pending.count("intake") == 1
    assert second.control.stream_state()["watermark"] == 1
