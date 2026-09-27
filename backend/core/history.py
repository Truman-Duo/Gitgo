"""Authoritative operation history stored through the SQLite facade.

Legacy JSON/JSONL is imported idempotently and archived.  The public static
API remains compatible, but no call continues writing repository-local audit
files after the cutover.
"""

from __future__ import annotations

import json
import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.core.storage import StorageRuntime, bind_storage, get_storage

HISTORY_FILE = "gitgo_history.json"

_MAX_ENTRIES = 200        # 常驻上限
_COMPACT_THRESHOLD = 400  # 超过此行数触发 compact


@dataclass
class HistoryEntry:
    timestamp: str  # ISO format
    project_name: str
    operation: str = ""          # "scan" | "formalize" | "sync" | "push"
                                  # | "triage_accept" | "triage_promote" | "triage_discard"
                                  # | "delete_formal" | "dissolve_formal"
    status: str = "success"       # "success" | "failed" | "cancelled"
    detail: dict = field(default_factory=dict)  # 操作特定数据
    correlation_id: str = ""      # session 级关联 ID，同一次工作流的所有记录共享

    # ── L0: StateLog 2.0 关联字段 ──
    fact_refs: list[str] = field(default_factory=list)
    # 本 event 触发了哪些 fact 的派生。例：["fact_frequent_mod_dashboard_py"]
    tags: list[str] = field(default_factory=list)
    # 语义标签。例：["exploration", "abandoned", "rejected", "lesson_applied"]
    parent_event_id: str = ""
    # 前驱 event 的 correlation_id。例：rejection 的 parent 是被拒绝的 formalize

    # 保留旧字段向后兼容（add_entry 委托到 add_operation 内部填充）
    file_count: int = 0
    commit_hash: str = ""
    commit_message: str = ""
    workspace: str = ""
    backup: str = ""


