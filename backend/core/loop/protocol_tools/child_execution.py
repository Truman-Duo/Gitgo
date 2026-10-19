"""child execution protocol tools."""
from __future__ import annotations
from pathlib import Path
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.lifecycle import transition

def _run_agent_step(*args, **kwargs):
    from backend.core.loop.executor import agent_step
    return agent_step(*args, **kwargs)

def install(ctx):
    process = ctx.process
    workspace_path = ctx.workspace_path
    llm_provider = ctx.llm_provider
    dispatcher = ctx.dispatcher
    on_stream_event = ctx.on_stream_event
    manager = ctx.manager
    _owned_child = ctx.owned_child
    _spawn_child = ctx.spawn_child
    def _spawn_child(
        *,
        task_description: str,
        profile_id: str,
        task_kind: str,
        max_steps: int,
        target_files: list[str],
        acceptance_criteria: list[str],
        allowed_tools: list[str] | None = None,
        tool_scope_mode: str = "profile",
        required_test_ids: list[str] | None = None,
        required_tool_calls: list[dict] | None = None,
        depends_on: list[str] | None = None,
        input_interfaces: list[str] | None = None,
        output_interfaces: list[str] | None = None,
        interface_contract: dict | None = None,
        required_for_parent_completion: bool = True,
        supersedes_process_id: str = "",
        continue_process_id: str = "",
        display_name: str = "",
        actor_kind: str = "worker",
        review_target_id: str = "",
        budget_request: dict | None = None,
    ):
        if manager is None:
            raise RuntimeError("AgentProcessManager is unavailable")
        if llm_provider is None or dispatcher is None:
            raise RuntimeError("delegate runtime is missing provider or tool dispatcher")
        dependency_ids = list(dict.fromkeys(
            str(item) for item in (depends_on or []) if str(item)
        ))
        for upstream_id in dependency_ids:
            if _owned_child(upstream_id) is None:
                raise PermissionError(
                    "depends_on must reference an owned sibling task"
                )
        profile = CapabilityProfiles.get(profile_id)
        if profile.actor_kind.value != actor_kind:
            raise PermissionError(
                f"profile {profile_id} is not valid for {actor_kind}"
            )
        previous_contract = None
        continuation = None
        resume_partial = False
        if continue_process_id:
            from backend.core.loop.task_contract import get_task_contract
            requirements = get_task_contract(process).get("host_requirements") or {}
            if continue_process_id in requirements.get("excluded_process_ids", []):
                raise ValueError("This manual create request requires a new B session; omit continue_process_id")
            continuation = _owned_child(continue_process_id)
            if continuation is None or continuation.actor_kind != actor_kind:
                raise ValueError("continue_process_id must identify an owned agent of the same kind; inspect list_agents")
            if supersedes_process_id and supersedes_process_id != continue_process_id:
                raise ValueError("continuation must supersede the same previous execution")
            previous_contract = process.delegated_contracts.get(continue_process_id) or {}
            if previous_contract.get("superseded_by"):
                raise ValueError("CONTINUATION_ALREADY_SUPERSEDED: continue the latest execution of this session")
            resume_partial = bool(
                continuation.worktree.get("isolated") and not continuation.worktree.get("promoted")
                and continuation.worktree.get("state") == "leased"
                and continuation.status in {ProcessStatus.FAILED, ProcessStatus.TIMED_OUT, ProcessStatus.CANCELLED}
                and actor_kind == "worker" and task_kind == "action"
                and profile_id == continuation.capability_profile_id == "development.workspace"
            )
            if (continuation.worktree.get("path") and continuation.worktree.get("mode") == "write"
                    and continuation.worktree.get("state") != "shared"
                    and not continuation.worktree.get("promoted") and not resume_partial):
                raise ValueError("CONTINUATION_WORKTREE_UNPUBLISHED: review and promote the previous result, or explicitly choose a fresh task; unmerged edits will not be silently discarded")
            if resume_partial:
                if (continuation.worktree.get("snapshot_commit") != process.worktree.get("snapshot_commit")
                        or not Path(continuation.worktree["path"]).is_dir()):
                    raise ValueError("CONTINUATION_BASE_MISMATCH: retained checkout and original supervisor snapshot must still be available")
                if not dependency_ids:
                    dependency_ids = list(continuation.depends_on)
                if dependency_ids != continuation.depends_on:
                    raise ValueError("CONTINUATION_BASE_MISMATCH: retained work requires the original dependencies")
            elif continuation.worktree.get("isolated") and process.worktree.get("snapshot_commit"):
                raise ValueError("CONTINUATION_SNAPSHOT_PINNED: this DAG still pins its original snapshot; start a new supervisor turn after promotion to continue from the updated workspace")
            supersedes_process_id = continue_process_id
        if supersedes_process_id:
            previous = _owned_child(supersedes_process_id)
            previous_contract = process.delegated_contracts.get(supersedes_process_id)
            if previous is None or previous_contract is None:
                raise ValueError("supersedes_process_id is not an owned delegated task")
            if previous.status in {
                ProcessStatus.RUNNING, ProcessStatus.WAITING,
                ProcessStatus.CANCELLING, ProcessStatus.AWAITING_USER,
            }:
                raise ValueError("cannot supersede a non-terminal delegated task")
        normalised_targets = []
        workspace_root = Path(workspace_path or process.worktree_path).resolve()
        for item in target_files:
            value = str(item).strip().replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if value == "*":
                normalised_targets.append("*")
                continue
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            try:
                value = str(
                    candidate.resolve(strict=False).relative_to(workspace_root)
                ).replace("\\", "/")
            except ValueError as exc:
                raise PermissionError(
                    f"target file escapes workspace: {item}"
                ) from exc
            if value and value not in normalised_targets:
                normalised_targets.append(value)
        if resume_partial:
            old_targets = {str(item).replace("\\", "/").removeprefix("./")
                           for item in previous_contract.get("target_files", [])}
            if "*" not in normalised_targets and (not old_targets or not old_targets.issubset(normalised_targets)):
                raise ValueError("CONTINUATION_SCOPE_MISMATCH: include the retained task's target files or choose a fresh task")
        claims = [f"filesystem:{item}" for item in normalised_targets]
        if not claims and profile_id == "development.workspace":
            claims = ["filesystem:*"]
        if not claims and actor_kind == "reviewer":
            # A review needs a stable workspace snapshot.  The exclusive
            # wildcard waits for all active writers and blocks new ones.
            claims = ["filesystem:*"]
        profile_tools = CapabilityProfiles.resolve_tools(profile_id)
        requested_tools = [
            str(item).strip() for item in (allowed_tools or [])
            if str(item).strip()
        ]
        unknown_tools = sorted(set(requested_tools) - set(profile_tools))
        if unknown_tools:
            raise PermissionError(
                "task-scoped tools exceed capability profile: "
                + ", ".join(unknown_tools)
            )
        tool_scope_mode = str(tool_scope_mode or "profile").strip().lower()
        if tool_scope_mode not in {"profile", "test_only"}:
            raise PermissionError("tool_scope_mode must be profile or test_only")
        if requested_tools:
            effective_tools = list(dict.fromkeys(requested_tools))
            tool_scope_source = "supervisor_allowlist"
        elif tool_scope_mode == "test_only":
            if not required_test_ids or profile_id != "development.workspace":
                raise PermissionError(
                    "test_only scope requires a development.workspace contract "
                    "with required_test_ids"
                )
            # A registered-test contract is mechanically executable.  Do
            # not give the worker broad inspection/edit tools that invite
            # preliminary scans or unrelated workspace changes.
            effective_tools = ["run_test"]
            tool_scope_source = "host_registered_test_contract"
        elif normalised_targets and profile_id == "governance.observe":
            # Exact target files make full-workspace scan/status/list calls
            # unnecessary.  Retain the bounded read and governance queries.
            bounded_observe = {
                "read_file", "search_text", "artifact_read", "dependency_query",
                "context_open", "context_search", "contract_detect_drift",
                "contract_get_impact", "contract_get_changed_symbols",
                "lesson_search", "lesson_list", "workbench", "decision_evidence",
            }
            effective_tools = [
                name for name in profile_tools if name in bounded_observe
            ]
            tool_scope_source = "host_bounded_target_contract"
        else:
            effective_tools = profile_tools
            tool_scope_source = "capability_profile"
        if required_test_ids and "run_test" not in effective_tools:
            raise PermissionError(
                "a required-test contract must authorize run_test"
            )
        normalized_required_calls: list[dict] = []
        for raw_requirement in list(required_tool_calls or []):
            requirement = dict(raw_requirement or {})
            name = str(requirement.get("tool_name") or "").strip()
            if not name:
                raise ValueError("required_tool_calls.tool_name is required")
            minimum = int(requirement.get("min_calls", 1) or 0)
            maximum_raw = requirement.get("max_calls")
            maximum = (
                int(maximum_raw) if maximum_raw is not None else None
            )
            if minimum < 0 or minimum > 100:
                raise ValueError("required_tool_calls.min_calls must be between 0 and 100")
            if maximum is not None and (maximum < minimum or maximum > 100):
                raise ValueError(
                    "required_tool_calls.max_calls must be between min_calls and 100"
                )
            # A dynamic name is legal when this task can define tools.  The
            # Host validates its actual existence through execution receipts.
            if name not in effective_tools and "define_tool" not in effective_tools:
                raise PermissionError(
                    f"required tool is not authorized by this task: {name}"
                )
            normalized_required_calls.append({
                "tool_name": name,
                "min_calls": minimum,
                **({"max_calls": maximum} if maximum is not None else {}),
                "include_composite_steps": bool(
                    requirement.get("include_composite_steps", False)
                ),
            })
        if "run_test" in effective_tools:
            for claim in (
                "process:pytest", "filesystem:.gitgo/test_manifest.json",
            ):
                if claim not in claims:
                    claims.append(claim)
        parent_context, _parent_context_version = process.read_context_snapshot()
        from backend.core.loop.context_policy import context_admission_policy
        from backend.core.loop.coordination import relationship_policy
        relation = relationship_policy(
            owner_process_id=process.process_id,
            depends_on=dependency_ids,
            continuation_process_id=continue_process_id,
            required_for_parent_completion=required_for_parent_completion,
            capability_profile_id=profile_id,
            task_kind=task_kind,
        )
        child_context = {
            "signals": list(parent_context.get("signals", []) or []),
            "evidence_candidates": list(parent_context.get("evidence_candidates", []) or []),
            "brief": str(parent_context.get("brief", "")),
            "project_name": str(parent_context.get("project_name", "")),
            "workspace_path": str(parent_context.get("workspace_path", "")),
            "context_refs": dict(parent_context.get("context_refs", {}) or {}),
            "context_policy": context_admission_policy(actor_kind, task_kind),
            "relationship_policy": relation,
            "task_contract": {
                "acceptance_criteria": acceptance_criteria,
                "target_files": target_files,
                "parent_process_id": process.process_id,
                "required_test_ids": list(required_test_ids or []),
                "required_tool_calls": normalized_required_calls,
                "allowed_tools": list(effective_tools),
                "tool_scope_source": tool_scope_source,
                "tool_scope_mode": tool_scope_mode,
                "depends_on": dependency_ids,
                "input_interfaces": list(input_interfaces or []),
                "output_interfaces": list(output_interfaces or []),
                "interface_contract": dict(interface_contract or {}),
                "relationship_policy": relation,
            },
        }
        child = manager.fork(
            parent_id=process.process_id,
            role="reviewer" if actor_kind == "reviewer" else "executor",
            tool_registry=__import__(
                "backend.core.loop.tools", fromlist=["ToolRegistry"]
            ).ToolRegistry(effective_tools),
            max_steps=max(1, min(max_steps, 200)),
            ring_level=RingLevel.RING_3,
            context_snapshot=child_context,
            task_description=task_description,
            task_id=process.allocate_child_task_id(),
            workspace_path=workspace_path or process.worktree_path,
            provider_id=process.provider_id,
            model_id=process.model_id,
            actor_kind=actor_kind,
            capability_profile_id=profile_id,
            task_kind=task_kind,
            required_test_ids=list(required_test_ids or []),
            depends_on=dependency_ids,
            child_budget_request=dict(budget_request or {}),
            session=continuation.session if continuation is not None else None,
        )
        if display_name:
            # A may name its worker; control characters cannot reach the TUI.
            name = " ".join("".join(c for c in display_name if c.isprintable()).split())[:48]
            if name:
                child.session.display_name = name
        child.review_target_id = review_target_id
        contract = {
            "task_kind": task_kind,
            "acceptance_criteria": list(acceptance_criteria),
            "target_files": list(target_files),
            "required_test_ids": list(required_test_ids or []),
            "required_tool_calls": normalized_required_calls,
            "allowed_tools": list(effective_tools),
            "tool_scope_source": tool_scope_source,
            "tool_scope_mode": tool_scope_mode,
            "required_for_parent_completion": bool(required_for_parent_completion),
            "supersedes_process_id": supersedes_process_id,
            "depends_on": dependency_ids,
            "input_interfaces": list(input_interfaces or []),
            "output_interfaces": list(output_interfaces or []),
            "interface_contract": dict(interface_contract or {}),
            "relationship_policy": relation,
            "budget_lease": dict(child.budget_lease or {}),
        }
        process.register_child_contract(
            child.process_id, contract,
            supersedes_process_id=supersedes_process_id,
        )

        def emit_child_event(event: dict) -> None:
            if on_stream_event is None:
                return
            enriched = dict(event)
            enriched.setdefault("process_id", child.process_id)
            enriched["parent_process_id"] = process.process_id
            enriched["root_process_id"] = (
                process.process_id if process.parent_id is None else process.parent_id
            )
            enriched["task_id"] = child.active_task_id
            enriched["actor_kind"] = child.actor_kind
            on_stream_event(enriched)

        def run_child():
            execution_workspace = workspace_path or process.worktree_path
            lifecycle_stage = "start"
            emit_child_event({
                "event": "agent_started",
                "status": child.status.value,
                "capability_profile_id": child.capability_profile_id,
            })
            result = None
            try:
                lifecycle_stage = "prepare_worktree"
                review_target = manager.get(review_target_id) if review_target_id else None
                review_has_snapshot = bool(
                    review_target
                    and (review_target.worktree or {}).get("result_commit")
                )
                isolated_code_action = (
                    task_kind == "action"
                    and profile_id == "development.workspace"
                )
                if manager.worktree_manager is not None and (
                    isolated_code_action
                    or (actor_kind == "reviewer" and review_has_snapshot)
                ):
                    execution_workspace = manager.materialize_process_worktree(
                        child, review_target_id=review_target_id,
                        continue_process_id=continue_process_id if resume_partial else "",
                    )
                    emit_child_event({
                        "event": "worktree_leased",
                        "worktree": dict(child.worktree),
                    })
                from backend.core.loop.interface_contract import (
                    verify_declared_contract,
                )
                lifecycle_stage = "verify_input_interfaces"
                boundary_context, _ = child.read_context_snapshot()
                boundary_contract = dict(
                    dict(boundary_context.get("task_contract") or {}).get("interface_contract")
                    or interface_contract or {}
                )
                input_violations = verify_declared_contract(
                    boundary_contract,
                    execution_workspace,
                    list(input_interfaces or []),
                )
                if input_violations:
                    transition(child, ProcessStatus.FAILED)
                    child.result = {
                        "status": "failed",
                        "process_id": child.process_id,
                        "code": "INTERFACE_CONTRACT_VIOLATION",
                        "error": "upstream interface contract was not satisfied",
                        "violations": input_violations,
                    }
                    result = child.result
                    return result
                lifecycle_stage = "assemble_upstream_context"
                child_context_now, _ = child.read_context_snapshot()
                upstream_context = []
                for upstream in manager.dependency_closure(child):
                    upstream_context.append({
                        "process_id": upstream.process_id,
                        "task_id": upstream.active_task_id,
                        "status": upstream.status.value,
                        "outcome": upstream.result,
                        "result_commit": str(
                            (upstream.worktree or {}).get("result_commit") or ""
                        ),
                    })
                child_context_now["upstream_outcomes"] = upstream_context
                if child.worktree:
                    child_context_now["worktree"] = dict(child.worktree)
                child.replace_context_snapshot(child_context_now)
                from backend.core.loop.validation_evidence import review_target_version, review_is_current
                reviewed_version = (review_target_version(review_target)
                                    if actor_kind == "reviewer" and review_target is not None else None)
                lifecycle_stage = "agent_loop"
                result = _run_agent_step(
                    process=child,
                    llm_provider=llm_provider,
                    instruction=task_description,
                    dispatcher=dispatcher,
                    workspace_path=execution_workspace,
                    on_stream_event=emit_child_event,
                )
                lifecycle_stage = "verify_output_interfaces"
                boundary_context, _ = child.read_context_snapshot()
                boundary_contract = dict(
                    dict(boundary_context.get("task_contract") or {}).get("interface_contract")
                    or interface_contract or {}
                )
                output_violations = verify_declared_contract(
                    boundary_contract,
                    execution_workspace,
                    list(output_interfaces or []),
                )
                if output_violations and child.status == ProcessStatus.COMPLETED:
                    transition(child, ProcessStatus.FAILED)
                    child.result = {
                        "status": "failed",
                        "process_id": child.process_id,
                        "code": "INTERFACE_CONTRACT_VIOLATION",
                        "error": "declared output interface was not satisfied",
                        "violations": output_violations,
                    }
                    result = child.result
                    return result
                if (
                    isolated_code_action
                    and child.status == ProcessStatus.COMPLETED
                    and child.worktree.get("isolated")
                ):
                    lifecycle_stage = "seal_worktree"
                    manager.seal_process_worktree(child)
                    if isinstance(result, dict):
                        result.setdefault("metadata", {})["worktree"] = dict(
                            child.worktree
                        )
                    emit_child_event({
                        "event": "worktree_sealed",
                        "worktree": dict(child.worktree),
                    })
                if actor_kind == "reviewer" and review_target_id:
                    lifecycle_stage = "record_review_approval"
                    target = manager.get(review_target_id)
                    if (
                        target is not None
                        and child.status == ProcessStatus.COMPLETED
                        and isinstance(child.review_claim, dict)
                        and child.review_claim.get("verdict") == "approved"
                    ):
                        current = review_is_current(target, reviewed_version)
                        child.review_claim["target_version"] = reviewed_version
                        child.review_claim["applicability"] = "current" if current else "needs_recheck"
                        if isinstance(result, dict):
                            result["review_applicability"] = child.review_claim["applicability"]
                        if current:
                            target.runtime_preferences.setdefault("review_evidence", {})[child.process_id] = reviewed_version
                            target.review_approvals.append(child.process_id)
                return result
            except Exception as exc:
                # The child-owned terminal event is the persistence/UI
                # barrier.  Convert failures here, before that event is
                # emitted, rather than relying on Manager's outer thread
                # wrapper to change status after a false completed event.
                transition(child, ProcessStatus.FAILED)
                if manager.worktree_manager is not None:
                    retained = manager.worktree_manager.storage.load_worktree_state(child.process_id)
                    if retained:
                        child.worktree = dict(retained)
                child.result = {
                    "status": "failed",
                    "process_id": child.process_id,
                    "code": "CHILD_LIFECYCLE_FAILED",
                    "error": str(exc),
                    "metadata": {
                        "automatic_replay_suppressed": True,
                        "lifecycle_stage": lifecycle_stage,
                        "worktree": dict(child.worktree or {}),
                    },
                }
                result = child.result
                return result
            finally:
                if actor_kind == "reviewer" and child.worktree.get("isolated"):
                    try:
                        manager.dispose_process_worktree(child, keep_ref=False)
                    except Exception as cleanup_exc:
                        emit_child_event({
                            "event": "worktree_cleanup_failed",
                            "reason": str(cleanup_exc),
                        })

        def publish_terminal():
            try:
                emit_child_event({"event": "agent_terminal", "status": child.status.value,
                                  "outcome": child.result})
            except Exception as exc:
                emit_child_event({"event": "storage_health", "storage": {
                    "level": "blocked", "reasons": ["child_terminal_checkpoint_failed"], "message": str(exc)}})
                raise

        resource_mode = (
            "exclusive"
            if actor_kind == "reviewer" or profile.allows_self_execution
            else "shared"
        )
        review_process = manager.get(review_target_id) if review_target_id else None
        review_uses_isolated = bool(
            actor_kind == "reviewer"
            and review_process
            and (review_process.worktree or {}).get("result_commit")
        )
        if manager.worktree_manager is not None and (
            (task_kind == "action" and profile_id == "development.workspace")
            or review_uses_isolated
        ):
            # File claims protect shared workspaces. Isolated linked
            # worktrees have distinct data planes, so unrelated writers
            # must not be serialized by their logical target paths.
            claims = [f"worktree:{child.process_id}"]
        manager.start(
            child.process_id,
            run_child,
            resources=claims,
            resource_mode=resource_mode,
            on_terminal=publish_terminal,
        )
        return child


    ctx.spawn_child = _spawn_child
