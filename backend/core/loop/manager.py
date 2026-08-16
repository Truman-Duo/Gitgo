"""AgentProcessManager — 进程树生命周期管理。

fork → wait → kill → reap。daemon 调用 fork；daemon loop 调 reap 回收孤儿。

v0.45: 新增 SessionStore —— 会话持久化（append-only JSONL + atomic checkpoint）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel

if TYPE_CHECKING:
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.session import AgentSession


class AgentProcessManager:
    """进程树管理器。daemon 之上的调度层。"""

    # 集成点：daemon 在启动时创建实例，workspace_dirty 处理前调 fork，
    # 定期调 reap 回收孤儿
    MAX_FORK_DEPTH = 2

    def __init__(self):
        self._processes: dict[str, AgentProcess] = {}

    def fork(self, parent_id: str | None, role: str,
             tool_registry: "ToolRegistry", max_steps: int,
             ring_level: RingLevel,
             context_snapshot: dict | None = None,
             workspace_path: str = "",
             provider_id: str = "",
             model_id: str = "") -> AgentProcess:
        """创建子进程。parent_id=None 时为 A 级 Agent (ring 0)。

        B 级（parent_id 非 None）初始为 WAITING，agent_step 真正启动时转 RUNNING，
        让 dashboard 能显示真实的 "pending" 状态。"""
        # Fork depth ≤ 2 检查
        if parent_id is not None:
            parent = self._processes.get(parent_id)
            if parent and parent.parent_id is not None:
                raise ValueError(
                    f"Fork depth exceeds {self.MAX_FORK_DEPTH}. "
                    f"B-level agents cannot fork."
                )

        # B 级 Agent 的 context_snapshot 不能包含 contract 原文或完整 lessons
        if ring_level == RingLevel.RING_3 and context_snapshot:
            forbidden = {"contract_yaml", "lessons_full", "contract_raw"}
            if forbidden & set(context_snapshot.keys()):
                raise ValueError(
                    "B-level agent context must not contain raw contract "
                    "or full lessons. Use governance_brief summaries instead."
                )

        process = AgentProcess(
            process_id=str(uuid.uuid4()),
            role=role,
            ring_level=ring_level,
            tool_registry=tool_registry,
            max_steps=max_steps,
            status=ProcessStatus.WAITING if parent_id is not None else ProcessStatus.RUNNING,
            parent_id=parent_id,
            context_snapshot=context_snapshot,
            created_at=datetime.now().isoformat(),
            worktree_path=workspace_path,
            provider_id=provider_id,
            model_id=model_id,
        )
        self._processes[process.process_id] = process

        from backend.core.history import HistoryManager
        HistoryManager.add_operation(
            "system", "agent_forked", "success",
            {"process_id": process.process_id, "role": role,
             "ring_level": ring_level.value, "parent_id": parent_id},
            correlation_id=process.process_id,
        )
        return process

    def wait(self, process_id: str,
             timeout: float | None = None) -> dict | None:
        """阻塞直到子进程完成。超时返回 None 不 kill。"""
        process = self._processes.get(process_id)
        if not process:
            return None

        start = time.time()
        while process.status in (ProcessStatus.RUNNING, ProcessStatus.WAITING):
            if timeout and time.time() - start > timeout:
                return None
            time.sleep(0.5)

        return process.result

    def kill(self, process_id: str) -> None:
        """终止子进程。标记为 KILLED 并置取消标志以真停任务线程。"""
        process = self._processes.get(process_id)
        if process and process.status in (ProcessStatus.RUNNING, ProcessStatus.WAITING):
            process.cancel_requested = True
            process.status = ProcessStatus.KILLED
            from backend.core.history import HistoryManager
            HistoryManager.add_operation(
                "system", "agent_killed", "success",
                {"process_id": process_id, "role": process.role},
                correlation_id=process_id,
            )

    def reap(self) -> list[AgentProcess]:
        """回收孤儿进程——父进程已死但本身还在的进程。"""
        active_ids = {p.process_id for p in self._processes.values()
                      if p.status in (ProcessStatus.RUNNING, ProcessStatus.WAITING)}
        orphans = []
        for p in self._processes.values():
            if p.parent_id and p.parent_id not in active_ids:
                p.status = ProcessStatus.ORPHANED
                orphans.append(p)
                from backend.core.history import HistoryManager
                HistoryManager.add_operation(
                    "system", "agent_reaped", "success",
                    {"process_id": p.process_id, "role": p.role,
                     "parent_id": p.parent_id},
                    correlation_id=p.process_id,
                )
        return orphans

    def get(self, process_id: str) -> AgentProcess | None:
        return self._processes.get(process_id)


# ── v0.45: Session Persistence (JSONL + atomic checkpoint) ─────

class SessionStore:
    """会话持久化 —— append-only JSONL + atomic checkpoint。

    每条事件追加一行 JSON 到 .gitgo/sessions/{pid}.jsonl。
    定期做 atomic checkpoint（tmp→rename）全量快照。
    崩溃恢复：优先 checkpoint → 缺失部分从 jsonl replay。
    """

    SESSIONS_DIR = ".gitgo/sessions"
    MAX_JSONL_LINES = 500  # 超过此值触发 checkpoint 压缩

    def __init__(self, workspace_path: str):
        self._ws = Path(workspace_path)
        self._dir = self._ws / self.SESSIONS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────

    def append_event(self, process_id: str, event_type: str,
                     data: dict | None = None) -> None:
        """追加一行 JSON 事件到 jsonl 文件。"""
        entry = {
            "ts": datetime.now().isoformat(),
            "event": event_type,
            "data": data or {},
        }
        jsonl_path = self._jsonl_path(process_id)
        try:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 持久化失败不阻塞主流程

    def save_checkpoint(self, process_id: str,
                        session: "AgentSession") -> str | None:
        """Atomic write 全量快照。

        tmp 写入 → os.replace（原子 rename）→ 返回 checkpoint 路径。
        同时截断 jsonl（已合并到 checkpoint）。
        """
        checkpoint_data = {
            "process_id": process_id,
            "checkpoint_at": datetime.now().isoformat(),
            "messages": session.messages,
        }
        tmp_path = self._dir / f"{process_id}.checkpoint.tmp"
        final_path = self._dir / f"{process_id}.checkpoint.json"
        try:
            tmp_path.write_text(
                json.dumps(checkpoint_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(final_path))
            # 截断 jsonl——checkpoint 已包含全量
            jsonl_path = self._jsonl_path(process_id)
            if jsonl_path.exists():
                jsonl_path.write_text("", encoding="utf-8")
            return str(final_path)
        except OSError:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return None

    def load_session(self, process_id: str) -> list[dict] | None:
        """加载会话消息列表。

        优先级：checkpoint → replay jsonl → 合并。
        返回 None 表示无任何持久化数据。
        """
        messages: list[dict] = []
        checkpoint_path = self._dir / f"{process_id}.checkpoint.json"
        jsonl_path = self._jsonl_path(process_id)

        # 1. 加载 checkpoint
        if checkpoint_path.exists():
            try:
                data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                messages = data.get("messages", [])
            except (json.JSONDecodeError, OSError):
                pass

        # 2. Replay jsonl（checkpoint 之后的事件）
        if jsonl_path.exists():
            try:
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev_type = entry.get("event", "")
                    ev_data = entry.get("data", {})
                    if ev_type == "message_append":
                        msg = ev_data.get("message")
                        if msg:
                            messages.append(msg)
                    elif ev_type == "message_pop":
                        # rollback 裁剪
                        count = ev_data.get("count", 1)
                        for _ in range(min(count, len(messages))):
                            messages.pop()
            except OSError:
                pass

        return messages if messages else None

    def delete_session(self, process_id: str) -> None:
        """清理会话持久化文件（正常完成时调用）。"""
        for suffix in (".jsonl", ".checkpoint.json", ".checkpoint.tmp"):
            p = self._dir / f"{process_id}{suffix}"
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def list_incomplete(self) -> list[str]:
        """扫描 .gitgo/sessions/ 找出有持久化数据但尚未清理的 process_id。

        用于 daemon 启动时恢复。
        """
        ids: set[str] = set()
        try:
            for entry in self._dir.iterdir():
                name = entry.name
                # {pid}.jsonl 或 {pid}.checkpoint.json
                if name.endswith(".jsonl"):
                    ids.add(name[:-6])
                elif name.endswith(".checkpoint.json"):
                    ids.add(name[:-16])
        except OSError:
            pass
        return sorted(ids)

    def should_checkpoint(self, process_id: str) -> bool:
        """检查 jsonl 行数是否超过阈值，需要触发 checkpoint。"""
        jsonl_path = self._jsonl_path(process_id)
        if not jsonl_path.exists():
            return False
        try:
            text = jsonl_path.read_text(encoding="utf-8")
            return text.count("\n") >= self.MAX_JSONL_LINES
        except OSError:
            return False

    # ── Internal ─────────────────────────────────────────────

    def _jsonl_path(self, process_id: str) -> Path:
        return self._dir / f"{process_id}.jsonl"
