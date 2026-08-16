"""Sync/Push 阶段 — step_sync / step_push。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.core.config import ConfigManager
from backend.core.operations import push_to_backup, sync_to_backup

from backend.core.sync_session.models import SessionStage


class SyncPushMixin:
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

        if not self.backup_path:
            self.on_log("未配置备份路径")
            return False

        self.on_log(f"同步到备份仓库: {fc.message.split(chr(10))[0]}")

        # ── Gate A: 可插拔 Gate 检查（通过 contract.yaml gates.sync 配置）──
        from backend.core.policy.gates import load_gates

        # 尝试读取 daemon 预计算的 drift_cache（系统维护，非 LLM 维护）
        _drift_cache = None
        try:
            from backend.core.history import HistoryManager
            entries = HistoryManager.load()
            cached = [e for e in entries if e.operation == "drift_cache"][-1:]
            if cached:
                _drift_cache = cached[0].detail
            else:
                _drift_cache = {}
        except Exception:
            _drift_cache = {}

        gate_blocked = False
        for gate in load_gates("sync", str(self.workspace_path)):
            # 注入 drift_cache 到 gate 实例
            if hasattr(gate, '_cache') and _drift_cache is not None:
                gate._cache = _drift_cache
            result = gate.check(self, self.project, fc, selected)
            prefix = f"[Gate A/{gate.name}]"

            if result.message:
                level_tag = result.level.upper()
                self.on_log(f"{prefix} {level_tag}: {result.message}")

            # 记录告警到 HistoryManager
            if result.alerts:
                HistoryManager.add_operation(
                    self.project.name, "governance_drift", "warning",
                    {"alert_count": len(result.alerts),
                     "rules": [a.get("rule", gate.name) for a in result.alerts]},
                    correlation_id=self._correlation_id,
                )
            elif result.rule and result.rule != "contract_drift":
                # 非 drift 类告警也记录（如 foreign_commit）
                HistoryManager.add_operation(
                    self.project.name, "governance_drift", result.level,
                    {"rule": result.rule, "message": result.message},
                    correlation_id=self._correlation_id,
                )

            if result.blocked and gate.fail_action == "block":
                self.on_log(f"{prefix} BLOCKED: {result.message}")
                gate_blocked = True
                break

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

        # ── Gate B: 可插拔 Gate 检查（通过 contract.yaml gates.push 配置）──
        from backend.core.policy.gates import load_gates

        push_files = []
        for fc in targets:
            push_files.extend([e.rel_path for e in self.entries
                               if e.status != "same" and e.selected])
        selected_for_push = [e for e in self.entries
                            if e.status != "same" and e.selected]

        for gate in load_gates("push", str(self.workspace_path)):
            result = gate.check(self, self.project, targets[0], selected_for_push)
            prefix = f"[Gate B/{gate.name}]"

            if result.message:
                self.on_log(f"{prefix}: {result.message}")
            for a in result.alerts:
                self.on_log(f"  [{a.get('rule', gate.name)}] {a.get('message', '')[:100]}")

            if result.blocked and gate.fail_action == "block":
                self.on_log(f"{prefix} BLOCKED: {result.message}")
                return False, [a.get("message", "") for a in result.alerts]

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
