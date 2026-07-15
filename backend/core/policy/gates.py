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

        # 尝试复用 drift_cache（PolicyEngine 产出，watcher dirty 时失效）
        # cache 存储在 daemon_ctx 中，通过 step_sync 调用时传入
        cache = getattr(self, '_cache', None)
        if cache is not None and not cache.get("dirty", True):
            alerts = cache.get("alerts", [])
            errors = [a for a in alerts if a.get("level") == "error"]
            return GateResult(
                blocked=len(errors) > 0,
                rule="contract_drift",
                level="error" if errors else "warning",
                message=f"{len(alerts)} drift alerts ({len(errors)} errors) [cached]",
                alerts=alerts,
            )

        # 缓存不可用 → 现场检测
        contract = ContractManager.load(session.workspace_path)
        if not contract:
            return GateResult()

        changed_paths = [e.rel_path for e in selected_entries]
        drift_alerts = detect_drift(session.workspace_path, changed_paths, contract)
        dep_alerts = check_feature_signatures(
            session.workspace_path, changed_paths, contract,
        )
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
        cfg = getattr(project, 'authorship', {}) or {}
        privacy_cfg = cfg.get("privacy", {})
        privacy_alerts = scan_files_privacy(
            str(session.workspace_path),
            push_files,
            level=privacy_cfg.get("level", 2),
            deep_scan=privacy_cfg.get("deep_scan", False),
        )
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

    # 尝试从 contract.yaml 加载配置
    gates_cfg: dict = {}
    try:
        import yaml
        contract_path = Path(workspace_path) / ".gitgo" / "contract.yaml"
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        gates_cfg = raw.get("gates", {}).get(context, {})
    except Exception:
        pass

    enabled = gates_cfg.get("enabled", list(builtins.keys()))
    disabled = gates_cfg.get("disabled", [])
    overrides = gates_cfg.get("config", {})

    instances: list[SyncGate] = []
    for name, cls in builtins.items():
        if name in disabled:
            continue
        if enabled and name not in enabled:
            continue

        instance = cls()
        # 允许 contract.yaml 覆盖 order 和 fail_action
        cfg = overrides.get(name, {})
        if "order" in cfg:
            instance.order = cfg["order"]
        if "fail_action" in cfg:
            instance.fail_action = cfg["fail_action"]

        instances.append(instance)

    instances.sort(key=lambda g: g.order)
    return instances


def register_gate(context: str, gate_cls: type[SyncGate]) -> None:
    """注册自定义 Gate。"""
    _BUILTIN_GATES.setdefault(context, {})[gate_cls.name] = gate_cls
