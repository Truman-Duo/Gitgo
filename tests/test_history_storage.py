from __future__ import annotations

import json
from dataclasses import asdict

from backend.core.history import HistoryEntry, HistoryManager
from backend.core.storage import StorageRuntime


def _entry(index: int) -> HistoryEntry:
    return HistoryEntry(
        timestamp=f"2026-08-25T00:00:{index:02d}",
        project_name="gitgo",
        operation=f"event_{index}",
        detail={"index": index},
    )


def test_save_is_atomic_jsonl_and_remains_appendable(tmp_path_factory):
    with StorageRuntime(
        tmp_path_factory, state_home=tmp_path_factory / "state",
    ) as storage:
        HistoryManager.set_workspace(str(tmp_path_factory), storage=storage)
        HistoryManager.save([_entry(1), _entry(2)])

        assert storage.history_count() == 2
        assert not HistoryManager._path().exists()

        HistoryManager.add_operation("gitgo", "event_3")
        assert [entry.operation for entry in HistoryManager.load()] == [
            "event_1", "event_2", "event_3",
        ]


def test_load_recovers_legacy_array_with_jsonl_tail(tmp_path_factory):
    path = tmp_path_factory / ".gitgo" / "gitgo_history.json"
    path.parent.mkdir(parents=True)
    legacy = json.dumps([asdict(_entry(1)), asdict(_entry(2))], indent=2)
    tail = json.dumps(asdict(_entry(3))) + "\n" + json.dumps(asdict(_entry(4))) + "\n"
    path.write_text(legacy + "\n" + tail, encoding="utf-8")

    with StorageRuntime(
        tmp_path_factory, state_home=tmp_path_factory / "state",
    ) as storage:
        HistoryManager.set_workspace(str(tmp_path_factory), storage=storage)
        assert [entry.operation for entry in HistoryManager.load()] == [
            "event_1", "event_2", "event_3", "event_4",
        ]
        assert not path.exists()
        assert list(path.parent.glob("gitgo_history.json.legacy-imported-*"))


def test_root_legacy_history_is_imported_and_archived_inside_gitgo_metadata(tmp_path_factory):
    old_root_file = tmp_path_factory / "gitgo_history.json"
    old_root_file.write_text(json.dumps([asdict(_entry(1))]), encoding="utf-8")
    with StorageRuntime(
        tmp_path_factory, state_home=tmp_path_factory / "state",
    ) as storage:
        HistoryManager.set_workspace(str(tmp_path_factory), storage=storage)
        assert [entry.operation for entry in HistoryManager.load()] == ["event_1"]
        assert not old_root_file.exists()
        assert list((tmp_path_factory / ".gitgo" / "legacy").glob(
            "gitgo_history.json.legacy-imported-*"
        ))


def test_compact_migrates_mixed_history_without_losing_recent_events(tmp_path_factory, monkeypatch):
    path = tmp_path_factory / ".gitgo" / "gitgo_history.json"
    path.parent.mkdir(parents=True)
    entries = [_entry(i) for i in range(10)]
    legacy = json.dumps([asdict(entry) for entry in entries[:7]], indent=2)
    tail = "\n".join(json.dumps(asdict(entry)) for entry in entries[7:]) + "\n"
    path.write_text(legacy + "\n" + tail, encoding="utf-8")
    with StorageRuntime(
        tmp_path_factory, state_home=tmp_path_factory / "state",
    ) as storage:
        HistoryManager.set_workspace(str(tmp_path_factory), storage=storage)
        monkeypatch.setattr("backend.core.history._MAX_ENTRIES", 4)
        HistoryManager._compact()

        assert [entry.operation for entry in HistoryManager.load()] == [
            "event_6", "event_7", "event_8", "event_9",
        ]
        assert not path.exists()
