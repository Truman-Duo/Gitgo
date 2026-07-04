"""SyncSession — Runtime Kernel (Layer 1: Operational State Machine)

Gitgo 的运行时核心。18 个 step_*() 方法驱动状态转移。
GUI / CUI / CLI / Daemon 四种前端共用此状态机。

Operational State Machine:
  IDLE → SCANNING → SELECTING → COMMITTING → SYNCING → PUSHING → IDLE
            ↘ TRIAL_CHECKING → TRIAL_REVIEWING → INCOMING_CONFIRMING

规则:
  - 所有状态转移必须通过 step_*() 方法，禁止直接修改 self.stage
  - 每个 step_*() 方法在成功时写入对应的 governance event
  - 硬编码调用序列（非 event-driven）——参见 RuntimeConstitution §4 Observer Constraint

纯 Python 实现，无 Qt 依赖。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

from backend.adapters import FileAdapter, GitRunner
from backend.adapters.factory import create_adapters_for_node
from backend.core.config import Config, ConfigManager, ProjectConfig
from backend.core.operations import (
    CommitInfo,
    FileEntry,
    _find_next_number,
    build_commit_template,
    compare_files,
    get_exclude_patterns,
    get_git_log,
    push_to_backup,
    scan_workspace,
    sync_to_backup,
    validate_commit_message,
)
from backend.models import IncomingChange, TrialAction

# ── 枚举 ─────────────────────────────────────────────────────


class SessionStage(Enum):
    IDLE = auto()
    TRIAL_CHECKING = auto()
    TRIAL_REVIEWING = auto()
    INCOMING_CONFIRMING = auto()
    SCANNING = auto()
    SELECTING = auto()
    COMMITTING = auto()
    SYNCING = auto()
    PUSHING = auto()
    FAILED = auto()


# ── 数据模型 ────────────────────────────────────────────────


@dataclass
class FormalCommit:
    message: str
    number: int
    prefix: str
    synced: bool = False
    pushed: bool = False
    is_incoming: bool = False
    sources_cleared: bool = False
    created_at: str = ""
    source_indices: set[int] = field(default_factory=set)


# ── 决策钩子类型 ────────────────────────────────────────────

FileSelectionHook = Callable[[list[FileEntry]], list[FileEntry]]
CommitSelectionHook = Callable[[list[CommitInfo]], set[int]]
CommitMessageEditHook = Callable[[str, ProjectConfig], str | None]
SecurityWarningHook = Callable[[list[dict]], bool]
TriageHook = Callable[[list[IncomingChange], ProjectConfig], Optional[tuple[int, str]]]


# ── SyncSession ─────────────────────────────────────────────


class SyncSession:
    """工作流状态机 — 编排 scan → commit → sync → push 全流程。

    交互模式（GUI/CUI）：覆盖决策钩子，然后逐个调用 step_*() 方法。
    Daemon 模式：调用 run_full_workflow() 自动走完所有步骤。
    """

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

    # ── 步骤 0: Trial 三叉 ──────────────────────────────────

    def step_check_trial(self) -> list[IncomingChange]:
        """检查 trial 仓库是否有新 commit。独立于主 scan 流程。"""
        self.stage = SessionStage.TRIAL_CHECKING
        self.on_stage_changed(self.stage)

        trial_node = self.project.trial
        if not trial_node or not trial_node.file_access.path:
            self.on_log("未配置 Trial 仓库，跳过")
            return []

        self.trial_adapter, self.trial_git_runner = create_adapters_for_node(trial_node)
        if not self.trial_git_runner.is_git_repo():
            self.on_log("Trial 路径不是 git 仓库")
            return []

        since_hash = trial_node.last_known_head or None
        current_head = self.trial_git_runner.rev_parse("HEAD")
        if not current_head:
            self.on_log("无法获取 Trial HEAD")
            return []

        if not since_hash:
            self.on_log(f"首次记录 Trial HEAD: {current_head[:12]}")
            trial_node.last_known_head = current_head
            ConfigManager.save(self.config)
            return []

        if since_hash == current_head:
            self.on_log("Trial 仓库无新 commit")
            return []

        from backend.core.operations import get_trial_log

        changes = get_trial_log(
            trial_node.file_access.path,
            since_hash=since_hash,
            git_runner=self.trial_git_runner,
        )
        # 过滤已处理的 incoming（B-4）
        processed = getattr(self.project, 'processed_incoming', {})
        changes = [c for c in changes if c.hash not in processed]
        self.incoming_changes = changes

        if changes:
            self.stage = SessionStage.TRIAL_REVIEWING
            self.on_stage_changed(self.stage)
            self.on_log(f"发现 {len(changes)} 个 Trial 新 commit")
        else:
            self.stage = SessionStage.IDLE
            self.on_stage_changed(self.stage)
            self.on_log("Trial 无新 commit")
        return changes

    def step_triage_incoming(self, index: int, action: str) -> bool:
        """对指定 IncomingChange 执行三叉决策。

        action: "accept" | "promote" | "discard"
        """
        if index < 0 or index >= len(self.incoming_changes):
            return False
        change = self.incoming_changes[index]
        trial_node = self.project.trial
        if not trial_node:
            return False

        trial_path = trial_node.file_access.path

        if action == "accept":
            if not self.project.release or not self.project.release.file_access.path:
                self.on_log("未配置 Release 仓库，无法 accept")
                return False
            self.on_log(f"Accept: cherry-pick {change.hash[:12]} 到 Release")
            r1 = self.bk_git_runner.run(["remote", "add", "trial", trial_path])
            if r1.returncode != 0 and "already exists" not in r1.stderr:
                self.on_log(f"添加 remote 失败: {r1.stderr}")
                return False
            ok1, e1 = self.bk_git_runner.fetch("trial")
            if not ok1:
                self.on_log(f"Fetch 失败: {e1}")
                self.bk_git_runner.run(["remote", "remove", "trial"])
                return False
            # cherry-pick with auto-resolve for modify/delete conflicts
            ok2, e2 = self.bk_git_runner.cherry_pick(change.hash)
            if not ok2:
                # Retry with theirs strategy for modify/delete conflicts
                self.bk_git_runner.run(["cherry-pick", "--abort"])
                r2 = self.bk_git_runner.run(
                    ["cherry-pick", "-X", "theirs", change.hash]
                )
                if r2.returncode != 0:
                    self.on_log(f"Cherry-pick 失败: {r2.stderr}")
                    self.bk_git_runner.run(["cherry-pick", "--abort"])
                    self.bk_git_runner.run(["remote", "remove", "trial"])
                    return False
            self.bk_git_runner.run(["remote", "remove", "trial"])
            prefix = self.project.commit_format.get("prefix", "PROJ")
            fc = FormalCommit(
                message=change.message,
                number=0,
                prefix=prefix,
                synced=True,
                pushed=False,
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
            self.formal_commits.append(fc)
            change.triage = TrialAction.ACCEPTED
            self.on_log(f"Accept 完成: {change.hash[:12]} → Release")

        elif action == "promote":
            self.on_log(f"Promote: fetch trial {change.hash[:12]} 到 Workspace")
            r1 = self.ws_git_runner.run(["remote", "add", "trial", trial_path])
            if r1.returncode != 0 and "already exists" not in r1.stderr:
                self.on_log(f"添加 remote 失败: {r1.stderr}")
                return False
            ok1, e1 = self.ws_git_runner.fetch("trial")
            if not ok1:
                self.on_log(f"Fetch 失败: {e1}")
                self.ws_git_runner.run(["remote", "remove", "trial"])
                return False
            branch = f"incoming/trial-{change.hash[:8]}"
            r2 = self.ws_git_runner.run(["branch", branch, "FETCH_HEAD"])
            if r2.returncode != 0:
                self.on_log(f"创建分支 {branch} 失败: {r2.stderr}")
                self.ws_git_runner.run(["remote", "remove", "trial"])
                return False
            self.ws_git_runner.run(["remote", "remove", "trial"])
            change.triage = TrialAction.PROMOTED
            self.on_log(f"Promote 完成: 分支 {branch} 已创建")

        elif action == "discard":
            change.triage = TrialAction.DISCARDED
            self.on_log(f"Discard: 已忽略 {change.hash[:12]}")

        else:
            self.on_log(f"未知操作: {action}")
            return False

        # 持久化已处理结果（B-4）
        self._record_processed(change.hash, action)
        self._last_op = {"op": f"triage_{action}", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            self.project.name, f"triage_{action}", "success",
            {"trial_hash": change.hash,
             "trial_message": change.message.split('\n')[0][:80]},
            correlation_id=self._correlation_id,
        )
        self.save_session()

        all_processed = all(c.triage != TrialAction.PENDING for c in self.incoming_changes)
        if all_processed and trial_node:
            head = self.trial_git_runner.rev_parse("HEAD") if self.trial_git_runner else None
            if head:
                trial_node.last_known_head = head
                ConfigManager.save(self.config)
                self.on_log(f"Trial last_known_head 已更新: {head[:12]}")
        return True

    # ── 步骤 1: 扫描 ────────────────────────────────────────

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

    # ── 步骤 2: 加载 commit 列表 ────────────────────────────

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

    # ── 步骤 3: 创建正式 Commit ────────────────────────────

    def step_create_formal_commit(
        self,
        selected_indices: Optional[set[int]] = None,
        message: Optional[str] = None,
        template_name: str | None = None,
    ) -> Optional[FormalCommit]:
        """从选中的 workspace commit 创建正式 commit。

        selected_indices=None → 调用 on_commit_select 让 UI 做选择。
        message=None          → 调用 on_commit_message_edit 让 UI 编辑。
        """
        self.stage = SessionStage.COMMITTING
        self.on_stage_changed(self.stage)

        if not self.commits:
            self.step_load_commits()
        if not self.commits:
            self.on_log("没有可用的 commit")
            return None

        is_direct_submit = selected_indices is not None and len(selected_indices) == 0

        if selected_indices is None:
            selected_indices = self.on_commit_select(self.commits)
        if not is_direct_submit and (not selected_indices or len(selected_indices) < 1):
            self.on_log("未选择任何 commit")
            return None

        self.selected_workspace = selected_indices

        if is_direct_submit:
            template = message or ""
        else:
            selected_commits = [self.commits[i] for i in sorted(selected_indices)]
            template = build_commit_template(selected_commits, self.project,
                                             template_name=template_name)

        prefix = self.project.commit_format.get("prefix", "PROJ")
        number_start = self.project.commit_format.get("number_start", 0)

        if message is None:
            message = self.on_commit_message_edit(template, self.project)
            if message is None:
                self.on_log("用户取消编辑 commit message")
                return None
            err = validate_commit_message(message)
            if err:
                self.on_log(f"Commit message 格式错误: {err}")
                message = self.on_commit_message_edit(template, self.project)
                if message is None:
                    return None
                err2 = validate_commit_message(message)
                if err2:
                    self.on_log(f"仍错误: {err2}")
                    return None

        # 分配编号
        max_n = number_start
        for fc in self.formal_commits:
            if fc.number > max_n:
                max_n = fc.number
        repo_max = _find_next_number(
            self.project.backup_path, prefix,
            git_runner=self.bk_git_runner,
            workspace_path=self.workspace_path,
        )
        next_n = max(max_n, repo_max)

        # Update local counter
        if self.workspace_path:
            counter_file = Path(self.workspace_path) / ".gitgo" / "next_number"
            counter_file.parent.mkdir(parents=True, exist_ok=True)
            counter_file.write_text(str(next_n))

        fc = FormalCommit(
            message=message,
            number=next_n,
            prefix=prefix,
            source_indices=selected_indices,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        self.formal_commits.append(fc)
        self.on_log(f"正式 Commit 已创建: [{prefix}-{fc.number}]")
        self._last_op = {"op": "formalize", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            self.project.name, "formalize", "success",
            {"commit": f"[{prefix}-{fc.number}]",
             "source_indices": list(selected_indices),
             "files_changed": [
                 {"path": e.rel_path, "status": e.status}
                 for e in self.entries if e.selected
             ]},
            correlation_id=self._correlation_id,
        )
        self.save_session()
        return fc

    # ── 步骤 3.5: Formal commit 管理 ──────────────────────

    def step_toggle_workspace_selection(self, index: int, mode: str = "single") -> set[int]:
        """切换 workspace commit 选择状态。

        mode: "single" (替换) | "toggle" (Ctrl) | "range" (Shift)
        返回新的 selected_workspace。
        """
        if mode == "toggle":
            if index in self.selected_workspace:
                self.selected_workspace.discard(index)
            else:
                self.selected_workspace.add(index)
        elif mode == "range" and self.selected_workspace:
            last = max(self.selected_workspace)
            start, end = (last, index) if last < index else (index, last)
            for i in range(start, end + 1):
                self.selected_workspace.add(i)
        else:
            self.selected_workspace = {index}
        return self.selected_workspace

    def step_delete_formal(self, index: int) -> bool:
        """删除 formal commit。"""
        if index < 0 or index >= len(self.formal_commits):
            self.on_log(f"无效的 formal commit 索引: {index}")
            return False
        fc = self.formal_commits.pop(index)
        self.on_log(f"已删除 formal commit: [{fc.prefix}-{fc.number}]")
        self._last_op = {"op": "delete_formal", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            self.project.name, "delete_formal", "success",
            {"commit": f"[{fc.prefix}-{fc.number}]"},
            correlation_id=self._correlation_id,
        )
        self.save_session()
        return True

    def step_edit_formal_message(self, index: int, message: str) -> bool:
        """编辑 formal commit message。"""
        if index < 0 or index >= len(self.formal_commits):
            return False
        err = validate_commit_message(message)
        if err:
            self.on_log(f"Commit message 格式错误: {err}")
            return False
        self.formal_commits[index].message = message
        self.on_log("Formal commit message 已更新")
        self._last_op = {"op": "edit_message", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        from backend.core.history import HistoryManager as _HM
        _HM.add_operation(
            self.project.name, "governance_edited", "success",
            {"index": index, "prefix": self.formal_commits[index].prefix,
             "number": self.formal_commits[index].number},
            correlation_id=self._correlation_id,
        )
        self.save_session()
        return True

    def step_edit_formal_number(self, index: int, new_number: int) -> bool:
        """编辑 formal commit 编号（同步更新 message 中的 tag）。"""
        if index < 0 or index >= len(self.formal_commits):
            return False
        fc = self.formal_commits[index]
        if new_number == fc.number:
            return True
        for other in self.formal_commits:
            if other.number == new_number and other is not fc:
                self.on_log(f"编号冲突: {new_number} 已被使用")
                return False
        old_tag = f"[{fc.prefix}-{fc.number}]"
        new_tag = f"[{fc.prefix}-{new_number}]"
        old_number = fc.number
        fc.message = fc.message.replace(old_tag, new_tag, 1)
        fc.number = new_number
        self.on_log(f"编号已更新: [{fc.prefix}-{fc.number}]")
        self._last_op = {"op": "edit_number", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        from backend.core.history import HistoryManager as _HM
        _HM.add_operation(
            self.project.name, "governance_renumbered", "success",
            {"index": index, "prefix": fc.prefix,
             "old_number": old_number, "new_number": new_number},
            correlation_id=self._correlation_id,
        )
        self.save_session()
        return True

    def step_dissolve_formal(self, index: int) -> bool:
        """Dissolve formal commit — 清除来源引用并删除。"""
        if index < 0 or index >= len(self.formal_commits):
            return False
        fc = self.formal_commits[index]
        if not fc.source_indices:
            self.on_log("该 Formal Commit 没有关联的 Workspace commits，无法 Dissolve")
            return False
        self.selected_workspace = set()
        self.formal_commits.pop(index)
        self.on_log(f"已 Dissolve: [{fc.prefix}-{fc.number}]，Workspace commits 已恢复")
        self._last_op = {"op": "dissolve", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            self.project.name, "dissolve_formal", "success",
            {"commit": f"[{fc.prefix}-{fc.number}]"},
            correlation_id=self._correlation_id,
        )
        HistoryManager.add_operation(
            self.project.name, "governance_dissolved", "success",
            {"commit": f"[{fc.prefix}-{fc.number}]",
             "source_indices": sorted(fc.source_indices)},
            correlation_id=self._correlation_id,
        )
        self.save_session()
        return True

    def step_clear_formal_sources(self, index: int) -> bool:
        """清除 formal commit 的 workspace 来源引用。"""
        if index < 0 or index >= len(self.formal_commits):
            return False
        fc = self.formal_commits[index]
        if not fc.source_indices:
            self.on_log("该 Formal Commit 没有关联的 Workspace commits")
            return False
        fc.sources_cleared = True
        fc.source_indices = set()
        self.on_log(f"已清除 [{fc.prefix}-{fc.number}] 的来源引用")
        self._last_op = {"op": "clear_sources", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        self.save_session()
        return True

    def step_add_incoming_formal(self, message: str) -> FormalCommit:
        """从 incoming accept 创建 formal commit（is_incoming=True）。"""
        prefix = self.project.commit_format.get("prefix", "PROJ")
        fc = FormalCommit(
            message=message,
            number=0,
            prefix=prefix,
            source_indices=set(),
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        fc.is_incoming = True
        self.formal_commits.append(fc)
        self.on_log(f"Incoming formal commit 已创建: [{fc.prefix}-{fc.number}]")
        self._last_op = {"op": "add_incoming", "status": "success",
                         "timestamp": datetime.now().isoformat()}
        self.save_session()
        return fc

    # ── 步骤 4: Sync ────────────────────────────────────────

    def step_sync(self, formal_index: Optional[int] = None) -> bool:
        """同步到备份仓库。formal_index=None → 找第一个未 synced 的。"""
        self.stage = SessionStage.SYNCING
        self.on_stage_changed(self.stage)

        if formal_index is not None:
            fc = self.formal_commits[formal_index]
        else:
            target = None
            for fc in self.formal_commits:
                if not fc.synced:
                    target = fc
                    break
            if target is None:
                self.on_log("没有待同步的正式 Commit")
                return False
            fc = target

        selected = [e for e in self.entries if e.selected]
        if not selected:
            self.on_log("未选择任何文件")
            return False

        # ── 外来 commit 检测 ──
        if self.backup_path and self.bk_git_runner.is_git_repo():
            current_head = self.bk_git_runner.rev_parse("HEAD")
            if current_head:
                release_node = self.project.release
                recorded = release_node.last_known_head if release_node else ""
                if recorded and current_head != recorded:
                    self.on_log(
                        f"[WARN] Release repo 有外来 commit\n"
                        f"  recorded: {recorded[:12]}\n"
                        f"  current:  {current_head[:12]}"
                    )
                    HistoryManager.add_operation(
                        self.project.name, "integrity_warning", "warning",
                        {"rule": "foreign_commit_detected",
                         "recorded_head": recorded,
                         "current_head": current_head},
                        correlation_id=self._correlation_id,
                    )

        if not self.backup_path:
            self.on_log("未配置备份路径")
            return False

        self.on_log(f"同步到备份仓库: {fc.message.split(chr(10))[0]}")

        # ── Gate A: 抽象工作区→抽象备份区的合法性边界 ──
        from backend.core.contract import ContractManager, detect_drift
        contract = ContractManager.load(self.workspace_path)
        gate_blocked = False
        if contract:
            changed_paths = [e.rel_path for e in selected]
            drift_alerts = detect_drift(self.workspace_path, changed_paths, contract)
            # 依赖签名检测
            from backend.core.contract import check_feature_signatures
            dep_alerts = check_feature_signatures(
                self.workspace_path, changed_paths, contract,
            )
            all_alerts = drift_alerts + dep_alerts
            if all_alerts:
                errors = [a for a in all_alerts if a.get("level") == "error"]
                for a in all_alerts:
                    self.on_log(f"[Gate A] {a['level'].upper()}: {a['message'][:120]}")
                from backend.core.history import HistoryManager as _HM
                _HM.add_operation(
                    self.project.name, "governance_drift", "warning",
                    {"alert_count": len(all_alerts),
                     "rules": [a["rule"] for a in all_alerts]},
                    correlation_id=self._correlation_id,
                )
                if errors:
                    self.on_log(
                        f"[Gate A] BLOCKED: {len(errors)} error-level drift(s) detected. "
                        f"Fix before sync or use --force to override."
                    )
                    gate_blocked = True

        if gate_blocked:
            self.stage = SessionStage.FAILED
            self.on_stage_changed(self.stage)
            return False

        success = sync_to_backup(
            selected, fc.message,
            self.workspace_path, self.backup_path,
            self.on_progress,
            plugin_ids=self.project.commit_format.get("plugins"),
            ws_adapter=self.ws_adapter, bk_adapter=self.bk_adapter,
            git_runner=self.bk_git_runner,
        )

        if success:
            fc.synced = True

            from backend.core.history import HistoryManager as _HM
            _HM.add_operation(
                self.project.name, "governance_synced", "success",
                {"commit": f"[{fc.prefix}-{fc.number}]"},
                correlation_id=self._correlation_id,
            )

            # ── Dependency Graph Update ──
            try:
                from backend.core.contract import build_dep_graph
                build_dep_graph(Path(self.workspace_path))
            except Exception:
                pass

            # ── Memory Snapshot + Skeleton ──
            if self.backup_path:
                try:
                    from backend.core.identity.snapshot import snapshot_tool_memories
                    result = snapshot_tool_memories(
                        self.workspace_path, self.backup_path, self.project,
                    )
                    _HM.add_operation(
                        self.project.name, "governance_memory_snapshot", "success",
                        {"sources": result.get("snapped", [])},
                        correlation_id=self._correlation_id,
                    )
                    from backend.core.identity.guard import _save_directory_skeleton
                    _save_directory_skeleton(self.workspace_path)
                    # 自动更新项目合约（记录本次 sync 确认的 feature）
                    from backend.core.contract import ContractManager
                    msg_first_line = fc.message.split("\n")[0] if fc.message else ""
                    feature_name = msg_first_line[:60] if msg_first_line else "sync"
                    ContractManager.update_feature(
                        self.workspace_path, self.project.name,
                        feature_name=feature_name,
                        location="",
                    )
                    _HM.add_operation(
                        self.project.name, "governance_contract_updated", "success",
                        {"feature": feature_name},
                        correlation_id=self._correlation_id,
                    )
                    # 自动收割教训（反复修改→成功的模式）
                    from backend.core.knowledge.lesson import harvest_lessons
                    ts = self.project.commit_format.get("prefix", "")
                    harvested = harvest_lessons(self.workspace_path, self.project.name,
                                               tech_stack=ts)
                    if harvested:
                        _HM.add_operation(
                            self.project.name, "governance_lesson", "success",
                            {"harvested_count": len(harvested)},
                            correlation_id=self._correlation_id,
                        )
                except OSError:
                    pass

            commit_hash = ""
            try:
                ch = self.ws_git_runner.rev_parse("HEAD", timeout=15)
                if ch:
                    commit_hash = ch
                    self.project.sync_base = ch
                    ConfigManager.save(self.config)
            except OSError:
                pass

            from backend.core.history import HistoryManager
            HistoryManager.add_entry(
                project_name=self.project.name,
                file_count=len(selected),
                commit_hash=commit_hash,
                commit_message=fc.message,
                workspace=str(self.workspace_path),
                backup=str(self.backup_path) if self.backup_path else "",
                correlation_id=self._correlation_id,
            )
            self._last_op = {"op": "sync", "status": "success",
                             "timestamp": datetime.now().isoformat()}
            self.save_session()
            self.on_log("同步成功！")
        else:
            self.stage = SessionStage.FAILED
            self.on_stage_changed(self.stage)
            self.on_log("同步失败")
        return success

    # ── 步骤 5: Push ────────────────────────────────────────

    def step_push(self, skip_scan: bool = False) -> tuple[bool, list[dict]]:
        """推送到远程仓库（批量推送所有 synced+unpushed 的 formal commit）。

        返回 (success, warnings)。
        """
        self.stage = SessionStage.PUSHING
        self.on_stage_changed(self.stage)

        if not self.backup_path or not self.bk_git_runner.is_git_repo():
            self.on_log("备份目录不是 git 仓库")
            return False, []

        targets = [fc for fc in self.formal_commits if fc.synced and not fc.pushed]
        if not targets:
            self.on_log("没有待 push 的正式 Commit")
            return False, []

        commit_refs = [f"[{fc.prefix}-{fc.number}]" for fc in targets]
        self.on_log(f"推送到远程仓库: {', '.join(commit_refs)}")

        # ── Gate B: Privacy Scan ──
        push_files = []
        for fc in targets:
            push_files.extend([e.rel_path for e in self.entries
                               if e.status != "same" and e.selected])
        if push_files:
            from backend.core.authorship import scan_files_privacy
            cfg = getattr(self.project, 'authorship', {}) or {}
            privacy_cfg = cfg.get("privacy", {})
            privacy_alerts = scan_files_privacy(
                str(self.workspace_path),
                list(set(push_files)),
                level=privacy_cfg.get("level", 2),
                deep_scan=privacy_cfg.get("deep_scan", False),
            )
            if privacy_alerts:
                errors = [a for a in privacy_alerts if a.get("level") == "error"]
                self.on_log(f"[Gate B] Privacy scan: {len(privacy_alerts)} alerts ({len(errors)} errors)")
                for a in privacy_alerts:
                    self.on_log(f"  [{a['rule']}] {a['message'][:100]}")
                if errors:
                    self.on_log("[Gate B] BLOCKED: privacy violations detected")
                    return False, [a["message"] for a in errors]

        success, warnings = push_to_backup(
            self.backup_path,
            progress_callback=self.on_progress,
            skip_scan=skip_scan,
            security_config=self.project.security_scan,
            plugin_ids=self.project.commit_format.get("plugins"),
            git_runner=self.bk_git_runner,
        )

        if not success and warnings and not skip_scan:
            self.on_log(f"安全检查发现 {len(warnings)} 项敏感信息")
            force = self.on_security_warning(warnings)
            if force:
                self.on_log("用户选择忽略警告，强制推送")
                return self.step_push(skip_scan=True)
            self.on_log("用户取消 push")
            return False, warnings

        if success:
            for fc in targets:
                fc.pushed = True
            self._last_op = {"op": "push", "status": "success",
                             "timestamp": datetime.now().isoformat()}
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                self.project.name, "push", "success",
                {"commits": commit_refs},
                correlation_id=self._correlation_id,
            )
            HistoryManager.add_operation(
                self.project.name, "governance_pushed", "success",
                {"commits": commit_refs, "count": len(targets)},
                correlation_id=self._correlation_id,
            )
            self.save_session()
            self.on_log(f"Push 成功！({len(targets)} commits)")
            return True, []
        else:
            self.stage = SessionStage.FAILED
            self.on_stage_changed(self.stage)
            self.on_log("Push 失败")
            return False, []

    # ── B-3: Accept 两步确认状态机 ─────────────────────────

    def step_start_accept_confirm(self, change: IncomingChange):
        """第一步：显示 Bridge，等待二次确认"""
        if self.stage != SessionStage.TRIAL_REVIEWING:
            self.on_log(f"[WARN] step_start_accept_confirm 需要 TRIAL_REVIEWING 阶段，当前为 {self.stage}")
            return
        self._pending_accept = change
        self.stage = SessionStage.INCOMING_CONFIRMING
        self.on_stage_changed(self.stage)

    def step_confirm_accept(self) -> IncomingChange | None:
        """第二步：用户确认，返回待处理的 change"""
        if self.stage != SessionStage.INCOMING_CONFIRMING:
            self.on_log(f"[WARN] step_confirm_accept 需要 INCOMING_CONFIRMING 阶段，当前为 {self.stage}")
            return None
        change = self._pending_accept
        self._pending_accept = None
        self.stage = SessionStage.IDLE
        self.on_stage_changed(self.stage)
        return change

    def step_cancel_accept(self):
        """用户取消，回到 REVIEWING"""
        if self.stage != SessionStage.INCOMING_CONFIRMING:
            self.on_log(f"[WARN] step_cancel_accept 需要 INCOMING_CONFIRMING 阶段，当前为 {self.stage}")
            return
        self._pending_accept = None
        self.stage = SessionStage.TRIAL_REVIEWING
        self.on_stage_changed(self.stage)

    # ── B-4: Incoming 已处理持久化 ─────────────────────────

    def _record_processed(self, hash: str, action: str):
        """记录已处理的 incoming commit，避免重启后重复显示"""
        from datetime import datetime
        if not hasattr(self.project, 'processed_incoming') or self.project.processed_incoming is None:
            self.project.processed_incoming = {}
        self.project.processed_incoming[hash] = {
            "action": action,
            "time": datetime.now().isoformat(),
        }
        ConfigManager.save(self.config)

    # ── 步骤 6: Create Release ────────────────────────────────

    def step_create_release(self, tag: str = "", name: str = "",
                            body: str = "") -> tuple[bool, str]:
        """在远程仓库创建 Release（GitHub/GitLab）。

        若无显式参数，则从最新 pushed formal commit 自动生成 tag/name/body。
        返回 (success, message)。
        """
        from backend.remote import create_connector

        release_node = self.project.release
        if not release_node or not release_node.remote:
            self.on_log("未配置远程仓库")
            return False, "未配置远程仓库"

        remote = release_node.remote
        connector = create_connector(remote)
        if not connector:
            self.on_log(f"不支持的远程仓库类型: {remote.kind}")
            return False, f"不支持的远程仓库类型: {remote.kind}"

        if not connector.is_configured():
            self.on_log(f"未配置 {remote.kind} 访问令牌")
            return False, f"未配置 {remote.kind} 访问令牌"

        # 自动从最新 pushed formal commit 生成参数
        if not tag or not body:
            pushed = [fc for fc in self.formal_commits if fc.pushed]
            if pushed:
                latest = pushed[-1]
                auto_tag = f"{latest.prefix}-{latest.number}"
                if not tag:
                    tag = auto_tag
                if not name:
                    name = auto_tag
                if not body:
                    body = latest.message
            elif not tag:
                self.on_log("没有可用的 pushed formal commit，且未指定 --tag")
                return False, "缺少 tag 参数"

        self.on_log(f"创建 Release: {tag}")
        ok, msg = connector.create_release(tag, name, body)
        if ok:
            self.on_log(f"Release 已创建: {msg}")
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                self.project.name, "release", "success",
                {"tag": tag, "name": name},
                correlation_id=self._correlation_id,
            )
        else:
            self.on_log(f"Release 创建失败: {msg}")
        return ok, msg

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

    # ── Session 持久化 ──────────────────────────────────────

    def save_session(self) -> Path:
        """持久化当前 session 状态到 .gitgo/session.json"""
        import json
        from backend.models import TrialAction

        session_dir = self.workspace_path / ".gitgo"
        session_dir.mkdir(exist_ok=True)
        data = {
            "project": self.project.name,
            "updated_at": datetime.now().isoformat(),
            "stage": self.stage.name,
            "entries_summary": {
                "total": len(self.entries),
                "new": sum(1 for e in self.entries if e.status == "new"),
                "modified": sum(1 for e in self.entries if e.status == "modified"),
            },
            "workspace_commits_since_base": len(self.commits),
            "formal_commits": [
                {
                    "message": fc.message,
                    "number": fc.number,
                    "prefix": fc.prefix,
                    "synced": fc.synced,
                    "pushed": fc.pushed,
                    "is_incoming": fc.is_incoming,
                    "sources_cleared": fc.sources_cleared,
                    "source_indices": list(fc.source_indices),
                    "created_at": fc.created_at,
                }
                for fc in self.formal_commits
            ],
            "incoming_summary": {
                "total": len(self.incoming_changes),
                "pending": sum(1 for c in self.incoming_changes
                              if c.triage == TrialAction.PENDING),
            },
            "last_operation": getattr(self, '_last_op', None),
        }
        path = session_dir / "session.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load_session(cls, project: ProjectConfig, config: Config):
        """从 .gitgo/session.json 恢复 session。返回 None 如果文件不存在。"""
        import json

        path = Path(project.workspace_path or Path.cwd()) / ".gitgo" / "session.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        session = cls(project, config)
        session.stage = SessionStage[data.get("stage", "IDLE")]
        for fc_data in data.get("formal_commits", []):
            fc = FormalCommit(
                message=fc_data["message"],
                number=fc_data["number"],
                prefix=fc_data["prefix"],
                synced=fc_data.get("synced", False),
                pushed=fc_data.get("pushed", False),
                is_incoming=fc_data.get("is_incoming", False),
                sources_cleared=fc_data.get("sources_cleared", False),
                source_indices=set(fc_data.get("source_indices", [])),
                created_at=fc_data.get("created_at", ""),
            )
            session.formal_commits.append(fc)
        return session

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
