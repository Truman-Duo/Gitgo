"""SyncSession 子系统测试数据生成器。

覆盖：FileEntry / CommitInfo / FormalCommit / scan 结果 / sync 链路。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from backend.core.operations.models import FileEntry, CommitInfo
from tests.factory import pools


class SyncGenerator:
    def __init__(self, factory):
        self.f = factory

    def file_entry(self, **overrides) -> FileEntry:
        """生成一条随机 FileEntry。"""
        return FileEntry(
            rel_path=overrides.pop("rel_path", self.f._pick(pools.FILE_PATHS)),
            status=overrides.pop("status",
                self.f._pick(["new", "modified", "same", "renamed"],
                             [2, 4, 3, 1])),
            old_path=overrides.pop("old_path", None),
            workspace_hash=overrides.pop("workspace_hash",
                self._random_hash()),
            backup_hash=overrides.pop("backup_hash",
                self._random_hash()),
            selected=overrides.pop("selected", self.f._bool(0.7)),
            **overrides,
        )

    def file_entries(self, n: int = 10, **overrides) -> list[FileEntry]:
        """生成 N 条随机 FileEntry（带 realistic 状态分布）。"""
        return [self.file_entry(**overrides) for _ in range(n)]

    def commit_info(self, **overrides) -> CommitInfo:
        """生成一条随机 CommitInfo。"""
        return CommitInfo(
            hash=overrides.pop("hash", self._random_hash()),
            subject=overrides.pop("subject",
                self.f._pick(pools.TASK_DESCRIPTIONS)),
            type=overrides.pop("type",
                self.f._pick(pools.COMMIT_TYPES)),
            scope=overrides.pop("scope",
                self.f._pick(pools.COMMIT_SCOPES) if self.f._bool(0.6) else None),
            body=overrides.pop("body", ""),
            **overrides,
        )

    def commit_infos(self, n: int = 5) -> list[CommitInfo]:
        """生成 N 条随机 CommitInfo。"""
        return [self.commit_info() for _ in range(n)]

    def formal_commit(self, **overrides) -> dict:
        """生成一条 FormalCommit 的 dict 表示。"""
        prefix = overrides.pop("prefix",
            self.f._pick(["GITGO", "PROJ", "FEAT", "FIX"]))
        number = overrides.pop("number", self.f._int(1, 200))
        return {
            "message": overrides.pop("message",
                f"[{prefix}-{number}] {self.f._pick(pools.COMMIT_TYPES)}: "
                f"{self.f._pick(pools.TASK_DESCRIPTIONS)}"),
            "number": number,
            "prefix": prefix,
            "synced": overrides.pop("synced", self.f._bool(0.5)),
            "pushed": overrides.pop("pushed", self.f._bool(0.3)),
            "created_at": overrides.pop("created_at",
                self.f._ts(self.f._int(1, 1440))),
            **overrides,
        }

    def scan_result(self, file_count: int = 10) -> dict:
        """生成一次完整 scan 的结果。"""
        entries = self.file_entries(file_count)
        commits = self.commit_infos(self.f._int(1, 5))

        changed = [e for e in entries if e.status != "same"]
        return {
            "entries": entries,
            "commits": commits,
            "changed_count": len(changed),
            "total_count": len(entries),
            "status_dict": {
                "stage": "IDLE",
                "entries_total": len(entries),
                "entries_changed": len(changed),
                "formal_commits": len(commits),
            },
        }

    def sync_chain(self, file_count: int = 10) -> dict:
        """生成完整 scan → formalize → sync 链路数据。"""
        entries = self.file_entries(file_count)
        commits = self.commit_infos(self.f._int(2, 5))
        selected = [e for e in entries if e.selected]

        formal = self.formal_commit(number=self.f._int(10, 50))

        return {
            "entries": entries,
            "commits": commits,
            "selected": selected,
            "formal_commit": formal,
            "changed_files": [e.rel_path for e in selected
                             if e.status != "same"],
        }

    # ── 内部 ──────────────────────────────────────────────

    def _random_hash(self) -> str:
        raw = f"{self.f.rng.random()}{self.f._next_id()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]
