"""策略注册表 — 从 contract.yaml 加载项目级策略配置。"""

from pathlib import Path
from backend.core.policy.base import PolicyCheck
from backend.core.policy.lessons import LessonTriggerCheck
from backend.core.policy.contract import ContractDriftCheck
from backend.core.policy.identity import IdentityIntegrityCheck
from backend.core.policy.dependency import DependencyChainCheck

_ALL_CHECKS: dict[str, type[PolicyCheck]] = {
    "lesson_triggers": LessonTriggerCheck,
    "contract_drift": ContractDriftCheck,
    "identity_integrity": IdentityIntegrityCheck,
    "dependency_chain": DependencyChainCheck,
}


def load_checks(project_name: str,
                workspace_path: Path) -> list[PolicyCheck]:
    """从 contract.yaml 加载策略。未配置时返回全部默认策略。"""
    from backend.core.contract import ContractManager

    contract = ContractManager.load(workspace_path)
    enabled_names = set(_ALL_CHECKS.keys())

    if contract:
        raw = contract.to_dict()
        policy_cfg = raw.get("policy_checks", {})
        if isinstance(policy_cfg, dict):
            cfg_enabled = policy_cfg.get("enabled", [])
            cfg_disabled = policy_cfg.get("disabled", [])
            if cfg_enabled:
                enabled_names = set(cfg_enabled)
            enabled_names -= set(cfg_disabled)

    return [cls() for name, cls in _ALL_CHECKS.items() if name in enabled_names]


def register_check(name: str, cls: type[PolicyCheck]) -> None:
    """注册自定义策略。第三方/项目可调此函数添加新策略。"""
    _ALL_CHECKS[name] = cls
