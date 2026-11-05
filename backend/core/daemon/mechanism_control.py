"""User mechanism commands and deterministic continuation, without model steering."""
from __future__ import annotations

from copy import deepcopy

from backend.core.loop import mechanisms
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.lifecycle import transition


def _resolve(cmd, session, ctx, *, create, observation=False):
    manager, store = ctx["apm"], ctx.get("session_store")
    process_id = str(cmd.get("process_id") or "")
    if not process_id:
        roots = [p for p in manager.list_processes() if not p.parent_id and p.session is not None]
        if roots:
            process_id = max(roots, key=lambda p: p.created_at or "").process_id
        elif store:
            state = store.load_latest_root_process_state()
            process_id = str(((state or {}).get("process") or {}).get("process_id") or "")
    if process_id:
        from backend.core.loop.conversation_continuation import inspect_conversation_process, resolve_conversation_process
        resolver = inspect_conversation_process if observation else resolve_conversation_process
        return resolver(manager, store, session.workspace_path, process_id)
    if not create:
        return None
    # An empty parked conversation is a real lifecycle object. Creating it
    # requires neither a provider call nor an invented user task.
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.capabilities import CapabilityProfiles
    process = manager.fork(parent_id=None, role="supervisor", ring_level=RingLevel.RING_0,
                           tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
                           max_steps=50, actor_kind="supervisor", capability_profile_id="supervisor.control",
                           task_kind="answer", workspace_path=str(session.workspace_path))
    transition(process, ProcessStatus.COMPLETED)
    # This parked settings anchor ran no provider/tool work. Publish a real
    # terminal barrier so recovery cannot mistake it for provisional output.
    if process.task_budget is not None:
        process.budget_lease = process.task_budget.release_process(
            process.process_id, status=process.status.value) or {}
    process.lifecycle_finalized = True
    return process


def resume_waiters(session, project, ctx):
    """Called after activation and task unwind; never answer a permission card."""
    manager = ctx.get("apm")
    if manager is None:
        return
    for process in manager.list_processes():
        pending = process.pending_decision
        if not isinstance(pending, dict) or pending.get("kind") != "manual_execution":
            continue
        if not process._run_lock.acquire(blocking=False):
            continue  # The worker calls us again after releasing this lock.
        resume = False
        try:
            root = mechanisms.owner(process)
            with root.session._mechanism_lock:
                current = mechanisms.snapshot(process)
                if root.session.mechanism_activation_blocked:
                    continue
                if process.status != ProcessStatus.AWAITING_USER or process.pending_decision is not pending:
                    continue
                from backend.core.loop.task_maintenance import state_for, is_suspended
                if is_suspended(process):
                    continue
                maintenance = state_for(process)
                requirements_changed = any(c.get("status") == "pending" for c in maintenance.get("changes", {}).values())
                if (process.parent_id and (current["mode"] == "auto" or requirements_changed)
                        and not manager.has_continuation(process.process_id)):
                    process.runtime_preferences["mechanism_resume_blocked"] = "CHILD_CONTINUATION_UNAVAILABLE"
                    continue
                store = ctx.get("session_store")
                before = (process.status, process.pending_decision, process.result,
                          deepcopy(process.pending_tool_batch))
                ledger_start = len(process.session.host_ledger)
                if current["mode"] == "auto" or requirements_changed:
                    process.pending_decision = None
                    process.result = None
                    process.pending_tool_batch["decision_action"] = "automatic"
                    transition(process, ProcessStatus.RUNNING)
                    resume = True
                elif pending["manual_unit"]["revision"] != current["revision"]:
                    unit = {**pending["manual_unit"], "revision": current["revision"]}
                    process.pending_decision = mechanisms.publish_manual_decision(process, unit)
                    process.pending_tool_batch["decision_id"] = process.pending_decision["decision_id"]
                    process.pending_tool_batch["manual_unit"] = unit
                else:
                    continue
                process.session.host_ledger.append({"event": "user_decision_cancelled",
                    "decision_id": pending["decision_id"], "task_id": process.active_task_id,
                    "kind": "manual_execution", "reason": "requirements_changed" if requirements_changed else "mode_auto" if resume else "mechanism_revision_changed"})
                try:
                    if store is None:
                        raise ValueError("MECHANISM_CHECKPOINT_UNAVAILABLE")
                    store.save_process_checkpoint(process)
                except Exception as exc:
                    from backend.core.storage import StorageCommitUnknown
                    if not isinstance(exc, StorageCommitUnknown):
                        process.status, process.pending_decision, process.result, process.pending_tool_batch = before
                        del process.session.host_ledger[ledger_start:]
                    else:
                        from backend.core.loop.lifecycle import block_uncertain_checkpoint
                        block_uncertain_checkpoint(process)
                    raise
                if resume and process.task_budget is not None:
                    process.task_budget.resume_from_user()
        except Exception as exc:
            resume = False
            queue = ctx.get("evq")
            if queue is not None:
                queue.put({"event": "mechanism_continuation_blocked", "process_id": process.process_id,
                           "error": str(exc)})
        finally:
            process._run_lock.release()
        if resume:
            if process.parent_id:
                manager.resume_child(process.process_id)
                continue
            from .dispatch import _cmd_task
            _cmd_task({"action": "continue_mechanism", "process_id": process.process_id,
                       "session_id": process.session.session_id, "task_id": process.active_task_id,
                       "role": process.role, "task_kind": process.task_kind,
                       "actor_kind": process.actor_kind, "capability_profile_id": process.capability_profile_id,
                       "max_steps": process.max_steps, "instruction": ""},
                      session, project, ctx, lambda event: ctx["evq"].put(event))


