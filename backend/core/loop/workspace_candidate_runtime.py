"""Run an admitted native producer in a private view, then submit frozen bytes.

The existing invocation journal owns effect uncertainty. CAS/state_refs retain
pending candidates only; they do not grant authority or replace file commits.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil

from backend.core.sandbox import SandboxDenied
from backend.core.tools.file_mutation import FileMutation, file_service_scope
from backend.core.tools.file_mutation_batch import FileMutationBatch, commit_file_batch
from backend.core.tools.workspace_projection import capture, project
from .file_candidate_runtime import FileCandidateRuntime

_NAMESPACE = "workspace_candidates.v1"


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _failure(code, detail):
    from backend.core.errors import error_payload
    return {**error_payload(code, message=str(detail)[:2000]),
            "effect_state": "not_committed", "execution_state": "not_started"}


class WorkspaceCandidateRuntime(FileCandidateRuntime):
    def execute(self, handler, args, timeout, *, diagnostics, runtime_metadata, trusted_arguments=None, result_kind="command"):
        if self.ctx.is_cancelled() or getattr(self.ctx.process, "cancel_requested", False):
            from .tool_pipeline import ToolExecutionCancelled
            raise ToolExecutionCancelled("Native producer cancelled before launch", effect_state="not_committed")
        if result_kind not in {"command", "authored"} or any(k != "_dynamic_spec" for k in (trusted_arguments or {})):
            return _failure("SANDBOX_POLICY_INVALID", "Unknown trusted native producer contract")
        self.trusted_arguments = json.loads(_encode(trusted_arguments or {}))
        self.result_kind = result_kind
        self.collect_outputs = True
        self.copy_inputs = True
        storage, root = self.storage, Path(self.workspace).resolve()
        public = {k: v for k, v in args.items() if not k.startswith("_") and k not in {"candidate_ref", "lesson_feedback_id"}}
        identity = {"owner": self.owner, "session_id": self.session_id,
            "workspace": str(root), "project_id": storage.paths.project_id,
            "handler": handler, "registration": self.registration, "arguments": public}
        if self.trusted_arguments or result_kind != "command":
            identity.update(trusted_arguments=self.trusted_arguments, result_kind=result_kind)
        key = hashlib.sha256(_encode(identity)).hexdigest()
        lock = storage.paths.state_home / "mutation-locks" / ("workspace-" + key)
        lock.mkdir(parents=True, exist_ok=True)
        from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
        try:
            lease = StorageLease(lock, exclusive=True)
        except StorageMaintenanceActive:
            return _failure("FILE_MUTATION_BUSY", "The exact execution candidate is in use")
        with lease:
            reference, record, source = None, None, "unavailable"
            try:
                reference = storage.get_state_ref(_NAMESPACE, key)
                record = json.loads(storage.read_blob(reference)) if reference else None
                if args.get("candidate_ref") and args["candidate_ref"] != reference:
                    return _failure("FILE_CANDIDATE_STALE", "The exact pending candidate is unavailable; no command was started")
                if record is None:
                    if args.get("lesson_feedback_id"):
                        return _failure("FILE_CANDIDATE_STALE", "Feedback cannot start a replacement producer")
                    record = self._produce(handler, public, timeout, diagnostics)
                    record["identity"] = identity
                    reference = storage.put_blob(_encode(record), media_type="application/vnd.gitgo.workspace-candidate+json")
                    storage.put_state_ref(_NAMESPACE, key, reference)
                    source = "generated"
                else:
                    source = "frozen"
                # JSON round-tripping turns the immutable owner tuple into a list.
                if _encode(record["identity"]) != _encode(identity) or record.get("version") != 1:
                    return _failure("FILE_CANDIDATE_STALE", "Pending candidate binding changed")
                if record["producer"].get("cleanup", {}).get("complete") is not True or record["producer"].get("access_retired") is not True:
                    return _failure("SANDBOX_CLEANUP_PENDING", "Producer retirement is unconfirmed")
                self.last_render = record["producer"] if source == "generated" else {}
                result = self._commit(record, args)
            except Exception as exc:
                # _commit handles effectful exceptions separately. Exceptions here
                # can only precede a formal commit or follow a returned result.
                result = _failure(getattr(exc, "code", "WORKSPACE_CANDIDATE_UNAVAILABLE"), exc)

            finally:
                if isinstance(self.last_render.get("cleanup"), dict):
                    runtime_metadata["cleanup"] = self.last_render["cleanup"]
            if reference:
                result.update(candidate_ref=reference, candidate_source=source)
            if record:
                result["candidate_render"] = record.get("producer", {})
                if record.get("result_kind") != "authored":
                    result["command_result"] = record.get("result", {})
                producer = record.get("producer", {})
                result["producer_completed"] = (producer.get("launch_state") == "started" and producer.get("exit_code") == 0
                    and producer.get("access_retired") is True and (record.get("result_kind") == "authored" or type(record.get("result", {}).get("exit_code")) is int))
                result["producer_exit_code"] = producer.get("exit_code") if record.get("result_kind") == "authored" else record.get("result", {}).get("exit_code")
                result["output_publication"] = ("committed" if result.get("effect_state") == "committed" else
                    "partial" if result.get("effect_state") == "partial" else "unknown" if result.get("effect_state") == "ambiguous" else
                    "not_required" if not record.get("members") and result.get("input_applicability") == "current" else "withheld")
                # Child output cannot manufacture file/lesson/commit evidence.
                for name in ("argv", "cwd", "stdout", "stderr", "exit_code", "success", "truncated", "shell", "purpose"):
                    if name == "success" and result.get("error"):
                        continue
                    if name in record.get("result", {}) and name not in result:
                        result[name] = record["result"][name]
            if record and record.get("result_kind") == "authored":
                result["tool_output"] = record.get("result", {}).get("tool_output")
                spec = self.trusted_arguments.get("_dynamic_spec") or {}
                result.update(definition_digest=spec.get("digest", ""), definition_version=spec.get("version", 1))
            render = record.get("producer", {}) if record else self.last_render
            if render:
                result["candidate_render"] = render
                result["producer_observation"] = "retained_candidate" if source == "frozen" else "current_invocation"
                if not record:
                    # A retired wrapper may have started the command before its
                    # protocol result was lost. Empty output is not non-execution.
                    result["producer_completed"] = False if render.get("launch_state") == "not_started" else None
                if render.get("launch_state") in {"started", "unknown"}:
                    result["execution_state"] = ("completed" if record else "unknown" if result.get("error") == "SANDBOX_CLEANUP_PENDING" else
                        "timed_out" if result.get("error") == "TOOL_TIMEOUT" else "cancelled" if result.get("error") == "TOOL_CANCELLED" else "failed")
                info = result.get("error_info")
                if isinstance(info, dict):
                    info["details"].update(formal_effect_state=result.get("effect_state"),
                        producer_launch_state=render.get("launch_state", "unknown"),
                        producer_completed=result.get("producer_completed"), producer_observation=result["producer_observation"])
                for action in result.get("next_actions", []):
                    if reference and action.get("action") == self.tool.name:
                        action["candidate_ref"] = reference
            if reference and (not result.get("error") or result.get("producer_failed")):
                try:
                    storage.release_state_ref(_NAMESPACE, key, expected_ref=reference)
                except Exception as exc:
                    result.setdefault("warnings", []).append({"code": "WORKSPACE_CANDIDATE_RETIRE_FAILED", "error_type": type(exc).__name__,
                        "message": "Execution output was retained; inspect completed effects before any repeat."})
            return result

    def preview(self, handler, inputs, timeout, *, trusted_arguments, copy_inputs, diagnostics, input_names=()):
        """Disposable native execution; never stores or publishes output files."""
        if handler not in {"authored_python", "authored_privileged_python", "authored_host_python", "run_test"} or any(
                k not in {"_dynamic_spec", "_host_results"} for k in trusted_arguments):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Unknown private self-test contract")
        self.trusted_arguments = json.loads(_encode(trusted_arguments))
        self.result_kind, self.collect_outputs, self.copy_inputs = "authored", False, copy_inputs
        self.input_names = tuple(input_names)
        return self._produce(handler, inputs, timeout, diagnostics)

    def _produce(self, handler, public, timeout, diagnostics):
        from backend.core.sandbox_access import WorkspaceAccess, native_runtime_access
        from .execution_contract import NativeInvocation
        from .read_approval import native_cleanup_pending, runtime_cleanup_report
        if native_cleanup_pending(self.ctx.process):
            raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "Prior native resources require inspection")
        root = Path(self.workspace).resolve()
        from backend.core.tools.workspace_projection import WorkspaceImage
        before = capture(root) if self.copy_inputs else WorkspaceImage((), ())
        project_root = self.storage.paths.project_root.resolve()
        from .candidate_workspace import allocate_candidate_workspace
        stages, stage = allocate_candidate_workspace(self.storage.paths, "workspace", workspace=self.workspace)
        metadata = {"owner": self.owner, "registration": self.registration}
        self.last_render = {"producer": "native_workspace", "launch_state": "not_started", "cleanup": {"complete": True}, "access_retired": False}
        access, retired = None, False
        try:
            before.materialize(stage)
            # No script rewriting. Only the declared cwd gets a relative mapping;
            # embedded absolute paths remain literal and have no formal-root ACL.
            command_args = dict(public)
            cwd = Path(str(command_args.get("cwd") or "."))
            if cwd.is_absolute():
                command_args["cwd"] = cwd.resolve().relative_to(root).as_posix()
            if os.name == "nt":
                runtimes, shallow = native_runtime_access()
                journals = project_root / "sandbox_access"
                journals.mkdir(parents=True, exist_ok=True)
                access = WorkspaceAccess(stage, runtimes, journals / (stage.name + ".json"), shallow_runtimes=shallow, metadata_roots=[stage.parent])
                access.prepare()
            if not self.is_current() or self.ctx.is_cancelled():
                raise SandboxDenied("TOOL_CANCELLED", "Task changed or cancelled before producer launch")
            actual = {**command_args, **self.trusted_arguments, "_workspace": str(stage)}
            invocation = NativeInvocation.compile(handler, actual, timeout, owner=self.owner, registration=self.registration)
            try:
                result = self.pipeline._execute_isolated(handler, actual, timeout, cancellation_event=self.ctx.cancellation,
                    diagnostics=diagnostics, runtime_metadata=metadata, native_invocation=invocation)
            except Exception as exc:
                from .error_taxonomy import classify_tool_error
                raise SandboxDenied(classify_tool_error(exc, tool_name=self.tool.name).code, str(exc)) from exc
        finally:
            self.last_render.update({k: metadata[k] for k in ("launch_state", "exit_code", "execution_basis") if k in metadata})
            cleanup = runtime_cleanup_report(metadata.get("cleanup"))
            if cleanup is not None:
                self.last_render["cleanup"] = cleanup
            if metadata.get("launch_state") in {"started", "unknown"} and (cleanup or {}).get("complete") is not True:
                self.last_render["cleanup"] = {"complete": False, "mode": "unconfirmed"}
                raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "Retain private execution view until native retirement is confirmed")
            if access is not None:
                try:
                    access.close()
                except Exception as exc:
                    self.last_render["cleanup"] = {"complete": False, "mode": "access_retirement_failed"}
                    raise SandboxDenied("SANDBOX_CLEANUP_PENDING", str(exc)) from exc
            retired = True
            self.last_render["access_retired"] = True
            # Failed launches retain no formal effects; remove the view only after
            # native/access retirement. Successful output is captured below.
            if "result" not in locals() and retired:
                self._discard_stage(stage, stages)
        try:
            if metadata.get("launch_state") != "started" or metadata.get("exit_code") != 0:
                raise SandboxDenied("SANDBOX_EXECUTION_FAILED", "Native launch and protocol exit were not confirmed")
            if not self.collect_outputs:
                observed = {}
                if self.input_names:
                    from backend.core.sandbox_read import ReadRoot
                    try:
                        with ReadRoot(stage) as acquired:
                            for name in self.input_names:
                                with acquired.open(name) as opened:
                                    observed[name] = hashlib.sha256(opened.read_bytes(max_bytes=16 * 1024 * 1024)).hexdigest()
                    except (OSError, ValueError, RuntimeError) as exc:
                        raise SandboxDenied("FILE_CHANGED", "Private validation inputs are unavailable after execution") from exc
                return {"result": result, "producer": self.last_render,
                    "versions": before.versions, "directories": before.directories, "input_versions_after": observed}
            after = capture(stage, output=True)
            batch = project(root, before, after, operation=self.tool.name)
            original_bytes = {name: raw for name, raw, _ in before.files}
            rows = [{"path": c.path.relative_to(root).as_posix(), "operation": c.operation,
                     "before": base64.b64encode(original_bytes.get(c.path.relative_to(root).as_posix(), b"")).decode(),
                     "after": base64.b64encode(c.payload).decode(), "before_sha256": c.before_sha256,
                     "text_view_available": c.text_view_available} for c in batch.members]
            if self.result_kind == "authored":
                failed = bool(result.get("error")) or result.get("success") is False or result.get("passed") is False
                result = {"tool_output": result, **({"success": False, "error": result.get("error") or "CUSTOM_TOOL_EXECUTION_FAILED"} if failed else {})}
            return {"version": 1, "result_kind": self.result_kind, "versions": before.versions, "directories": before.directories,
                    "members": rows, "result": result, "producer": self.last_render}
        finally:
            if retired:
                self._discard_stage(stage, stages)

    @staticmethod
    def _discard_stage(stage, stages):
        import stat
        info = stage.lstat()
        if (stage.resolve().parent != stages or stage.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise SandboxDenied("WORKSPACE_CANDIDATE_UNAVAILABLE", "Private execution view changed identity; retain it for inspection")
        shutil.rmtree(stage)

    def _commit(self, record, args):
        root = Path(self.workspace).resolve()
        output = record["result"]
        if self.ctx.is_cancelled() or getattr(self.ctx.process, "cancel_requested", False):
            return _failure("TOOL_CANCELLED", "Execution completed but publication was cancelled")
        if not self.is_current():
            return _failure("FILE_CANDIDATE_STALE", "Task or tool binding changed before publication")
        def current():
            image = capture(root)
            return (_encode(image.versions) == _encode(record["versions"])
                    and list(image.directories) == list(record["directories"]))
        try:
            members = []
            for row in record["members"]:
                from backend.core.sandbox_read import relative_parts
                relative_parts(row["path"])
                first, last = base64.b64decode(row["before"], validate=True), base64.b64decode(row["after"], validate=True)
                view = row["text_view_available"]
                members.append(FileMutation(row["operation"], root, root / row["path"], row["before_sha256"],
                    first.decode("utf-8") if view else "", last.decode("utf-8") if view else "", after_bytes=last, text_view_available=view))
            if not current():
                return {**_failure("FILE_CHANGED", "Execution inputs changed; no formal output was submitted"), "input_applicability": "changed"}
            if output.get("error") or output.get("success") is False:
                return {**_failure(str(output.get("error") or "COMMAND_EXIT_NONZERO"), "Native command failed; its private changes were not published"),
                        "producer_failed": True, "input_applicability": "current"}
            if not members:
                return {"effect_state": "not_committed", "execution_state": "completed", "file_results": [], "input_applicability": "current"}
        except Exception as exc:
            return _failure("WORKSPACE_CANDIDATE_UNAVAILABLE", exc)
        service_args = {**args, "_mutation_lock_root": str(self.storage.paths.state_home / "mutation-locks")}
        # The same file locks cover all potentially read inputs. No second lock
        # authority is introduced for the formal filesystem.
        with file_service_scope(lambda: self.ctx.is_cancelled() or getattr(self.ctx.process, "cancel_requested", False), self):
            try:
                result = commit_file_batch(FileMutationBatch(root, tuple(members), operation=self.tool.name), service_args,
                    check_versions=current, guard_paths=[root / name for name in record["versions"]])
                result["input_applicability"] = ("changed" if result.get("error") == "FILE_CHANGED" else "unknown"
                    if result.get("effect_state") in {"partial", "ambiguous"} else "current")
                return result
            except Exception as exc:
                return {**_failure("FILE_MUTATION_FAILED", type(exc).__name__), "effect_state": "ambiguous", "execution_state": "unknown"}
