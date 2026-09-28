"""Policy Engine — 可插拔治理策略框架。"""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING, Any
from backend.core.policy.base import PolicyCheck
from backend.core.policy.lessons import LessonTriggerCheck
from backend.core.policy.contract import ContractDriftCheck
from backend.core.policy.identity import IdentityIntegrityCheck
from backend.core.policy.dependency import DependencyChainCheck

if TYPE_CHECKING:
    from backend.core.sync_session import SyncSession
    from backend.core.config import ProjectConfig

__all__ = ["PolicyEngine", "PolicyCheck",
           "LessonTriggerCheck", "ContractDriftCheck",
           "IdentityIntegrityCheck", "DependencyChainCheck"]


class PolicyEngine:
    """运行一组 PolicyCheck 策略，返回结果字典。"""

    def __init__(self, checks: list[PolicyCheck] | None = None,
                 contract: Any = None, lessons: list | None = None,
                 changed_files: list[str] | None = None):
        self._checks = checks or self._defaults(
            contract, lessons, changed_files=changed_files,
        )
        # Registry-loaded checks still consume the same authoritative watcher
        # event.  The hint is deliberately scoped to this PolicyEngine run;
        # it is not persisted on SyncSession where it could become stale.
        if changed_files is not None:
            for check in self._checks:
                if isinstance(check, LessonTriggerCheck):
                    check.set_changed_files(changed_files)

    @staticmethod
    def _defaults(contract: Any = None,
                  lessons: list | None = None,
                  changed_files: list[str] | None = None) -> list[PolicyCheck]:
        return [
            LessonTriggerCheck(lessons=lessons, changed_files=changed_files),
            ContractDriftCheck(contract=contract),
            IdentityIntegrityCheck(),
            DependencyChainCheck(),
        ]

    @classmethod
    def from_project(cls, project_name: str,
                     workspace_path: Path,
                     changed_files: list[str] | None = None) -> "PolicyEngine":
        """从 contract.yaml 加载项目级策略配置和 contract 对象。"""
        from backend.core.policy.registry import load_checks
        from backend.core.contract import ContractManager
        contract = ContractManager.load(workspace_path)
        checks = load_checks(project_name, workspace_path)
        # Inject contract into checks that accept it
        for c in checks:
            if hasattr(c, '_contract') and c._contract is None:
                c._contract = contract
        return cls(checks=checks, changed_files=changed_files)

    def run(self, session: "SyncSession",
            project: "ProjectConfig", *, task_kind: str = "") -> dict:
        """运行所有策略。返回 {check_name: [alerts]}。"""
        results: dict = {}
        for check in self._checks:
            if not check.applies_to(task_kind):
                continue
            results[check.name] = check.check(session, project)
        return results


def build_policy_message(results: dict) -> str:
    """构建给 agent 看的修正需求消息。"""
    parts = []
    for lesson in results.get("lesson_triggers", []):
        parts.append(f"[{lesson['severity']}] lesson[{lesson['lesson_id']}]: {lesson['rule']}")
    for d in results.get("contract_drift", []):
        parts.append(f"[warning] contract drift: {d['message']}")
    for i in results.get("identity_integrity", []):
        parts.append(f"[{i.get('level','warning')}] integrity: {i.get('message','')}")
    for dc in results.get("dependency_chain", []):
        parts.append(f"[info] dep-chain: {dc['message']}")
    if parts:
        return "本轮变更匹配到以下治理规则，请先修正：\n" + "\n".join(parts)
    return ""


def should_harvest(workspace_path: Path,
                   project_name: str,
                   warning_threshold: int = 3) -> bool:
    """检查最近的 policy_check_result 是否连续 N 次 warning——触发收割条件。"""
    from backend.core.history import HistoryManager
    entries = HistoryManager.load()
    recent = [e for e in entries
              if e.project_name == project_name
              and e.operation == "policy_check_result"]
    if len(recent) < warning_threshold:
        return False
    return all(e.status == "warning" for e in recent[-warning_threshold:])


def run_harvest_if_needed(workspace_path: Path,
                          project_name: str,
                          tech_stack: str = "",
                          warning_threshold: int = 3) -> int:
    """条件收割——连续 N 次 policy_check_result=warning 时触发。返回收割数。"""
    if not should_harvest(workspace_path, project_name, warning_threshold):
        return 0
    from backend.core.knowledge.harvest import harvest_lessons
    harvested = harvest_lessons(workspace_path, project_name, tech_stack)
    return len(harvested)
