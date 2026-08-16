"""SyncSession 基座 — 状态初始化 + 状态查询 + 决策钩子默认实现 + 全流程编排。

最终的状态机类 ``SyncSession`` 由本基座与各阶段 mixin 组合而成（见 session.py）。
基座不 import 任何 mixin，避免循环依赖；跨阶段互调经运行时 MRO 解析。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Optional

from backend.adapters import FileAdapter, GitRunner
from backend.adapters.factory import create_adapters_for_node
from backend.core.config import Config, ProjectConfig
from backend.core.operations import CommitInfo, FileEntry
from backend.models import IncomingChange, TrialAction

from backend.core.sync_session.models import SessionStage, FormalCommit
from backend.core.sync_session.hooks import (
    FileSelectionHook,
    CommitSelectionHook,
    CommitMessageEditHook,
    SecurityWarningHook,
    TriageHook,
)


class SyncSessionBase:
    def __init__(self, project: ProjectConfig, config: Config):
        self.project = project
        self.config = config

        # 路径
        self.workspace_path = Path(project.workspace_path or Path.cwd()).resolve()
        self.backup_path = Path(project.backup_path) if project.backup_path else None

        # ── 适配器 ──
        workspace_node = project.workspace
        # 如果 workspace 无 path，用 CWD
        if not workspace_node.file_access.path:
            workspace_node.file_access.path = str(self.workspace_path)
        self.ws_adapter, self.ws_git_runner = create_adapters_for_node(workspace_node)

        if project.release and project.release.file_access.path:
            self.bk_adapter, self.bk_git_runner = create_adapters_for_node(
                project.release
            )
        else:
            from backend.adapters import LocalFileAdapter, LocalGitRunner

            self.bk_adapter = LocalFileAdapter(Path.cwd())
            self.bk_git_runner = LocalGitRunner(Path.cwd())

        # 状态
        self.stage: SessionStage = SessionStage.IDLE

        # 数据缓存（step_scan / step_load_commits 写入）
        self.entries: list[FileEntry] = []
        self.commits: list[CommitInfo] = []
        self.formal_commits: list[FormalCommit] = []
        self.selected_workspace: set[int] = set()

        # ── Trial 三叉缓存 ──
        self.incoming_changes: list[IncomingChange] = []
        self.trial_adapter: Optional[FileAdapter] = None
        self.trial_git_runner: Optional[GitRunner] = None

        # ── 决策钩子（UI 覆盖） ──
        self.on_file_selection: FileSelectionHook = self._default_file_selection
        self.on_commit_select: CommitSelectionHook = self._default_commit_selection
        self.on_commit_message_edit: CommitMessageEditHook = self._default_commit_message_edit
        self.on_security_warning: SecurityWarningHook = self._default_security_warning
        self.on_triage_decision: TriageHook = self._default_triage

        # ── 进度/日志回调 ──
        self.on_stage_changed: Callable[[SessionStage], None] = lambda s: None
        self.on_progress: Callable[[int, int, str], None] = lambda c, t, m: None
        self.on_log: Callable[[str], None] = lambda m: None

        # B-3: Accept 两步确认暂存
        self._pending_accept: IncomingChange | None = None

        # P4-Pre: session 级关联 ID，同一次工作流的所有 history 记录共享
        self._correlation_id: str = str(uuid.uuid4())

    # ── 状态查询 ────────────────────────────────────────────

    def status_dict(self, semantic: bool = True, layered: bool = False) -> dict:
        """返回机器可读的当前项目状态。

        semantic=True 时附加 semantic 子块（agent 可直接消费的判断）。
        layered=True 时输出三层显式结构（operational / governance / semantic）。
        旧格式（layered=False）向后兼容。
        """
        trial_pending = sum(1 for c in self.incoming_changes
                            if c.triage == TrialAction.PENDING)
        entries_changed = sum(1 for e in self.entries
                              if e.status != "same" and e.selected)
        formal_total = len(self.formal_commits)
        formal_synced = sum(1 for fc in self.formal_commits if fc.synced)
        formal_pushed = sum(1 for fc in self.formal_commits if fc.pushed)

        if layered:
            semantic_block = self._build_semantic_layer(
                trial_pending, entries_changed, formal_total,
                formal_synced, formal_pushed,
            ) if semantic else None

            result = {
                "project": self.project.name,
                "layers": {
                    "operational": {
                        "stage": self.stage.name,
                        "entries_total": len(self.entries),
                        "entries_changed": entries_changed,
                        "workspace_path": str(self.workspace_path),
                    },
                    "governance": {
                        "formal_total": formal_total,
                        "formal_synced": formal_synced,
                        "formal_pushed": formal_pushed,
                        "workspace_commits": len(self.commits),
                        "trial_configured": (
                            self.project.trial is not None
                            and bool(self.project.trial.file_access.path)
                        ),
                        "trial_pending": trial_pending,
                        "trial_total": len(self.incoming_changes),
                    },
                },
            }
            if semantic_block:
                result["layers"]["semantic"] = semantic_block
            return result

        # 旧格式（向后兼容）
        result = {
            "project": self.project.name,
            "stage": self.stage.name,
            "workspace": {
                "path": str(self.workspace_path),
                "entries_total": len(self.entries),
                "entries_changed": entries_changed,
            },
            "commits": {
                "workspace_total": len(self.commits),
                "formal_total": formal_total,
                "formal_synced": formal_synced,
                "formal_pushed": formal_pushed,
            },
            "trial": {
                "configured": (self.project.trial is not None
                               and bool(self.project.trial.file_access.path)),
                "pending": trial_pending,
                "total": len(self.incoming_changes),
            },
        }

        if semantic:
            result["semantic"] = self._build_semantic_layer(
                trial_pending, entries_changed, formal_total,
                formal_synced, formal_pushed,
            )

        return result

    def _build_semantic_layer(self, trial_pending: int, entries_changed: int,
                               formal_total: int, formal_synced: int,
                               formal_pushed: int) -> dict:
        """从原始计数计算 agent 可消费的语义判断。"""
        if entries_changed == 0:
            entropy = "low"
        elif entries_changed <= 10:
            entropy = "medium"
        else:
            entropy = "high"

        action_queue = []
        if trial_pending > 0:
            action_queue.append("triage")
        if entries_changed > 0:
            action_queue.append("formalize")
        if formal_synced > formal_pushed:
            action_queue.append("push")

        suggested = action_queue[0] if action_queue else "idle"

        blocked_reason = None
        if formal_synced > formal_pushed and entries_changed > 0:
            blocked_reason = "unsynced_formal_commits"
        elif not self.backup_path or not (self.backup_path.exists() if self.backup_path else False):
            blocked_reason = "no_backup_configured"

        return {
            "workspace_entropy": entropy,
            "trial_requires_review": trial_pending > 0,
            "safe_to_formalize": entries_changed > 0 and self.stage == SessionStage.IDLE,
            "safe_to_publish": formal_synced > 0 and formal_synced > formal_pushed,
            "blocked_reason": blocked_reason,
            "suggested_next_action": suggested,
            "action_queue": action_queue,
        }

    # ── 默认决策实现 ────────────────────────────────────────

    @staticmethod
    def _default_file_selection(entries: list[FileEntry]) -> list[FileEntry]:
        for e in entries:
            if e.status != "same":
                e.selected = True
        return entries

    @staticmethod
    def _default_commit_selection(commits: list[CommitInfo]) -> set[int]:
        return set(range(len(commits)))

    @staticmethod
    def _default_commit_message_edit(template: str, project: ProjectConfig) -> str | None:
        lines = [l for l in template.split("\n") if l and not l.startswith("#")]
        return "\n".join(lines).strip() or None

    @staticmethod
    def _default_security_warning(warnings: list[dict]) -> bool:
        return False

    @staticmethod
    def _default_triage(changes: list[IncomingChange], project: ProjectConfig) -> Optional[tuple[int, str]]:
        return None

    # ── 重置 ────────────────────────────────────────────────

    def reset(self):
        """重置扫描/commit 缓存（不清除 formal_commits）"""
        self.stage = SessionStage.IDLE
        self.entries = []
        self.commits = []
        self.selected_workspace = set()
        self.incoming_changes = []
        self.trial_adapter = None
        self.trial_git_runner = None
        self.on_stage_changed(self.stage)

    # ── 全自动流程（Daemon 模式） ────────────────────────────

    def run_full_workflow(
        self,
        commit_message: Optional[str] = None,
        skip_push: bool = False,
        force_on_warning: bool = False,
    ) -> bool:
        """自动执行完整工作流：scan → commit → sync → push。

        commit_message: 直接指定 commit message
        skip_push:      跳过 push 步骤
        force_on_warning: 安全检查命中时强制推送
        """
        entries = self.step_scan()
        if not entries:
            self.on_log("无文件变更，跳过")
            return True
        changed = [e for e in entries if e.selected]
        if not changed:
            self.on_log("无选中文件，跳过")
            return True
        self.on_log(f"变更文件: {len(changed)} 个")

        self.step_load_commits()
        fc = self.step_create_formal_commit(message=commit_message)
        if fc is None:
            self.on_log("创建 formal commit 失败")
            return False

        if not self.step_sync():
            return False

        if not skip_push:
            if force_on_warning:
                success, _ = self.step_push(skip_scan=True)
            else:
                success, _ = self.step_push()
            if not success:
                return False

        self.stage = SessionStage.IDLE
        self.on_stage_changed(self.stage)
        return True
