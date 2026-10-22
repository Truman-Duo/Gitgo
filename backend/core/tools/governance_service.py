"""Project-governance operations, separate from arbitrary native programs."""
from copy import copy
from pathlib import Path

from backend.core.errors import error_payload
from backend.core.loop.agent_tool import AgentTool, CancellationMode
from backend.core.loop.execution_contract import data_broker
from backend.core.prompt_templates import load_prompt


def _failed(code, *, effect_state="not_committed", execution_state="not_started"):
    result = error_payload(code, state_changed=effect_state in {"partial", "committed"})
    result["error_info"].update(effect_state=effect_state, execution_state=execution_state,
        state_changed=None if effect_state == "ambiguous" else effect_state in {"partial", "committed"})
    return {**result, "effect_state": effect_state, "execution_state": execution_state}


def build_formalize_tool(session):
    root = session.workspace_path

    def prepare(args):
        return {**dict(args or {}), "_workspace": str(root)}

    def execute(args):
        if Path(args["_workspace"]).resolve() != Path(root).resolve():
            return _failed("GOVERNANCE_WORKSPACE_MISMATCH")
        from backend.adapters.local_git_runner import LocalGitRunner, ProtectedLocalGitRunner
        from backend.core.sync_session.state import GovernanceStateChanged, GovernanceStateUnavailable
        from backend.core.storage import StorageCommitUnknown, StorageBlocked
        from backend.core.sandbox import SandboxDenied
        from backend.core.loop.git_execution import GitBoundaryError
        from backend.core.sync_session import SyncSession
        try:
            working = SyncSession(session.project, session.config)
            for name in ("ws_git_runner", "bk_git_runner"):
                runner = getattr(working, name)
                if not isinstance(runner, LocalGitRunner):
                    return _failed("GIT_BOUNDARY_UNAVAILABLE")
                setattr(working, name, ProtectedLocalGitRunner(runner._repo))
            working.entries = list(session.entries)
            working.on_log = session.on_log
            working.on_stage_changed = session.on_stage_changed
            working.step_load_commits()
            indices = args.get("indices")
            if indices is not None and any(type(i) is not int or i < 0 or i >= len(working.commits) for i in indices):
                return _failed("FORMALIZE_INVALID_SELECTION")
            result = working.step_create_formal_commit(
                selected_indices=None if indices is None else set(indices), message=args["message"])
            if result is None:
                return _failed("FORMALIZE_FAILED")
        except StorageCommitUnknown:
            return _failed("STORAGE_COMMIT_UNKNOWN", effect_state="ambiguous", execution_state="unknown")
        except GovernanceStateChanged:
            reserved = getattr(locals().get("working"), "_formal_number_reservation", None) is not None
            return _failed("GOVERNANCE_STATE_CHANGED", effect_state="partial" if reserved else "not_committed", execution_state="completed")
        except GovernanceStateUnavailable:
            reserved = getattr(locals().get("working"), "_formal_number_reservation", None) is not None
            return _failed("GOVERNANCE_STATE_UNAVAILABLE", effect_state="partial" if reserved else "not_committed",
                           execution_state="completed" if reserved else "not_started")
        except StorageBlocked as exc:
            return _failed(getattr(exc, "code", "STORAGE_BLOCKED"), effect_state="ambiguous", execution_state="unknown")
        except (GitBoundaryError, SandboxDenied) as exc:
            return _failed(exc.code)
        except OSError:
            return _failed("GOVERNANCE_STATE_UNAVAILABLE", effect_state="ambiguous", execution_state="unknown")
        for name in ("formal_commits", "selected_workspace", "commits", "stage", "_governance_state_ref", "_governance_state_present", "_last_op"):
            setattr(session, name, copy(getattr(working, name)))
        response = {"commit": f"[{result.prefix}-{result.number}]", "message": result.message,
                    "source_indices": sorted(result.source_indices), "governance_state_ref": working._governance_state_ref,
                    "git_commit_created": False, "effect_state": "committed"}
        if working._metadata_audit_warning:
            response["warnings"] = [working._metadata_audit_warning]
        return response

    return AgentTool(name="formalize", description=load_prompt("tools/formalize"),
        parameters={"type": "object", "properties": {
            "indices": {"type": "array", "items": {"type": "integer", "minimum": 0}},
            "message": {"type": "string"}}, "required": ["message"], "additionalProperties": False},
        execute=execute, prepare_args=prepare, read_only=False,
        resources=["governance:formal-commits"], cancellation=CancellationMode.NON_INTERRUPTIBLE,
        execution_contract=data_broker("governance.formalize"))
