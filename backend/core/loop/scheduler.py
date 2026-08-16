"""SlotScheduler —— 多 Agent 编排层统一入口。

独立于 Backend：Backend 只回答 How，Scheduler 决定 Where / When / In What Order。

完整链路：
1. Partition  → 双层决策 → 产出 TaskSpec[] 列表
2. Build DAG  → 分析 input/output interfaces → depends_on 边
3. Assemble Context → 为每个 TaskSpec 组装 ContextSnapshot（L1 强制注入 + L2 契约）
4. Execute   → 按 DAG 顺序逐批提交 Backend
5. Collect   → 收集结果 + Escalation 处理 + verify_contract
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.loop.task_slot import (
        TaskSpec, TaskSlot, ContextSnapshot, Capability, SlotBackend,
    )
    from backend.core.loop.decomposition import DecompositionGuard


class SlotScheduler:
    """多 Agent 编排器。

    daemon 调用 scheduler.run(task, process, session, workspace_path)
    → Scheduler 决定是否分区 → 创建 TaskSlot → 执行 → 收集结果。
    """

    def __init__(self, default_backend: "SlotBackend | None" = None):
        self._default_backend = default_backend
        from backend.core.loop.decomposition import DecompositionGuard
        self._guard = DecompositionGuard()

    # ── 统一入口 ──────────────────────────────────────

    def run(
        self,
        task_description: str,
        target_files: list[str],
        process,             # AgentProcess
        session,             # AgentSession
        workspace_path: str,
        llm_provider=None,
        dep_graph: dict | None = None,
        context_compact_count: int = 0,
    ) -> dict:
        """统一入口：分区 → DAG → 上下文 → 执行 → 收集。"""
        from backend.core.loop.task_slot import (
            TaskSpec, ContextSnapshot, Capability, TaskSlot, TaskRuntime,
        )
        from backend.core.loop.decomposition import should_decompose
        from backend.core.loop.interface_contract import freeze_contract, verify_contract

        # 1. Partition
        decision = should_decompose(
            process, session, target_files,
            nudge_counters=getattr(process, '_nudge_counters', {}),
            context_compact_count=context_compact_count,
        )

        if not decision.required and not decision.suggested:
            # 不需要分区——创建单个 TaskSlot 执行
            spec = TaskSpec(
                spec_id="root",
                task_description=task_description,
                expected_output="TASK_COMPLETE",
            )
            slots = [self._create_slot(
                spec=spec,
                parent_slot_id=None,
                depends_on=[],
                depth=1,
                target_files=target_files,
                dep_graph=dep_graph,
                workspace_path=workspace_path,
                backend=self._default_backend,
            )]
        else:
            # 需要分区——按 LLM 建议或系统规则拆成多个 TaskSpec
            # Phase 1: 使用 decision.suggestions。如果 LLM 未给出有效建议，
            # 则按文件边界简单拆分（每个文件一个 slot，按依赖图排序）。
            specs = self._partition_by_files(
                task_description=task_description,
                target_files=target_files,
                dep_graph=dep_graph,
            )
            slots = [
                self._create_slot(
                    spec=spec,
                    parent_slot_id="root",
                    depends_on=self._resolve_depends(spec, specs, dep_graph),
                    depth=1,
                    target_files=spec.target_files if hasattr(spec, 'target_files') else [],
                    dep_graph=dep_graph,
                    workspace_path=workspace_path,
                    backend=self._default_backend,
                )
                for spec in specs
            ]

        # 2. Build DAG（已在 _resolve_depends 中完成）

        # 3. Assemble Context（L1 + L2）
        self._assemble_contexts(slots, dep_graph)

        # 4. Execute
        self._execute_slots(slots, workspace_path, llm_provider)

        # 5. Collect
        return self._collect_results(slots, workspace_path, dep_graph)

    # ── 内部方法 ──────────────────────────────────────

    def _create_slot(
        self,
        spec: "TaskSpec",
        parent_slot_id: str | None,
        depends_on: list[str],
        depth: int,
        target_files: list[str],
        dep_graph: dict | None,
        workspace_path: str,
        backend: "SlotBackend | None",
    ) -> "TaskSlot":
        from backend.core.loop.task_slot import (
            ContextSnapshot, Capability, TaskRuntime, TaskSlot,
        )

        # 初始 ContextSnapshot（L1 强制注入在 _assemble_contexts 中完成）
        context = ContextSnapshot(
            status={"target_files": target_files},
        )

        # 构建 Capability
        capacity = Capability(
            tool_allowlist=["scan", "status", "recall_grep",
                           "recall_semantic", "recall_rag",
                           "assemble_context", "assemble_return_context"],
            max_steps=min(spec.estimated_steps if hasattr(spec, 'estimated_steps') else 20, 50),
        )

        runtime = TaskRuntime(backend=backend)

        import uuid
        return TaskSlot(
            slot_id=str(uuid.uuid4())[:8],
            parent_slot_id=parent_slot_id,
            depends_on=depends_on,
            depth=depth,
            spec=spec,
            context=context,
            capability=capacity,
            runtime=runtime,
        )

    def _partition_by_files(
        self,
        task_description: str,
        target_files: list[str],
        dep_graph: dict | None,
    ) -> list["TaskSpec"]:
        """简单文件边界拆分——每个文件一个 slot。

        Phase 1 简化实现。未来增强：按依赖图分组（关联文件打包到一个 slot）。
        """
        from backend.core.loop.task_slot import TaskSpec
        specs = []
        for i, f in enumerate(target_files):
            specs.append(TaskSpec(
                spec_id=f"split-{i}",
                task_description=f"{task_description}——仅处理 {f}",
                expected_output="TASK_COMPLETE",
            ))
        return specs

    def _resolve_depends(
        self,
        spec: "TaskSpec",
        all_specs: list["TaskSpec"],
        dep_graph: dict | None,
    ) -> list[str]:
        """根据 dep_graph 解析 spec 的上游依赖。

        如果 dep_graph 显示 spec 涉及的文件 import 了另一个 spec 涉及的文件的符号，
        则另一个 spec 是上游依赖。
        """
        if not dep_graph:
            return []
        return []  # Phase 1 简化：不自动解析跨文件依赖

    def _assemble_contexts(
        self,
        slots: list["TaskSlot"],
        dep_graph: dict | None,
    ) -> None:
        """为每个 slot 组装 ContextSnapshot（L1 + L2）。"""
        from backend.core.loop.interface_contract import freeze_contract

        # L2: 接口契约——如果有交叉依赖
        contract = freeze_contract(slots, dep_graph or {})
        slot_map = {s.slot_id: s for s in slots}

        for slot in slots:
            # L1: 强制注入上游产出
            for upstream_id in slot.depends_on:
                upstream = slot_map.get(upstream_id)
                if upstream and upstream.is_complete:
                    slot.context.needed.append({
                        "source": "upstream_slot",
                        "rule": (
                            f"上游 {upstream_id} 已完成。"
                            f"产出接口: {upstream.spec.output_interfaces}"
                        ),
                        "output_files": (
                            upstream.runtime.result.get("files", [])
                            if upstream.runtime.result else []
                        ),
                    })

            # L2: 注入冻结契约
            if contract:
                slot.context.needed.append({
                    "source": "interface_contract",
                    "rule": (
                        f"接口契约 {contract.contract_id} 已冻结。"
                        f"共 {len(contract.interfaces)} 个接口。"
                    ),
                    "contract_id": contract.contract_id,
                    "interfaces": [
                        {"file": i.file, "symbol": i.symbol, "signature": i.signature}
                        for i in contract.interfaces
                    ],
                })

    def _execute_slots(
        self,
        slots: list["TaskSlot"],
        workspace_path: str,
        llm_provider=None,
    ) -> None:
        """按 DAG 顺序逐批执行 slot。

        批次划分：一批 = 所有上游已完成且自身未完成的 slot。
        """
        completed: set[str] = set()
        remaining = list(slots)

        while remaining:
            # 找出当前可执行的 slot（所有上游已完成）
            ready = [
                s for s in remaining
                if all(dep in completed for dep in s.depends_on)
            ]

            if not ready:
                # 死锁或无进展——剩余 slot 的上游未在 slots 中声明
                for s in remaining:
                    s.runtime.status = "FAILED"
                    s.runtime.error = "dependency not resolved"
                break

            for slot in ready:
                if slot.runtime.backend is None:
                    from backend.core.loop.task_slot import ForkBackend
                    backend = ForkBackend()
                    if llm_provider:
                        backend._llm_provider = llm_provider
                    slot.runtime.backend = backend

                slot.runtime.backend.run(slot, workspace_path)
                completed.add(slot.slot_id)

            remaining = [s for s in remaining if s.slot_id not in completed]

    def _collect_results(
        self,
        slots: list["TaskSlot"],
        workspace_path: str,
        dep_graph: dict | None,
    ) -> dict:
        """收集所有 slot 的结果，处理 Escalation。"""
        from backend.core.loop.interface_contract import verify_contract, freeze_contract

        results = []
        escalated = []
        contract = freeze_contract(slots, dep_graph or {})

        for slot in slots:
            results.append({
                "slot_id": slot.slot_id,
                "status": slot.runtime.status,
                "result": slot.runtime.result,
            })

            if slot.is_escalated:
                escalated.append(slot)

        # 如果有 escalation → 默认策略：中止全部，合并上下文重跑
        if escalated:
            return {
                "status": "ESCALATED",
                "escalated_slots": [
                    {"slot_id": s.slot_id, "reason": (
                        s.runtime.result.get("escalate_reason", "")
                        if s.runtime.result else ""
                    )}
                    for s in escalated
                ],
                "all_results": results,
                "recovery_action": "abort_and_rerun_single_context",
            }

        # verify_contract
        violations = verify_contract(contract, workspace_path) if contract else []
        if violations:
            return {
                "status": "CONTRACT_VIOLATION",
                "violations": [
                    {"file": v.file, "symbol": v.interface.symbol,
                     "type": v.violation_type,
                     "expected": v.expected_signature,
                     "actual": v.actual_signature}
                    for v in violations
                ],
                "all_results": results,
                "recovery_action": "abort_and_rerun_single_context",
            }

        return {
            "status": "COMPLETED",
            "results": results,
            "total_slots": len(slots),
            "completed_slots": sum(
                1 for s in slots if s.runtime.status == "COMPLETED"
            ),
        }
