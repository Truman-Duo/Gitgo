"""ToolExecution —— 工具批次事务。

一次 LLM 回复里的多个工具调用构成一个 Execution 事务：
- begin: 拍 workspace snapshot（内容级 SHA256 备份），发射 ExecutionStarted
- execute_batch: 按资源冲突分组调度；CRASH → 整批回滚；BUSINESS → 不回滚
- commit: 发射 ExecutionCompleted
- rollback: 恢复文件快照 + 裁剪会话 + 注入回滚通知 + 发射 rollback_notification

Pipeline 负责"一次工具调用怎么做"，Execution 负责"这群工具调用作为一个事务怎么做"。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.event_bus import ExecutionEvent
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolResult

SNAPSHOT_DIR = ".gitgo/snapshots"
MAX_SNAPSHOTS = 20


@dataclass
class ToolExecution:
    """一次工具批次事务。"""

    execution_id: str
    ctx: "ExecutionContext"
    tool_calls: list[dict]
    results: list["ToolResult"] = field(default_factory=list)
    snapshot: dict | None = None
    status: str = "pending"   # pending | running | completed | failed | cancelled
    idempotency_key: str = field(default="", repr=False)
    _rolled_back: bool = field(default=False, repr=False)

    def __post_init__(self):
        if not self.idempotency_key:
            tool_names = ",".join(sorted(tc.get("name", "") for tc in self.tool_calls))
            step = getattr(self.ctx, "process", None)
            step_num = step.steps_used if step and hasattr(step, "steps_used") else 0
            pid = step.process_id if step and hasattr(step, "process_id") else "unknown"
            raw = f"{pid}:{step_num}:{tool_names}"
            self.idempotency_key = hashlib.sha256(raw.encode()).hexdigest()[:16]

    def begin(self) -> None:
        """开始事务：拍 workspace 内容级快照，发射事件。"""
        self.snapshot = self._take_snapshot()
        self.status = "running"
        self.ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ExecutionEvent"])
            .ExecutionEvent("ExecutionStarted", execution_id=self.execution_id),
        )

    def execute_batch(self, tools: dict[str, "AgentTool"],
                      max_parallel: int = 8) -> list["ToolResult"]:
        """按资源冲突分组调度执行。

        v0.40: 替代 read_only 二值分区。两个写工具操作不同资源时可并行。
        v0.45: CRASH → 立即整批回滚；BUSINESS → 不回滚，作为正常结果返回。

        分组规则：
        - 无资源声明的工具（读工具）：全进一个无冲突组，组内并行
        - 有资源声明的工具：资源交集非空→同组串行，不同组→可并行
        - 跨组之间：后一组等前一组全部完成

        Returns: 按调用顺序排列的 ToolResult 列表。
        """
        from backend.core.loop.tool_pipeline import ToolPipeline
        from backend.core.loop.error_taxonomy import ErrorNature

        pipeline = ToolPipeline()

        # 解析每个工具调用的实际资源
        resolved: list[tuple[int, dict, "AgentTool | None", set[str]]] = []
        for i, tc in enumerate(self.tool_calls):
            tool_name = tc.get("name", "")
            tool = tools.get(tool_name)
            res = _resolve_tool_resources(tool, tc.get("args", {}))
            resolved.append((i, tc, tool, res))

        # 按资源冲突分组
        groups = _build_conflict_groups(resolved)

        results: list[ToolResult] = []

        # 逐组执行：组间串行，组内并行
        for group in groups:
            if self._rolled_back:
                break

            if len(group) == 1:
                call_index, tc, tool, _res = group[0]
                if tool is None:
                    r = ToolResult(
                        id=f"{self.execution_id}_{call_index}",
                        tool_name=tc.get("name", "?"),
                        is_error=True,
                        error="TOOL_NOT_FOUND",
                        call_index=call_index,
                    )
                    results.append(r)
                else:
                    r = pipeline.execute(tc, tool, self.ctx, self.execution_id, call_index)
                    results.append(r)
                    # v0.45: CRASH → rollback entire Execution
                    if r.is_error and self._is_crash_error(r):
                        self.rollback(reason=f"Tool '{r.tool_name}' crashed: {r.error}")
                        break
            else:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(group), max_parallel)
                ) as executor:
                    futures = {}
                    for call_index, tc, tool, _res in group:
                        if tool is None:
                            results.append(ToolResult(
                                id=f"{self.execution_id}_{call_index}",
                                tool_name=tc.get("name", "?"),
                                is_error=True,
                                error="TOOL_NOT_FOUND",
                                call_index=call_index,
                            ))
                            continue
                        future = executor.submit(
                            pipeline.execute, tc, tool, self.ctx,
                            self.execution_id, call_index,
                        )
                        futures[future] = call_index

                    for future in concurrent.futures.as_completed(futures):
                        call_index = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = ToolResult(
                                id=f"{self.execution_id}_{call_index}",
                                is_error=True,
                                error=str(exc),
                                call_index=call_index,
                            )
                        results.append(result)
                        # v0.45: CRASH → rollback entire Execution
                        if result.is_error and self._is_crash_error(result):
                            self.rollback(
                                reason=f"Tool '{result.tool_name}' crashed: {result.error}"
                            )
                            # Cancel remaining futures
                            for f in futures:
                                f.cancel()
                            break

        # 按调用顺序排列
        results.sort(key=lambda r: r.call_index)
        self.results = results
        return results

    def _is_crash_error(self, result: "ToolResult") -> bool:
        """Check if a ToolResult represents a CRASH (not BUSINESS) error.

        CRASH errors: tool exception, timeout, system error → rollback.
        BUSINESS errors: test fail, lint error, build fail → do NOT rollback.
        """
        if not result.is_error:
            return False
        diag = result.diagnostics
        nature = diag.get("nature", "")
        if nature == "business":
            return False
        return True

    def commit(self) -> None:
        """事务成功。清理快照备份。"""
        self.status = "completed"
        self._cleanup_snapshot()
        self.ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ExecutionEvent"])
            .ExecutionEvent(
                "ExecutionCompleted",
                execution_id=self.execution_id,
                results=self.results,
            ),
        )

    def rollback(self, reason: str = "") -> None:
        """事务失败——恢复文件快照 + 裁剪会话 + 注入回滚通知。"""
        if self.snapshot:
            self._restore_snapshot(self.snapshot)
        self._trim_session()
        self._inject_rollback_notice(reason)
        self._rolled_back = True
        self.status = "failed"
        # Emit rollback_notification for Dashboard to grey out this turn
        self.ctx.event_bus.emit(
            __import__("backend.core.loop.event_bus", fromlist=["ExecutionEvent"])
            .ExecutionEvent(
                "ExecutionFailed",
                execution_id=self.execution_id,
                reason=reason,
            ),
        )
        # Emit streaming event so Dashboard can mark rendered content
        try:
            if hasattr(self.ctx, 'event_bus'):
                self.ctx.event_bus.emit_raw("rollback_notification", {
                    "execution_id": self.execution_id,
                    "reason": reason,
                })
        except Exception:
            pass

    def cancel(self) -> None:
        """取消事务（外部触发，如用户 Ctrl+C）。"""
        self.ctx.cancel()
        self.status = "cancelled"

    # ── Snapshot (v0.45: content-level SHA256 backup) ──────────

    def _take_snapshot(self) -> dict:
        """拍 workspace 内容级快照。

        对本批次所有 write 工具的目标文件做 SHA256 内容备份。
        复用 executor._extract_referenced_files 的文件路径提取逻辑。
        """
        ws = Path(self.ctx.workspace_path)
        if not ws.exists():
            return {"files": {}}

        # Extract target file paths from write tool calls
        target_files = self._extract_write_targets()

        files = {}
        for file_path_str in target_files:
            file_path = Path(file_path_str)
            if not file_path.is_absolute():
                file_path = ws / file_path
            if not file_path.exists():
                files[file_path_str] = {"action": "will_be_created"}
                continue

            try:
                content = file_path.read_bytes()
                sha = hashlib.sha256(content).hexdigest()
                backup_dir = Path(ws) / SNAPSHOT_DIR
                backup_dir.mkdir(parents=True, exist_ok=True)
                # Find next version number for this hash
                existing = list(backup_dir.glob(f"{sha}@v*"))
                version = len(existing) + 1
                backup_path = backup_dir / f"{sha}@v{version}"
                if not existing:
                    backup_path.write_bytes(content)
                files[file_path_str] = {
                    "action": "modified",
                    "original_sha": sha,
                    "backup_path": str(backup_path),
                }
            except OSError:
                files[file_path_str] = {"action": "modified", "backup_failed": True}

        # Prune old snapshots
        self._prune_old_snapshots(ws)

        return {"files": files}

    def _extract_write_targets(self) -> list[str]:
        """Extract file paths from write tool calls' args."""
        targets = []
        for tc in self.tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("args", {})
            # Only extract from write tools (not read tools)
            if not self._is_write_tool(tool_name):
                continue
            for key in ("file", "path", "files", "target", "source"):
                val = args.get(key)
                if isinstance(val, str) and val:
                    targets.append(val)
                elif isinstance(val, list):
                    targets.extend([v for v in val if isinstance(v, str)])
        return list(set(targets))  # dedup

    def _is_write_tool(self, tool_name: str) -> bool:
        """Heuristic: tools that modify filesystem state."""
        write_tools = {"formalize", "write", "edit", "delete", "push", "sync",
                        "assemble_context", "assemble_return_context"}
        return tool_name in write_tools

    def _restore_snapshot(self, snapshot: dict) -> None:
        """恢复 workspace 到快照状态。

        失败时记录 CRITICAL 日志，不二次回滚，追加系统告警到 session。
        """
        ws = Path(self.ctx.workspace_path)
        files = snapshot.get("files", {})

        for file_path_str, info in files.items():
            file_path = Path(file_path_str)
            if not file_path.is_absolute():
                file_path = ws / file_path

            action = info.get("action", "modified")

            try:
                if action == "will_be_created":
                    if file_path.exists():
                        file_path.unlink()
                elif action == "modified":
                    backup_path = info.get("backup_path", "")
                    if backup_path and Path(backup_path).exists():
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup_path, file_path)
                elif action == "deleted":
                    backup_path = info.get("backup_path", "")
                    if backup_path and Path(backup_path).exists():
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup_path, file_path)
            except OSError as e:
                # Rollback itself failed — log, notify, but don't retry
                try:
                    self.ctx.log(f"CRITICAL: rollback restore failed for {file_path_str}: {e}")
                except Exception:
                    pass
                if self.ctx.session:
                    self.ctx.session.append_user(
                        f"[系统告警] 回滚失败：无法恢复 {file_path_str}（{e}）。"
                        "文件系统状态可能不一致，请人工检查。"
                    )

    def _trim_session(self) -> None:
        """裁剪 session.messages：移除本次 Execution 的 assistant 消息。

        跳过 _nudge_state == "pending" 的治理 nudge 消息
        （受 Context Management 设计保护）。

        当前流程中 tool_result 在 execute_batch 返回后才追加到 session，
        因此此处只需移除触发工具调用的那一条 assistant 消息。
        如果未来工具结果在批次内逐条追加，则需扩展此方法。
        """
        session = self.ctx.session
        if not session or not session.messages:
            return

        # Remove the most recent assistant message (the one that triggered
        # this Execution's tool calls). It's the last assistant message.
        for i in range(len(session.messages) - 1, -1, -1):
            if session.messages[i].get("role") == "assistant":
                session.messages.pop(i)
                break

    def _inject_rollback_notice(self, reason: str) -> None:
        """注入回滚通知消息到 session。"""
        if self.ctx.session:
            self.ctx.session.append_user(
                f"[系统] 上一批次操作已回滚：{reason or '未知错误'}。请重新尝试。"
            )

    def _cleanup_snapshot(self) -> None:
        """提交成功后清理本 Execution 的快照备份。

        只删除本 Execution 创建的备份文件。
        SHA256 去重：同一内容被多个 Execution 共享时，不删除共享文件。
        """
        if not self.snapshot:
            return
        files = self.snapshot.get("files", {})
        for info in files.values():
            backup_path = info.get("backup_path", "")
            if backup_path:
                try:
                    p = Path(backup_path)
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass

    def _prune_old_snapshots(self, ws: Path) -> None:
        """Prune old snapshot backups exceeding MAX_SNAPSHOTS."""
        backup_dir = ws / SNAPSHOT_DIR
        if not backup_dir.exists():
            return
        backups = sorted(backup_dir.glob("*@v*"), key=lambda p: p.stat().st_mtime)
        if len(backups) > MAX_SNAPSHOTS:
            for old in backups[:len(backups) - MAX_SNAPSHOTS]:
                try:
                    old.unlink()
                except OSError:
                    pass


