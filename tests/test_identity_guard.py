"""测试 Identity Guard — 完整性检测 + 记忆快照 + 状态包扩展"""

import json
import shutil
import tempfile
from pathlib import Path

from backend.core.identity.guard import (
    _run_integrity_checks,
    _detect_mass_override,
    _detect_identity_file_deletion,
    _detect_structure_collapse,
    _save_directory_skeleton,
)
from backend.core.identity.snapshot import (
    snapshot_tool_memories,
    restore_tool_memories,
    list_memory_snapshots,
)
from backend.core.operations.models import FileEntry


# ── 辅助（Windows 兼容，避免 pytest-asyncio tmp_path bug） ──

def _tmp():
    d = tempfile.mkdtemp()
    return Path(d)


def _rm(p: Path):
    shutil.rmtree(str(p), ignore_errors=True)


class _FakeProject:
    def __init__(self, integrity=None):
        self.integrity = integrity or {
            "enabled": True,
            "mass_override_threshold": 0.80,
            "identity_files": ["CLAUDE.md", ".claude/", ".gitignore"],
        }


def _ents(total: int, changed: int) -> list[FileEntry]:
    entries = []
    for i in range(total):
        status = "new" if i < changed else "same"
        entries.append(FileEntry(
            rel_path=f"src/file_{i}.py",
            status=status,
            workspace_hash=f"hash_{i}",
            backup_hash=f"hash_{i}" if status == "same" else "",
        ))
    return entries


# ── mass_override 检测 ──────────────────────────────────

def test_mass_override_triggers():
    p = _FakeProject()
    e = _ents(10, 8)
    r = _detect_mass_override(e, p)
    assert r is not None
    assert r["rule"] == "mass_override"
    assert r["changed_ratio"] == 0.8


def test_mass_override_no_trigger():
    r = _detect_mass_override(_ents(10, 3), _FakeProject())
    assert r is None


def test_mass_override_empty():
    assert _detect_mass_override([], _FakeProject()) is None


def test_mass_override_custom_threshold():
    p = _FakeProject({"mass_override_threshold": 0.50,
                      "identity_files": [], "enabled": True})
    r = _detect_mass_override(_ents(10, 6), p)
    assert r is not None


# ── identity_file_deletion 检测 ─────────────────────────

def test_identity_file_missing():
    p = _tmp()
    try:
        r = _detect_identity_file_deletion(p, _FakeProject())
        assert r is not None
        assert r["rule"] == "identity_file_deleted"
        assert "CLAUDE.md" in r["missing_files"]
    finally:
        _rm(p)


def test_identity_file_present():
    p = _tmp()
    try:
        (p / "CLAUDE.md").write_text("# test")
        (p / ".claude").mkdir()
        (p / ".gitignore").write_text("x")
        assert _detect_identity_file_deletion(p, _FakeProject()) is None
    finally:
        _rm(p)


def test_identity_file_custom_list():
    p = _tmp()
    try:
        project = _FakeProject({"enabled": True, "identity_files": ["README.md"]})
        r = _detect_identity_file_deletion(p, project)
        assert r is not None
        assert "README.md" in r["missing_files"]
        assert "CLAUDE.md" not in str(r["missing_files"])
    finally:
        _rm(p)


# ── structure_collapse 检测 ─────────────────────────────

def test_structure_collapse_no_baseline():
    assert _detect_structure_collapse(_ents(10, 3), Path("/nonexistent")) is None


def test_structure_collapse_triggers():
    p = _tmp()
    try:
        (p / ".gitgo").mkdir()
        (p / ".gitgo" / "directory_skeleton.json").write_text(
            json.dumps({"dirs": ["src", "tests", "docs"], "files": ["README.md"]}))
        entries = [
            FileEntry(rel_path="lib/module.py", status="new", workspace_hash="a", backup_hash=""),
            FileEntry(rel_path="bin/cli.py", status="new", workspace_hash="b", backup_hash=""),
        ]
        r = _detect_structure_collapse(entries, p)
        assert r is not None
        assert r["rule"] == "structure_collapse"
        assert r["jaccard"] < 0.3
    finally:
        _rm(p)


