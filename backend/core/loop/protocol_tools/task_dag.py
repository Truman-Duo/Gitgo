"""task dag protocol tools."""
from __future__ import annotations
from backend.core.loop.execution_contract import HOST_COMPUTE, data_broker
from pathlib import Path
from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.loop.capabilities import CapabilityProfiles

def install(ctx):
    process = ctx.process
    workspace_path = ctx.workspace_path
    on_stream_event = ctx.on_stream_event
    tools = ctx.tools
    manager = ctx.manager
    delegate_task = ctx.delegate_task
    def delegate_task_dag(args: dict) -> dict:
        """Validate local node ids, then admit one durable sibling DAG."""
        nodes = [dict(item or {}) for item in (args.get("nodes") or [])]
        if not 1 <= len(nodes) <= 8:
            return {"delegated": False, "error": "nodes must contain 1..8 tasks"}
        node_ids = [str(item.get("node_id") or "").strip() for item in nodes]
        if any(not item for item in node_ids) or len(set(node_ids)) != len(node_ids):
            return {"delegated": False, "error": "node_id values must be unique"}
        by_id = dict(zip(node_ids, nodes))
        from backend.core.loop.interface_contract import (
            capture_declared_contract,
            normalise_interface_ref,
        )
        producers: dict[str, str] = {}
        try:
            for node_id, node in by_id.items():
                node["input_interfaces"] = list(dict.fromkeys(
                    normalise_interface_ref(item)
                    for item in (node.get("input_interfaces") or [])
                ))
                node["output_interfaces"] = list(dict.fromkeys(
                    normalise_interface_ref(item)
                    for item in (node.get("output_interfaces") or [])
                ))
                for ref in node["output_interfaces"]:
                    previous = producers.get(ref)
                    if previous and previous != node_id:
                        return {
                            "delegated": False,
                            "error": f"interface {ref} has multiple owners",
                        }
                    producers[ref] = node_id
            # Interface declarations are control-plane data.  Derive the
            # happens-before edge mechanically instead of asking A to copy
            # the same relation into depends_on and risking disagreement.
            for node_id, node in by_id.items():
                dependencies = list(node.get("depends_on") or [])
                for ref in node["input_interfaces"]:
                    owner = producers.get(ref)
                    if not owner:
                        return {
                            "delegated": False,
                            "error": f"input interface has no DAG owner: {ref}",
                        }
                    if owner == node_id:
                        return {
                            "delegated": False,
                            "error": f"node {node_id} consumes its own interface: {ref}",
                        }
                    dependencies.append(owner)
                node["depends_on"] = list(dict.fromkeys(
                    str(item) for item in dependencies if str(item)
                ))
        except ValueError as exc:
            return {"delegated": False, "error": str(exc)}
        indegree = {node_id: 0 for node_id in node_ids}
        downstream: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        for node_id, node in by_id.items():
            dependencies = list(dict.fromkeys(
                str(item) for item in (node.get("depends_on") or []) if str(item)
            ))
            if node_id in dependencies:
                return {"delegated": False, "error": f"self dependency: {node_id}"}
            unknown = sorted(set(dependencies) - set(node_ids))
            if unknown:
                return {
                    "delegated": False,
                    "error": f"unknown dependencies for {node_id}: {', '.join(unknown)}",
                }
            node["depends_on"] = dependencies
            indegree[node_id] = len(dependencies)
            for upstream in dependencies:
                downstream[upstream].append(node_id)
        ready = sorted(item for item, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while ready:
            node_id = ready.pop(0)
            order.append(node_id)
            for target in sorted(downstream[node_id]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
                    ready.sort()
        if len(order) != len(nodes):
            return {"delegated": False, "error": "DAG contains a cycle"}
        # Preflight every deterministic contract property before the first
        # child is admitted.  This keeps malformed DAG calls atomic: a bad
        # later node cannot leave earlier workers running or consume task
        # budget merely because validation happened in topological order.
        workspace_root = Path(workspace_path or process.worktree_path).resolve()
        for node_id in order:
            node = by_id[node_id]
            description = str(node.get("task_description") or "").strip()
            if not description:
                return {
                    "delegated": False,
                    "error": f"node {node_id}: task_description is required",
                }
            profile_id = str(
                node.get("capability_profile_id") or "development.workspace"
            )
            profile_error = CapabilityProfiles.worker_error(profile_id)
            if profile_error:
                return {"delegated": False, "node_id": node_id, **profile_error}
            profile = CapabilityProfiles.get(profile_id)
            task_kind = str(node.get("task_kind") or "").strip() or (
                "action" if profile.allows_self_execution else "answer"
            )
            if task_kind not in {"answer", "plan", "action"}:
                return {
                    "delegated": False,
                    "error": f"node {node_id}: invalid task_kind {task_kind}",
                }
            if task_kind == "action" and not profile.allows_self_execution:
                return {
                    "delegated": False,
                    "error": (
                        f"node {node_id}: profile {profile_id} cannot execute actions"
                    ),
                }
            profile_tools = set(CapabilityProfiles.resolve_tools(profile_id))
            requested_tools = {
                str(item).strip() for item in (node.get("allowed_tools") or [])
                if str(item).strip()
            }
            unknown_tools = sorted(requested_tools - profile_tools)
            if unknown_tools:
                return {
                    "delegated": False,
                    "error": (
                        f"node {node_id}: tools exceed profile: "
                        + ", ".join(unknown_tools)
                    ),
                }
            scope_mode = str(node.get("tool_scope_mode") or "profile").lower()
            required_tests = [
                str(item) for item in (node.get("required_test_ids") or [])
                if str(item).strip()
            ]
            if scope_mode not in {"profile", "test_only"}:
                return {
                    "delegated": False,
                    "error": f"node {node_id}: invalid tool_scope_mode",
                }
            if scope_mode == "test_only" and (
                profile_id != "development.workspace" or not required_tests
            ):
                return {
                    "delegated": False,
                    "error": (
                        f"node {node_id}: test_only needs development.workspace "
                        "and required_test_ids"
                    ),
                }
            try:
                max_steps = int(node.get("max_steps", 50) or 50)
            except (TypeError, ValueError):
                return {
                    "delegated": False,
                    "error": f"node {node_id}: max_steps must be an integer",
                }
            if max_steps < 1:
                return {
                    "delegated": False,
                    "error": f"node {node_id}: max_steps must be positive",
                }
            for raw_target in (node.get("target_files") or []):
                value = str(raw_target).strip().replace("\\", "/")
                while value.startswith("./"):
                    value = value[2:]
                if value == "*":
                    continue
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = workspace_root / candidate
                try:
                    candidate.resolve(strict=False).relative_to(workspace_root)
                except ValueError:
                    return {
                        "delegated": False,
                        "error": f"node {node_id}: target escapes workspace",
                    }
        try:
            interface_contract = capture_declared_contract(
                workspace_root, [by_id[item] for item in order],
            )
        except ValueError as exc:
            return {"delegated": False, "error": str(exc)}
        for node in nodes:
            node["_interface_contract"] = interface_contract
        if process.task_budget is not None:
            snapshot = process.task_budget.snapshot()
            remaining = (
                int(snapshot["limits"]["max_agents"])
                - int(snapshot["used"]["agents"])
            )
            if len(nodes) > remaining:
                return {
                    "delegated": False,
                    "error": f"DAG needs {len(nodes)} agents but budget has {remaining}",
                }
        admitted: dict[str, str] = {}
        created: list[str] = []
        for node_id in order:
            node = by_id[node_id]
            result = delegate_task({
                **node,
                "depends_on": [admitted[item] for item in node["depends_on"]],
            })
            if result.get("delegated") is False or result.get("error"):
                for process_id in created:
                    manager.kill(process_id, reason="dag_admission_rollback")
                    contract = process.delegated_contracts.get(process_id)
                    if contract is not None:
                        contract["required_for_parent_completion"] = False
                        contract["admission_rolled_back"] = True
                return {
                    "delegated": False,
                    "error": f"node {node_id} admission failed: {result.get('error')}",
                    "rolled_back_process_ids": created,
                }
            admitted[node_id] = str(result["process_id"])
            created.append(str(result["process_id"]))
        if on_stream_event is not None:
            on_stream_event({
                "event": "agent_dag_admitted",
                "process_id": process.process_id,
                "task_id": process.active_task_id,
                "nodes": admitted,
                "order": order,
            })
        return {
            "delegated": True,
            "nodes": admitted,
            "topological_order": order,
        }


    tools["delegate_task_dag"] = AgentTool(
        execution_contract=data_broker("host.runtime.delegate_task_dag"),
        name="delegate_task_dag",
        description=(
            "Admit a complete sibling DAG in one Host-validated operation. Use "
            "local node_id/depends_on values; the Host resolves process ids, "
            "schedules ready nodes and injects upstream outcomes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string"},
                            "task_description": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            "capability_profile_id": {
                                "type": "string",
                                "enum": list(CapabilityProfiles.ids(actor_kind="worker")),
                                "default": "development.workspace",
                            },
                            "task_kind": {
                                "type": "string",
                                "enum": ["answer", "plan", "action"],
                            },
                            "target_files": {"type": "array", "items": {"type": "string"}},
                            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                            "required_test_ids": {"type": "array", "items": {"type": "string"}},
                            "required_tool_calls": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "tool_name": {"type": "string"},
                                        "min_calls": {"type": "integer"},
                                        "max_calls": {"type": "integer"},
                                        "include_composite_steps": {"type": "boolean"},
                                    },
                                    "required": ["tool_name"],
                                },
                            },
                            "input_interfaces": {"type": "array", "items": {"type": "string"}},
                            "output_interfaces": {"type": "array", "items": {"type": "string"}},
                            "allowed_tools": {"type": "array", "items": {"type": "string"}},
                            "max_steps": {"type": "integer"},
                            "required_for_parent_completion": {"type": "boolean"},
                        },
                        "required": [
                            "node_id", "task_description", "depends_on",
                            "target_files", "acceptance_criteria",
                        ],
                    },
                },
            },
            "required": ["nodes"],
        },
        execute=delegate_task_dag,
        read_only=True,
        effect=ToolEffect.READ,
    )
