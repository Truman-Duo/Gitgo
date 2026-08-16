"""扫描阶段 — step_scan / step_scan_files / step_load_commits。"""

from __future__ import annotations

from backend.core.operations import (
    CommitInfo,
    FileEntry,
    compare_files,
    get_exclude_patterns,
    get_git_log,
    scan_workspace,
)

from backend.core.sync_session.models import SessionStage


class ScanMixin:
    def step_scan(self, hash_cache: "FileHashCache | None" = None) -> list[FileEntry]:
        """扫描工作区并对比备份仓库"""
        self.stage = SessionStage.SCANNING
        self.on_stage_changed(self.stage)
        self.on_log("开始扫描工作区...")

        exclude = get_exclude_patterns(
            self.project, self.workspace_path, file_adapter=self.ws_adapter,
        )
        files = scan_workspace(
            self.workspace_path, exclude, file_adapter=self.ws_adapter,
        )
        self.on_log(f"找到 {len(files)} 个文件")

        if not self.backup_path or not self.bk_adapter.exists(""):
            self.on_log("未配置有效的备份路径")
            self.entries = []
            self.stage = SessionStage.FAILED
            self.on_stage_changed(self.stage)
            return self.entries

        if not self.bk_git_runner.is_git_repo():
            self.on_log("备份路径不是 git 仓库")
            self.entries = []
            self.stage = SessionStage.FAILED
            self.on_stage_changed(self.stage)
            return self.entries

        entries = compare_files(
            self.workspace_path, self.backup_path, files, self.on_progress,
            ws_adapter=self.ws_adapter, bk_adapter=self.bk_adapter,
            normalize_eol=True, hash_cache=hash_cache,
        )
        entries = self.on_file_selection(entries)

        # ── Integrity Detection ──
        if getattr(self.project, "integrity", {}).get("enabled", True):
            from backend.core.identity import _run_integrity_checks
            warnings_list = _run_integrity_checks(
                entries, self.workspace_path, self.project,
            )
            for w in warnings_list:
                self.on_log(f"[INTEGRITY] {w['level'].upper()}: {w['message']}")
                from backend.core.history import HistoryManager as _HM
                _HM.add_operation(
                    self.project.name, "integrity_warning", w["level"],
                    w, correlation_id=self._correlation_id,
                )

        self.on_log(f"对比完成: {len(entries)} 个文件变更")
        self.entries = entries
        self.stage = SessionStage.SELECTING
        self.on_stage_changed(self.stage)
        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            self.project.name, "scan", "success",
            {"entries_total": len(entries),
             "entries_changed": sum(1 for e in entries if e.status != "same")},
            correlation_id=self._correlation_id,
        )
        return entries

    def step_scan_files(self, changed_files: list[str],
                        hash_cache: "FileHashCache | None" = None) -> list[FileEntry]:
        """增量扫描——只对比指定文件列表。"""
        self.stage = SessionStage.SCANNING
        self.on_stage_changed(self.stage)
        if not self.backup_path or not self.bk_adapter.exists(""):
            self.entries = []; self.stage = SessionStage.FAILED; return self.entries
        if not self.bk_git_runner.is_git_repo():
            self.entries = []; self.stage = SessionStage.FAILED; return self.entries
        entries = compare_files(
            self.workspace_path, self.backup_path, changed_files, self.on_progress,
            ws_adapter=self.ws_adapter, bk_adapter=self.bk_adapter, normalize_eol=True,
            hash_cache=hash_cache,
        )
        self.entries = entries
        self.stage = SessionStage.SELECTING
        self.on_stage_changed(self.stage)
        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            self.project.name, "scan", "success",
            {"entries_total": len(entries),
             "entries_changed": sum(1 for e in entries if e.status != "same")},
            correlation_id=self._correlation_id,
        )
        return entries

    def step_load_commits(self) -> list[CommitInfo]:
        """加载工作区 git 日志"""
        commits = get_git_log(
            self.workspace_path, self.project.sync_base or None,
            git_runner=self.ws_git_runner,
        )
        self.commits = commits
        if commits:
            self.on_log(f"发现 {len(commits)} 个 workspace commit")
        else:
            self.on_log("未检测到新 commit")
        return commits
