"""Publish's fixed-tree checks use the existing registered native test runtime."""
from dataclasses import replace

from .file_candidate_runtime import FileCandidateRuntime
from .registered_test_runtime import RegisteredTestRuntime
from .workspace_candidate_runtime import WorkspaceCandidateRuntime


class PublishCheckRuntime(FileCandidateRuntime):
    def __init__(self, *args):
        super().__init__(*args)
        self.producers = []

    def bind_operation(self, operation, item_id, repository):
        from .publish_recovery import save
        self.runtime_key = "check/" + operation["id"]
        self.record = {"version": 1, "operation_id": operation["id"], "binding": operation["binding"],
                       "owner": list(self.owner), "producers": [], "state": "prepared"}
        save(self.storage, self.runtime_key, self.record)
        # A per-check cancellation view must not cancel the surrounding Agent task.
        self.ctx = replace(self.ctx, cancellation=PublishCancellation(self.ctx.cancellation, repository, item_id))
        return self.runtime_key

    def lifecycle(self, phase, stage, observation):
        from .publish_recovery import save, resource_location
        from backend.core.publish.state import digest
        if phase == "allocated":
            recovery_id = digest({"operation": self.record["operation_id"], "producer": len(self.record["producers"])})[:32]
            self.record["producers"].append({"stage": str(stage), "recovery_id": recovery_id,
                                            "location": resource_location(recovery_id)})
        producer = self.record["producers"][-1]
        if producer["stage"] != str(stage):
            raise RuntimeError("Publish producer identity changed")
        producer.update(phase=phase, observation=observation)
        save(self.storage, self.runtime_key, self.record)
        return producer["recovery_id"]

    def verify_tree(self, image, plan):
        from pathlib import Path
        import tempfile
        from backend.core.sandbox import SandboxDenied
        stages = (self.storage.paths.project_root / "publish-inputs").resolve()
        if stages.is_relative_to(Path(self.workspace).resolve()):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Publish input view overlaps the workspace")
        stages.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="tree-", dir=stages)).resolve()
        environment = None
        try:
            if plan.get("environment_policy"):
                from backend.core.publish.environment import snapshot
                environment = snapshot()
            from .publish_recovery import save
            self.record.update(source_stage=str(stage), state="running")
            save(self.storage, self.runtime_key, self.record)
            image.materialize(stage)
            private_context = replace(self.ctx, workspace_path=str(stage), storage=self.storage)
            plugin_result = self._run_plugins(private_context, image, plan) if plan.get("plugins") else None
            if plugin_result is not None:
                self.record.update(plugin_result=plugin_result, native_producers=self.producers)
                save(self.storage, self.runtime_key, self.record)
            if plugin_result is not None and (not plugin_result.get("passed") or plugin_result.get("selection")):
                from backend.core.publish.checks import planned
                result = {"error": plugin_result.get("error", "PUBLISH_PLUGIN_SELECTION_REVIEW_REQUIRED" if plugin_result.get("selection") else "PUBLISH_PLUGIN_PROTOCOL_INVALID"),
                          "cleanup_pending": plugin_result.get("error") == "SANDBOX_CLEANUP_PENDING",
                          "check_results": [{"check": check, "passed": False, "state": "not_run", "error": "PUBLISH_PLUGIN_BLOCKED"}
                                            for check in planned(plan)]}
            else:
                result = self._run_checks(private_context, image, plan)
            if plugin_result is not None:
                result["plugin_result"] = plugin_result
            if environment is not None:
                result["environment"] = environment
                if snapshot()["digest"] != environment["digest"]:
                    result.update(passed=False, error="PUBLISH_ENVIRONMENT_CHANGED", environment_error="PUBLISH_ENVIRONMENT_CHANGED")
        except (OSError, RuntimeError, ValueError, SandboxDenied) as exc:
            result = {"error": getattr(exc, "code", "PUBLISH_TEST_UNAVAILABLE"),
                      "cleanup_pending": getattr(exc, "code", "") == "SANDBOX_CLEANUP_PENDING",
                      "check_results": self.record.get("check_results", []),
                      "environment_error": getattr(exc, "code", "PUBLISH_ENVIRONMENT_UNAVAILABLE")}
        finally:
            # This is the parent-only immutable source view. The native
            # producer owns its separate retained view/access journal.
            WorkspaceCandidateRuntime._discard_stage(stage, stages)
        from .publish_recovery import save
        self.record.update(state="finished", result=result, native_producers=self.producers)
        save(self.storage, self.runtime_key, self.record)
        return result

    def _run_plugins(self, context, image, plan, *, completion=None):
        import hashlib
        import json
        from backend.core.publish.plugins import validate_message, validate_selection
        from backend.core.publish.state import digest
        from backend.core.sandbox import SandboxDenied
        from backend.core.storage.redaction import redact_for_persistence
        producer = WorkspaceCandidateRuntime(self.pipeline, context, self.registration, self.owner, self.tool)
        producer.lifecycle = self.lifecycle
        diagnostics = {}
        try:
            raw = self.storage.read_blob(plan["plugin_snapshot_ref"])
            if "sha256:" + hashlib.sha256(raw).hexdigest() != plan["plugin_snapshot_ref"]:
                raise ValueError("Plugin snapshot identity changed")
            bundle = json.loads(raw)
            arguments = {"bundle": bundle, "entries": bundle["entries"], "message": plan["commit_message"]}
            if completion is not None:
                arguments.update(phase="complete", completion=completion)
            record = producer.preview("run_publish_plugins", arguments, 30, trusted_arguments={}, copy_inputs=True,
                diagnostics=diagnostics, input_names=tuple(name for name, _, _ in image.files))
            expected = {name: hashlib.sha256(raw).hexdigest() for name, raw, _ in image.files}
            if record.get("input_versions_after") != expected:
                raise SandboxDenied("FILE_CHANGED", "Plugin modified the fixed final tree")
            if self.ctx.is_cancelled() or not self.is_current():
                raise SandboxDenied("TOOL_CANCELLED", "Plugin finished after cancellation")
            result = record["result"]
            if not isinstance(result, dict):
                raise ValueError("Invalid plugin result")
            if result.get("passed") is not True or result.get("error"):
                return {"error": result.get("error", "PUBLISH_PLUGIN_PROTOCOL_INVALID"),
                        "observations": result.get("observations", []), "summary": result.get("summary", "")}
            if completion is not None:
                return {"passed": True, "observations": result.get("observations", []), "validated_plan": digest(plan)}
            if "selection" in result:
                selection = validate_selection(result["selection"], bundle)
                return {"passed": True, "selection": selection, "observations": result.get("observations", []), "validated_plan": digest(plan)}
            files = [(name, "100644", "", raw) for name, raw, _ in image.files]
            message = validate_message(result.get("message"), plan, bundle["configuration"], files)
            if redact_for_persistence({"message": message})["message"] != message:
                raise SandboxDenied("PUBLISH_PLUGIN_PRIVACY_BLOCKED", "Plugin message cannot be persisted verbatim")
            return {"passed": True, "message": message, "observations": result.get("observations", []), "validated_plan": digest(plan)}
        except (OSError, RuntimeError, ValueError, SandboxDenied) as exc:
            return {"error": getattr(exc, "code", "PUBLISH_PLUGIN_PROTOCOL_INVALID")}
        finally:
            self.last_render = producer.last_render
            self.producers.append({"role": "completion_plugin" if completion is not None else "commit_plugin", **producer.last_render, "diagnostics": diagnostics})

    def complete_tree(self, image, plan, completion):
        """One durable, isolated completion attempt after a known Git result."""
        from pathlib import Path
        import tempfile
        from backend.core.sandbox import SandboxDenied
        from .publish_recovery import save
        stages = (self.storage.paths.project_root / "publish-inputs").resolve()
        if stages.is_relative_to(Path(self.workspace).resolve()):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Publish input view overlaps the workspace")
        stages.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="complete-", dir=stages)).resolve()
        try:
            self.record.update(source_stage=str(stage), state="running")
            save(self.storage, self.runtime_key, self.record)
            image.materialize(stage)
            context = replace(self.ctx, workspace_path=str(stage), storage=self.storage)
            result = self._run_plugins(context, image, plan, completion=completion)
            result["cleanup_pending"] = result.get("error") == "SANDBOX_CLEANUP_PENDING"
        except (OSError, RuntimeError, ValueError, SandboxDenied) as exc:
            result = {"error": getattr(exc, "code", "PUBLISH_PLUGIN_COMPLETION_UNAVAILABLE"),
                      "cleanup_pending": getattr(exc, "code", "") == "SANDBOX_CLEANUP_PENDING"}
        finally:
            WorkspaceCandidateRuntime._discard_stage(stage, stages)
        self.record.update(state="finished", result=result, native_producers=self.producers)
        save(self.storage, self.runtime_key, self.record)
        return result

    def _run_checks(self, context, image, plan):
        import time
        from backend.core.publish.checks import planned, accepted
        from backend.core.sandbox import SandboxDenied
        from .publish_recovery import save
        rows, deadline, stopped = [], time.monotonic() + 210, ""
        for check in planned(plan):
            if not stopped and self.ctx.is_cancelled():
                stopped = "TOOL_CANCELLED"
            elif not stopped and time.monotonic() >= deadline:
                stopped = "PUBLISH_CHECK_DEADLINE"
            if stopped:
                rows.append({"check": check, "passed": False, "error": stopped, "state": "not_run"})
                continue
            self.record.update(current_check=check["id"], check_results=list(rows))
            save(self.storage, self.runtime_key, self.record)
            runtime = RegisteredTestRuntime(self.pipeline, context, self.registration, self.owner, self.tool,
                timeout=min(check["timeout"] * len(check["seeds"]) + 15, max(1, int(deadline - time.monotonic()))))
            runtime.lifecycle = self.lifecycle
            try:
                result = runtime.run({"test_id": "publish:" + self.record["operation_id"] + ":" + check["id"],
                    "target": check["target"], "seeds": check["seeds"], "timeout": check["timeout"],
                    "input_files": [name for name, _, _ in image.files]})
            except (OSError, RuntimeError, ValueError, SandboxDenied) as exc:
                result = {"error": getattr(exc, "code", "PUBLISH_TEST_UNAVAILABLE")}
            finally:
                self.last_render = runtime.last_render
                self.producers.extend(runtime.producers)
            rows.append({**result, "check": check, "state": "observed"})
            self.record.update(check_results=list(rows), native_producers=self.producers)
            save(self.storage, self.runtime_key, self.record)
            if result.get("error") in {"SANDBOX_CLEANUP_PENDING", "TOOL_CANCELLED"}:
                stopped = result["error"]
        passed = accepted(plan, {"check_results": rows})
        return {"check_results": rows, "passed": passed, "verification_state": "current" if passed else "needs_recheck",
                "cleanup_pending": stopped == "SANDBOX_CLEANUP_PENDING",
                "error": stopped or next((row["error"] for row in rows if row.get("error")), ""),
                "seed_results": [seed for row in rows for seed in row.get("seed_results", [])]}