class HistoryManager:
    """Manage the bounded audit stream through one project StorageRuntime."""

    _workspace_path: str | None = None
    _local = threading.local()
    _lock = threading.Lock()

    @classmethod
    @contextmanager
    def workspace_scope(cls, path: str):
        """Bind one operation without changing another thread's default project."""
        previous = getattr(cls._local, "workspace_path", None)
        cls._local.workspace_path = str(path)
        try:
            yield
        finally:
            if previous is None:
                cls._local.__dict__.pop("workspace_path", None)
            else:
                cls._local.workspace_path = previous

    @classmethod
    def set_workspace(
        cls, path: str, *, storage: StorageRuntime | None = None,
    ) -> None:
        """Select a workspace and optionally bind the daemon-owned runtime."""
        cls._workspace_path = path
        cls._local.workspace_path = path
        if storage is not None:
            bind_storage(path, storage)
            cls._migrate_legacy()

    @staticmethod
    def _path() -> Path:
        """Legacy import path retained for diagnostics and compatibility."""
        ws = getattr(
            HistoryManager._local, "workspace_path", None,
        ) or HistoryManager._workspace_path
        if ws:
            p = Path(ws) / ".gitgo" / HISTORY_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        # Unbound legacy diagnostics are still private Gitgo metadata; never
        # create another audit file beside user deliverables merely because a
        # launcher changed cwd.
        return Path.cwd() / ".gitgo" / HISTORY_FILE

    @classmethod
    def _legacy_paths(cls) -> list[Path]:
        canonical = cls._path()
        candidates = [canonical]
        ws = getattr(cls._local, "workspace_path", None) or cls._workspace_path
        if ws:
            old_root_file = Path(ws) / HISTORY_FILE
            if old_root_file != canonical:
                candidates.append(old_root_file)
        return candidates

    @classmethod
    def _storage(cls) -> StorageRuntime:
        ws = getattr(cls._local, "workspace_path", None) or cls._workspace_path
        ws = ws or str(Path.cwd())
        return get_storage(ws)

    @classmethod
    def _read_legacy_path(cls, path: Path) -> list[HistoryEntry]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            return cls._load_jsonl(raw)
        if stripped.startswith("["):
            return cls._load_json_array(raw)
        return []

    @classmethod
    def _migrate_legacy(cls) -> None:
        paths = [path for path in cls._legacy_paths() if path.exists()]
        if not paths:
            return
        with cls._lock:
            runtime = cls._storage()
            for path in paths:
                if not path.exists():
                    continue
                entries = cls._read_legacy_path(path)
                try:
                    source = str(path.resolve()).encode("utf-8", errors="replace")
                    for index, entry in enumerate(entries):
                        record = asdict(entry)
                        encoded = json.dumps(
                            record, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        event_id = "legacy_history_" + hashlib.sha256(
                            source + b"\0" + encoded + b"\0" + str(index).encode("ascii")
                        ).hexdigest()[:32]
                        runtime.append_history_record(
                            record, event_id=event_id,
                            compact_threshold=0,
                        )
                    suffix = datetime.now().strftime("%Y%m%dT%H%M%S")
                    archive_parent = (
                        path.parent if path == cls._path()
                        else cls._path().parent / "legacy"
                    )
                    archive_parent.mkdir(parents=True, exist_ok=True)
                    archived = archive_parent / f"{path.name}.legacy-imported-{suffix}"
                    counter = 1
                    while archived.exists():
                        archived = archive_parent / f"{path.name}.legacy-imported-{suffix}-{counter}"
                        counter += 1
                    path.replace(archived)
                except Exception:
                    # Fail closed for this bounded source: do not archive the
                    # source and never fall back to a second writer.
                    raise

    # ── 读写（JSONL 格式，向后兼容旧 JSON 数组格式）────────────

    @staticmethod
    def load() -> list[HistoryEntry]:
        """Load the authoritative chronological SQLite projection."""
        HistoryManager._migrate_legacy()
        entries: list[HistoryEntry] = []
        for item in HistoryManager._storage().load_history_records():
            try:
                entries.append(HistoryEntry(**item))
            except TypeError:
                continue
        return entries

    @staticmethod
    def _load_jsonl(raw: str) -> list[HistoryEntry]:
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(HistoryEntry(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries

    @staticmethod
    def _load_json_array(raw: str) -> list[HistoryEntry]:
        """Load the legacy array format and recover any appended JSONL tail.

        Releases before v0.46 compacted JSONL into a pretty-printed JSON array,
        then ``_append_one`` continued appending JSON objects.  The resulting
        ``[...]{...}\n{...}`` file is not valid JSON, but both parts are still
        complete and recoverable.  ``raw_decode`` gives us the exact end of the
        array so no valid audit events need to be discarded during migration.
        """
        try:
            stripped = raw.lstrip()
            data, end = json.JSONDecoder().raw_decode(stripped)
            if not isinstance(data, list):
                return []
            entries = [HistoryEntry(**e) for e in data if isinstance(e, dict)]
            tail = stripped[end:].strip()
            if tail:
                entries.extend(HistoryManager._load_jsonl(tail))
            return entries
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def save(entries: list[HistoryEntry]) -> None:
        """Atomically replace the authoritative bounded history."""
        HistoryManager._storage().replace_history_records([
            asdict(entry) for entry in entries
        ])

    @staticmethod
    def _append_one(entry: HistoryEntry) -> None:
        """Append one SQLite/CAS record. Caller holds ``_lock``."""
        HistoryManager._storage().append_history_record(
            asdict(entry),
            compact_threshold=_COMPACT_THRESHOLD,
            keep_entries=_MAX_ENTRIES,
        )

    @classmethod
    def _compact(cls) -> None:
        """Atomically retain the newest configured entries."""
        entries = cls.load()
        if len(entries) <= _MAX_ENTRIES:
            return
        cls.save(entries[-_MAX_ENTRIES:])

    @classmethod
    def _compact_if_needed(cls) -> None:
        """Compatibility no-op; append performs low-frequency batch retention."""
        return

    # ── 公开 API ─────────────────────────────────────────────

    @classmethod
    def add_operation(cls, project_name: str, operation: str,
                      status: str = "success", detail: dict | None = None,
                      correlation_id: str = "") -> None:
        """记录一条操作历史（线程安全，JSONL 追加写入）。

        operation: "scan" | "formalize" | "sync" | "push"
                   | "triage_accept" | "triage_promote" | "triage_discard"
                   | "delete_formal" | "dissolve_formal"
        """
        entry = HistoryEntry(
            timestamp=datetime.now().isoformat(),
            project_name=project_name,
            operation=operation,
            status=status,
            detail=detail or {},
            correlation_id=correlation_id,
        )

        with cls._lock:
            cls._append_one(entry)

    @classmethod
    def add_suggestion(cls, project_name: str, suggest_type: str,
                       ai_proposal: dict, human_decision: dict,
                       correlation_id: str = "") -> None:
        """记录 AI 建议与人的最终决策差异，供 P4 质量度量使用。

        - ``suggest_type``: "formalize" | "triage" | "summary"
        - ``ai_proposal``: agent 返回的完整建议 JSON
        - ``human_decision``: 人最终执行时的参数
        """
        entry = HistoryEntry(
            timestamp=datetime.now().isoformat(),
            project_name=project_name,
            operation=f"suggest_{suggest_type}",
            status="recorded",
            detail={
                "ai_proposal": ai_proposal,
                "human_decision": human_decision,
            },
            correlation_id=correlation_id,
        )

        with cls._lock:
            cls._append_one(entry)

    @classmethod
    def add_entry(cls,
                  project_name: str,
                  file_count: int,
                  commit_hash: str,
                  commit_message: str,
                  workspace: str,
                  backup: str,
                  correlation_id: str = "",
                  ) -> None:
        """旧 API — 记录 sync 操作。委托到 add_operation 保持向后兼容。"""
        cls.add_operation(project_name, "sync", "success", {
            "file_count": file_count,
            "commit_hash": commit_hash,
            "commit_message": commit_message.split("\n")[0][:80],
            "workspace": workspace,
            "backup": backup,
        }, correlation_id=correlation_id)