def command(cmd, session, project, ctx, emit):
    try:
        if cmd.get("task_operation"):
            operation = cmd["task_operation"]
            if operation not in {"status", "catalog", "preview", "suspend", "resume", "detach", "reconcile"}:
                raise ValueError("TASK_MECHANISM_OPERATION_INVALID")
            process = _resolve(cmd, session, ctx, create=False, observation=operation in {"status", "catalog"})
            if process is None:
                if operation != "status":
                    raise ValueError("TASK_MECHANISM_NOT_FOUND")
                result = {"state": {}, "source_refs": [], "authority_granted": False}
            else:
                from backend.core.loop.task_maintenance import invoke, task_id
                from backend.core.loop.mechanism_lifecycle import fence
                with fence(process), process.session._mechanism_lock, process._context_lock:
                    if operation not in {"status", "catalog"} and cmd.get("task_id") != task_id(process):
                        raise ValueError("TASK_MECHANISM_TARGET_CHANGED")
                    if operation == 'reconcile':
                        from backend.core.mechanism_runtime.application import Application
                        from .dispatch import _resolve_llm_config, _runtime_llm_from_config
                        app = getattr(process, '_harness_application', None)
                        provider = ctx.get('llm') or (app.provider if app else None)
                        if provider is None:
                            config = _resolve_llm_config(str(session.workspace_path))
                            provider = _runtime_llm_from_config(config) if config else None
                        if app is None:
                            app = Application(process, provider)
                            process._harness_application = app
                        elif provider is not None:
                            app.bind_provider(provider, app.emit)
                        result = app.reconcile({'instance_id':cmd.get('instance_id'),
                            'expected_revision':cmd.get('expected_revision')})
                        ctx['apm'].notify_state_changed(process.process_id)
                    else:
                        result = invoke(process, {"operation": operation, "expected_revision": cmd.get("expected_revision"),
                                                  "contract": cmd.get("contract")}, user_control=True)
            emit({"event": "command_result", "cmd": "mechanism", "result": result})
            if operation == "resume":
                try:
                    resume_waiters(session, project, ctx)
                except Exception as exc:
                    # Resume has committed; a failed wakeup must not replay it.
                    queue = ctx.get("evq")
                    if queue is not None:
                        queue.put({"event": "mechanism_continuation_blocked", "error": str(exc)})
            return
        process = _resolve(cmd, session, ctx, create=cmd.get("mode") is not None, observation=cmd.get("mode") is None)
        if process is None:
            result = {"mode": "auto", "revision": 0, "phase": "effective", "process_id": ""}
        else:
            if cmd.get("mode") is not None:
                result = mechanisms.activate(process, cmd["mode"], cmd.get("expected_revision"), ctx.get("session_store"))
            else:
                result = mechanisms.snapshot(process)
            result = {**result, "process_id": process.process_id, "session_id": process.session.session_id}
    except Exception as exc:
        emit({"event": "command_result", "cmd": "mechanism", "error": str(exc)})
        return
    emit({"event": "command_result", "cmd": "mechanism", "result": result})
    if cmd.get("mode") is not None:
        queue = ctx.get("evq")
        try:
            if queue is not None:
                queue.put({"event": "mechanism_revision_activated", **result})
            resume_waiters(session, project, ctx)
        except Exception as exc:
            # The mode has committed. A failed consumer wakeup cannot turn
            # that fact into a failed/replayed activation response.
            if queue is not None:
                queue.put({"event": "mechanism_continuation_blocked", "error": str(exc),
                           "mode_effective": True, "revision": result["revision"]})
