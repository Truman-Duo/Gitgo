"""Dashboard CLI — 实时显示 gitgo 关联项目动态"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from backend.core.config import Config
from backend.core.history import HistoryManager
from backend.core.state_reader import StateReader


def _get_last_gate_result(project_name: str, workspace_path: str) -> dict:
    """读取最近一次 Gate A 结果。"""
    entries = HistoryManager.load()
    project_entries = [e for e in entries if e.project_name == project_name]

    # 找最近的 governance_synced（成功）或 governance_drift（blocked）
    last_sync = None
    last_drift = None
    for e in reversed(project_entries):
        if e.operation == "governance_synced" and last_sync is None:
            detail = e.detail if isinstance(e.detail, dict) else {}
            last_sync = {"status": "passed", "commit": detail.get("commit", "?"),
                         "time": e.timestamp[:19]}
        if e.operation == "governance_drift" and last_drift is None:
            detail = e.detail if isinstance(e.detail, dict) else {}
            last_drift = {"status": "blocked",
                          "rules": detail.get("rules", []),
                          "time": e.timestamp[:19]}
        if last_sync and last_drift:
            break

    if last_sync is None and last_drift is None:
        return {"status": "idle"}

    # 比较时间戳，取最新的
    if last_sync and last_drift:
        return last_sync if last_sync["time"] > last_drift["time"] else last_drift
    return last_sync or last_drift


def _count_pending_lessons(workspace_path: str, project_name: str) -> int:
    """统计待确认教训数。"""
    try:
        fp = Path(workspace_path) / ".gitgo" / "knowledge" / "instances" / project_name / "pending.jsonl"
        if not fp.exists():
            return 0
        return sum(1 for _ in fp.read_text(encoding="utf-8").splitlines() if _.strip())
    except OSError:
        return 0


def _get_contract_summary(workspace_path: str) -> dict:
    """获取合约摘要。"""
    from backend.core.contract import ContractManager
    c = ContractManager.load(Path(workspace_path))
    if c is None:
        return {"features": 0, "constraints": 0}
    return {"features": len(c.decided_features), "constraints": len(c.architecture_constraints)}


def _fmt_status(status: str) -> str:
    """格式化状态显示。"""
    if status == "passed":
        return "[green]PASSED[/green]"
    elif status == "blocked":
        return "[red]BLOCKED[/red]"
    return "[dim]IDLE[/dim]"


def cmd_dashboard(cfg: Config, refresh: int = 10) -> None:
    """--mode dashboard: 实时项目状态面板。

    refresh: 刷新间隔秒数（0 = 只刷新一次）
    """
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text

    console = Console()

    def _build_table() -> Table:
        table = Table(title="Gitgo Project Dashboard", title_style="bold white")
        table.add_column("Project", style="cyan", width=12)
        table.add_column("Gate A", width=18)
        table.add_column("Commit", width=14)
        table.add_column("Lessons", width=8, justify="right")
        table.add_column("Contract", width=14)
        table.add_column("Last Event", width=20)

        for proj in cfg.projects:
            ws = proj.workspace_path
            if not ws:
                continue
            gate = _get_last_gate_result(proj.name, ws)
            pending = _count_pending_lessons(ws, proj.name)
            contract = _get_contract_summary(ws)

            table.add_row(
                proj.name,
                _fmt_status(gate["status"]),
                gate.get("commit", "-"),
                str(pending),
                f"{contract['features']} feat / {contract['constraints']} constr",
                gate.get("time", "-"),
            )
        return table

    def _build_recent_events() -> str:
        lines = []
        entries = HistoryManager.load()[-10:]
        for e in reversed(entries):
            op = getattr(e, 'operation', '')
            if op.startswith("governance_"):
                op = op.replace("governance_", "")
            ts = getattr(e, 'timestamp', '')[:19] if hasattr(e, 'timestamp') else ""
            lines.append(f"[dim]{ts}[/dim] [cyan]{op}[/cyan] {e.status}")
        return "\n".join(lines) if lines else "(no events)"

    layout = Layout()
    layout.split_column(
        Layout(name="projects"),
        Layout(name="events", size=12),
    )

    if refresh > 0:
        with Live(layout, console=console, refresh_per_second=1.0 / refresh, screen=True) as live:
            while True:
                try:
                    layout["projects"].update(Panel(_build_table(), title="Projects"))
                    layout["events"].update(Panel(_build_recent_events(), title="Governance Events"))
                    time.sleep(refresh)
                except KeyboardInterrupt:
                    break
    else:
        layout["projects"].update(Panel(_build_table(), title="Projects"))
        layout["events"].update(Panel(_build_recent_events(), title="Governance Events"))
        console.print(layout)
