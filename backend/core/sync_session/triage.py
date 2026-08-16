"""Trial 三叉阶段 — step_check_trial / step_triage_incoming / _record_processed。"""

from __future__ import annotations

from datetime import datetime

from backend.adapters.factory import create_adapters_for_node
from backend.core.config import ConfigManager
from backend.models import IncomingChange, TrialAction

from backend.core.sync_session.models import SessionStage, FormalCommit


class TrialMixin:
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