class PublishCancellation:
    def __init__(self, parent, repository, item_id):
        self.parent, self.repository, self.item_id = parent, repository, item_id

    def is_set(self):
        if self.parent.is_set():
            return True
        # This is an explicit running command, never a read/query side effect.
        ledger, _ = self.repository.load()
        return ledger["items"][self.item_id]["status"] == "pausing"


def local_check_runtime(project, tool, request_id, *, cancelled=lambda: False):
    """An explicit operator invocation; no model, fake results or host pytest."""
    from .execution_context import ExecutionContext
    from .models import AgentProcess, RingLevel
    from .session import AgentSession
    from .tools import ToolRegistry
    from .tool_pipeline import ToolPipeline
    from .permission_broker import tool_contract_digest
    process = AgentProcess("publish-local-" + request_id, "operator", RingLevel.RING_3,
        active_task_id="publish-" + request_id, worktree_path=project.workspace_path,
        session=AgentSession(), tool_registry=ToolRegistry([tool.name]))
    class Cancellation:
        def is_set(self):
            return process.cancellation_event.is_set() or cancelled()
    ctx = ExecutionContext(process, process.session, project.workspace_path, cancellation=Cancellation())
    from .publish_build_runtime import PublishBuildRuntime
    return PublishBuildRuntime(ToolPipeline(), ctx, tool_contract_digest(tool),
                               (process.process_id, process.active_task_id), tool)
