"""Contract drift detection — check changed files against project contract."""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING, Any
from backend.core.policy.base import PolicyCheck

if TYPE_CHECKING:
    from backend.core.sync_session import SyncSession
    from backend.core.config import ProjectConfig


class ContractDriftCheck(PolicyCheck):
    name = "contract_drift"
    description = "Compare changed files against contract.yaml"

    def __init__(self, contract: Any = None):
        """contract 可选注入——loop 已加载时传入，避免重复读文件。"""
        self._contract = contract

    def check(self, session: SyncSession,
              _project: ProjectConfig) -> list[dict]:
        from backend.core.contract import ContractManager, detect_drift

        contract = self._contract or ContractManager.load(
            Path(session.workspace_path))
        if not contract:
            return []
        changed = [e.rel_path for e in session.entries if e.status != "same"]
        alerts = detect_drift(Path(session.workspace_path), changed, contract)
        if alerts:
            return [{"rule": d.get("rule", "contract"),
                     "message": d.get("message", ""),
                     "alert_count": len(alerts)} for d in alerts]
        return []
