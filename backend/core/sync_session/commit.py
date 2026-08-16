"""Formal commit 管理阶段 — 创建 + 7 个增删改方法。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.core.operations import (
    _find_next_number,
    build_commit_template,
    validate_commit_message,
)

from backend.core.sync_session.models import SessionStage, FormalCommit


class CommitMixin:
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
