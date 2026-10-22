"""SyncGate — 可插拔 Gate A/B 检查（与 PolicyCheck 对称架构）。

v0.33 E1-fix: step_sync / step_push 中的硬编码检查提取为可插拔 SyncGate。
            通过 contract.yaml 的 gates.sync / gates.push 配置启用/禁用/排序。

用法：
    gates = load_gates("sync", workspace_path)
    for gate in gates:
        result = gate.check(session, project, formal_commit, selected_entries)
        if result.blocked:
            return False
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.sync_session import SyncSession, FormalCommit
    from backend.core.config import ProjectConfig


@dataclass
class GateResult:
    """Gate 检查结果。"""
    allowed: bool = True
    blocked: bool = False
    rule: str = ""
    message: str = ""
    level: str = "warning"  # "error" | "warning" | "info"
    alerts: list[dict] = field(default_factory=list)


class SyncGate(ABC):
    """Gate 检查基类——与 PolicyCheck 对称。

    每个 Gate 声明：
    - name: 用于 contract.yaml 引用
    - description: 一句话描述
    - order: 执行顺序（越小越先执行）
    - fail_action: "block"（阻断）| "warn"（警告但放行）| "log"（仅记录）
    """

    name: str = ""
    description: str = ""
    order: int = 100
    fail_action: str = "warn"  # block | warn | log

    @abstractmethod
    def check(
        self,
        session: "SyncSession",
        project: "ProjectConfig",
        formal_commit: "FormalCommit",
        selected_entries: list,
    ) -> GateResult:
        """执行 Gate 检查。"""
        ...


# ── 内置 Gate 实现 ──────────────────────────────────────────


class ContractConfigurationGate(SyncGate):
    """Stop publication when its gate configuration cannot be established."""
    name = "contract_configuration"
    order = -1
    fail_action = "block"

    def check(self, session, project, formal_commit, selected_entries):
        return GateResult(allowed=False, blocked=True, rule="contract_state_unavailable",
                          level="error", message="CONTRACT_STATE_UNAVAILABLE: publication gate configuration is unreadable")


class ForeignCommitGate(SyncGate):
    """检测 release repo 是否有外来 commit。"""

    name = "foreign_commit"
    description = "检测 release repo 中未经 gitgo 同步的外来 commit"
    order = 10
    fail_action = "warn"

    def check(self, session, project, formal_commit, selected_entries):
        if not session.backup_path or not session.bk_git_runner.is_git_repo():
            return GateResult()

        try:
            current_head = session.bk_git_runner.rev_parse("HEAD")
        except Exception:
            return GateResult()

        if not current_head:
            return GateResult()

        release_node = project.release
        recorded = release_node.last_known_head if release_node else ""
        if recorded and current_head != recorded:
            return GateResult(
                blocked=False,
                rule="foreign_commit_detected",
                level="warning",
                message=(
                    f"Release repo 有外来 commit: "
                    f"recorded={recorded[:12]}, current={current_head[:12]}"
                ),
            )
        return GateResult()


class ContractDriftGate(SyncGate):
    """合约漂移检测（提取自 step_sync Gate A）。"""

    name = "contract_drift"
    description = "检测变更文件是否违反项目合约（签名丢失 + 技术栈漂移 + 架构约束）"
    order = 20
    fail_action = "block"

    def check(self, session, project, formal_commit, selected_entries):
        from backend.core.contract import ContractManager, detect_drift, check_feature_signatures
        from backend.core.history import HistoryManager

        # Cached alerts are an observation projection, not publication
        # authority: dirty=False alone binds neither contract nor source bytes.
        cache = getattr(self, '_cache', None)
        from backend.core.contract import ContractStateUnavailable
        try:
            contract = ContractManager.load(session.workspace_path)
        except ContractStateUnavailable:
            return GateResult(allowed=False, blocked=True, rule="contract_state_unavailable",
                              level="error", message="CONTRACT_STATE_UNAVAILABLE: drift not evaluated")
        if not contract:
            return GateResult()

        changed_paths = [e.rel_path for e in selected_entries]
        try:
            drift_alerts = detect_drift(session.workspace_path, changed_paths, contract)
            dep_alerts = check_feature_signatures(session.workspace_path, changed_paths, contract)
        except ContractStateUnavailable:
            return GateResult(allowed=False, blocked=True, rule="contract_source_unavailable",
                              level="error", message="CONTRACT_STATE_UNAVAILABLE: source observation failed")
        all_alerts = drift_alerts + dep_alerts

        # 更新缓存（供后续 Gate 调用复用）
        if cache is not None:
            cache["alerts"] = all_alerts
            cache["dirty"] = False

        errors = [a for a in all_alerts if a.get("level") == "error"]
        return GateResult(
            blocked=len(errors) > 0,
            rule="contract_drift",
            level="error" if errors else "warning",
            message=f"{len(all_alerts)} drift alerts ({len(errors)} errors)" if all_alerts else "",
            alerts=all_alerts,
        )


class PrivacyScanGate(SyncGate):
    """隐私扫描（提取自 step_push Gate B）。"""

    name = "privacy_scan"
    description = "扫描待推送文件中的敏感信息（API key / 私钥 / 内部路径）"
    order = 10
    fail_action = "block"

    def check(self, session, project, formal_commit, selected_entries):
        push_files = list(set(
            e.rel_path for e in selected_entries if e.status != "same"
        ))
        if not push_files:
            return GateResult()

        from backend.core.authorship import scan_files_privacy
        policy = dict(getattr(project, "outbound_policy", {}) or {})
        if policy and not policy.get("enabled", True):
            return GateResult()
        cfg = getattr(project, 'authorship', {}) or {}
        privacy_cfg = cfg.get("privacy", {})
        try:
            privacy_alerts = scan_files_privacy(
                str(session.workspace_path), push_files,
                level=policy.get("content_level", privacy_cfg.get("level", 2)),
                deep_scan=policy.get("deep_scan", privacy_cfg.get("deep_scan", False)),
                approved_fingerprints=policy.get("approved_fingerprints", []))
        except (OSError, ValueError):
            return GateResult(allowed=False, blocked=True, rule="privacy_scan_unavailable", level="error",
                              message="PRIVACY_OBSERVATION_UNAVAILABLE: publication scan is incomplete")
        if not privacy_alerts:
            return GateResult()

        errors = [a for a in privacy_alerts if a.get("level") == "error"]
        return GateResult(
            blocked=len(errors) > 0,
            rule="privacy_scan",
            level="error" if errors else "warning",
            message=f"{len(privacy_alerts)} privacy alerts ({len(errors)} errors)",
            alerts=privacy_alerts,
        )


# ── Registry ────────────────────────────────────────────────


# 内置 Gate 按 context 注册
_BUILTIN_GATES: dict[str, dict[str, type[SyncGate]]] = {
    "sync": {
        "privacy_scan": PrivacyScanGate,
        "foreign_commit": ForeignCommitGate,
        "contract_drift": ContractDriftGate,
    },
    "push": {
        "privacy_scan": PrivacyScanGate,
    },
}


def load_gates(context: str, workspace_path: str) -> list[SyncGate]:
    """加载指定 context 的 Gate 实例。

    context: "sync" | "push"

    先从 contract.yaml 读取 gates.{context} 配置（支持 enabled/disabled/order），
    无配置时 fallback 到全部内置 Gate（按默认 order 排序）。
    """
    builtins = _BUILTIN_GATES.get(context, {})
    if not builtins:
        return []

    from backend.core.contract import ContractManager, ContractStateUnavailable
    try:
        contract = ContractManager.load(Path(workspace_path))
        extensions = getattr(contract, "_extension_fields", {})
        gates = extensions.get("gates", {}) or {}
        if not isinstance(gates, dict):
            raise ValueError("gates must be a mapping")
        gates_cfg = gates.get(context, {}) or {}
        if not isinstance(gates_cfg, dict):
            raise ValueError("gate context must be a mapping")
        for key in ("enabled", "disabled"):
            value = gates_cfg.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError("gate names must be a list of strings")
        overrides = gates_cfg.get("config", {})
        if not isinstance(overrides, dict):
            raise ValueError("gate config must be a mapping")
        for cfg in overrides.values():
            if not isinstance(cfg, dict):
                raise ValueError("gate override must be a mapping")
            if "order" in cfg and type(cfg["order"]) is not int:
                raise ValueError("gate order must be an integer")
            if "fail_action" in cfg and cfg["fail_action"] not in {"block", "warn", "log"}:
                raise ValueError("gate fail_action is invalid")
    except (ContractStateUnavailable, ValueError) as exc:
        # Invalid routing/configuration cannot silently select defaults and
        # publish. Keep mandatory/default checks and an explicit blocking fact.
        return [ContractConfigurationGate(), *[cls() for cls in builtins.values()]]

    enabled = gates_cfg.get("enabled", list(builtins.keys()))
    disabled = gates_cfg.get("disabled", [])
    overrides = gates_cfg.get("config", {})

    instances: list[SyncGate] = []
    for name, cls in builtins.items():
        mandatory = name == "privacy_scan" and context in {"sync", "push"}
        if name in disabled and not mandatory:
            continue
        if enabled and name not in enabled and not mandatory:
            continue

        instance = cls()
        # 允许 contract.yaml 覆盖 order 和 fail_action
        cfg = overrides.get(name, {})
        if "order" in cfg:
            instance.order = cfg["order"]
        if "fail_action" in cfg:
            instance.fail_action = cfg["fail_action"]
        if mandatory:
            instance.fail_action = "block"

        instances.append(instance)

    instances.sort(key=lambda g: g.order)
    return instances


def register_gate(context: str, gate_cls: type[SyncGate]) -> None:
    """注册自定义 Gate。"""
    _BUILTIN_GATES.setdefault(context, {})[gate_cls.name] = gate_cls