def test_structure_collapse_similar():
    p = _tmp()
    try:
        (p / ".gitgo").mkdir()
        (p / ".gitgo" / "directory_skeleton.json").write_text(
            json.dumps({"dirs": ["src", "tests"], "files": ["README.md"]}))
        entries = [
            FileEntry(rel_path="src/main.py", status="modified", workspace_hash="a", backup_hash="a"),
        ]
        assert _detect_structure_collapse(entries, p) is None
    finally:
        _rm(p)


def test_structure_collapse_corrupt():
    p = _tmp()
    try:
        (p / ".gitgo").mkdir()
        (p / ".gitgo" / "directory_skeleton.json").write_text("bad json")
        assert _detect_structure_collapse(_ents(3, 1), p) is None
    finally:
        _rm(p)


# ── _run_integrity_checks ───────────────────────────────

def test_run_checks_all_pass():
    p = _tmp()
    try:
        (p / "CLAUDE.md").write_text("# t")
        (p / ".claude").mkdir()
        (p / ".gitignore").write_text("x")
        assert _run_integrity_checks(_ents(10, 2), p, _FakeProject()) == []
    finally:
        _rm(p)


def test_run_checks_mass_override():
    p = _tmp()
    try:
        (p / "CLAUDE.md").write_text("# t")
        (p / ".claude").mkdir()
        (p / ".gitignore").write_text("x")
        r = _run_integrity_checks(_ents(10, 9), p, _FakeProject())
        assert any(w["rule"] == "mass_override" for w in r)
    finally:
        _rm(p)


# ── _save_directory_skeleton ────────────────────────────

def test_save_skeleton():
    p = _tmp()
    try:
        (p / "src").mkdir()
        (p / "tests").mkdir()
        (p / "README.md").write_text("hi")
        _save_directory_skeleton(p)
        data = json.loads((p / ".gitgo" / "directory_skeleton.json").read_text())
        assert "src" in data["dirs"]
        assert "tests" in data["dirs"]
        assert "README.md" in data["files"]
    finally:
        _rm(p)


# ── Memory Snapshot ─────────────────────────────────────

def test_snapshot_and_restore():
    p = _tmp()
    ws = p / "ws"
    bp = p / "bp"
    ws.mkdir()
    bp.mkdir()
    try:
        (ws / ".claude").mkdir()
        (ws / ".claude" / "mem.json").write_text('{"k":"v"}')
        r = snapshot_tool_memories(ws, bp, _FakeProject())
        assert ".claude" in r["snapped"]

        shutil.rmtree(ws / ".claude")
        assert not (ws / ".claude").exists()

        restored = restore_tool_memories(bp, ws)
        assert ".claude" in restored["restored"]
        assert (ws / ".claude" / "mem.json").read_text() == '{"k":"v"}'
    finally:
        _rm(p)


def test_snapshot_incremental():
    import time
    p = _tmp()
    ws = p / "ws"
    bp = p / "bp"
    ws.mkdir()
    bp.mkdir()
    try:
        (ws / ".claude").mkdir()
        (ws / ".claude" / "m1.json").write_text("v1")
        snapshot_tool_memories(ws, bp, _FakeProject())
        time.sleep(1.1)  # 确保时间戳不同

        (ws / ".claude" / "m2.json").write_text("v2")
        snapshot_tool_memories(ws, bp, _FakeProject())

        snaps = list_memory_snapshots(bp)
        ts_set = {s["timestamp"] for s in snaps if s["source"] == ".claude"}
        assert len(ts_set) == 2
    finally:
        _rm(p)


def test_list_empty_snapshots():
    p = _tmp()
    bp = p / "bp"
    bp.mkdir()
    try:
        assert list_memory_snapshots(bp) == []
    finally:
        _rm(p)


def test_restore_no_snapshots():
    p = _tmp()
    ws = p / "ws"
    bp = p / "bp"
    ws.mkdir()
    bp.mkdir()
    try:
        r = restore_tool_memories(bp, ws)
        assert "error" in r
    finally:
        _rm(p)