# ── v0.40: 资源冲突调度 ─────────────────────────────────

def _resolve_tool_resources(
    tool: "AgentTool | None",
    args: dict,
) -> set[str]:
    """解析工具调用的实际资源键。

    优先级：
    1. tool.resources 显式声明 → 直接使用
    2. tool.resources is None → fallback 到 read_only：
       - read_only=True → set()（无冲突，可任意并行）
       - read_only=False → {"*"}（全局互斥，与任何写工具冲突）

    "*" 是全局互斥通配符——与任何非空资源集冲突。
    """
    if tool is None:
        return {"*"}  # 未知工具：保守串行

    if tool.resources is not None:
        return set(tool.resources)

    # Fallback：read_only 二值 → 资源集
    if tool.read_only:
        return set()
    return {"*"}


def _build_conflict_groups(
    resolved: list[tuple[int, dict, "AgentTool | None", set[str]]],
) -> list[list[tuple[int, dict, "AgentTool | None", set[str]]]]:
    """将工具调用按资源冲突分组。

    分组算法：
    - 按顺序处理每个工具调用
    - 检查它能否放入最后一个已有组（与该组的合并资源无冲突）
    - 如果能：放入该组，更新该组的合并资源集
    - 如果不能：创建新组
    - 无资源声明（空集）的工具可以放入任意组
    - "XXX:*" 通配符与同命名空间内任何资源冲突 → 独占一组

    结果：组间串行，组内并行。
    """
    groups: list[list[tuple[int, dict, "AgentTool | None", set[str]]]] = []
    group_resources: list[set[str]] = []  # 每组的合并资源

    for entry in resolved:
        res = entry[3]
        placed = False

        # 从后往前找第一个不冲突的组（优先放入最近的组以减少碎片）
        for gi in range(len(groups) - 1, -1, -1):
            if not _resources_conflict(res, group_resources[gi]):
                groups[gi].append(entry)
                group_resources[gi] |= res
                placed = True
                break

        if not placed:
            groups.append([entry])
            group_resources.append(res)

    return groups


def _group_resources(
    group: list[tuple[int, dict, "AgentTool | None", set[str]]],
) -> set[str]:
    """计算一个组的合并资源集。"""
    merged: set[str] = set()
    for _, _, _, res in group:
        merged |= res
    return merged


def _resources_conflict(a: set[str], b: set[str]) -> bool:
    """判断两个资源集是否冲突。

    冲突条件（任一满足即冲突）：
    - 交集非空 → 冲突
    - 一方含全局通配符 "XXX:*" 且另一方在同一命名空间有资源 → 冲突
    - 双方都含 "XXX:*" 在同一命名空间 → 冲突
    """
    if not a or not b:
        return False  # 空集：无冲突，可任意并行

    # 精确交集
    if a & b:
        return True

    # 通配符检测：如 "filesystem:*" 匹配 "filesystem:fileA"
    a_wildcards = {r.split(":")[0] for r in a if r.endswith(":*")}
    b_wildcards = {r.split(":")[0] for r in b if r.endswith(":*")}

    for r in a:
        ns = r.split(":")[0] if ":" in r else ""
        if ns and f"{ns}:*" in b:
            return True

    for r in b:
        ns = r.split(":")[0] if ":" in r else ""
        if ns and f"{ns}:*" in a:
            return True

    return False
