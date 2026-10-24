"""Run private tests, then let the existing manifest own their evidence."""
from pathlib import Path
from backend.core.sandbox import SandboxDenied
from .file_candidate_runtime import FileCandidateRuntime
from .workspace_candidate_runtime import WorkspaceCandidateRuntime
from .test_manifest import run_registered_test


class RegisteredTestRuntime(FileCandidateRuntime):
    def __init__(self, *args, timeout):
        super().__init__(*args)
        self.timeout = timeout
        self.phase = "preflight"
        self.producers = []

    def run(self, args):
        try:
            result = run_registered_test(self.workspace, args, execute=self.execute_seeds, before_record=self.before_record)
        except SandboxDenied as exc:
            from backend.core.errors import error_payload
            return {**error_payload(exc.code, message=str(exc)), "effect_state": "not_committed"}
        if self.phase == "record":
            self.phase = "recorded"
        return result

    def before_record(self):
        if not self.is_current() or self.ctx.is_cancelled() or getattr(self.ctx.process, "cancel_requested", False):
            raise SandboxDenied("TOOL_CANCELLED", "Task changed before recording test evidence")
        self.phase = "record"

    def execute_seeds(self, workspace, args, validation):
        self.phase = "execute"
        producer = WorkspaceCandidateRuntime(self.pipeline, self.ctx, self.registration, self.owner, self.tool)
        diagnostics = {}
        try:
            record = producer.preview("run_test", args, self.timeout, trusted_arguments={}, copy_inputs=True, diagnostics=diagnostics,
                input_names=tuple(validation.get("files", {})))
            if not self.is_current() or self.ctx.is_cancelled() or getattr(self.ctx.process, "cancel_requested", False):
                raise SandboxDenied("TOOL_CANCELLED", "Test finished after cancellation or task change; no manifest updated")
            if validation.get("state") == "observed":
                versions = record["versions"]
                if validation.get("scope") == "declared_inputs":
                    actual = {name: versions[name][0] for name in validation["files"] if name in versions}
                else:
                    from backend.core.dependency_graph import _IGNORED_PARTS
                    actual = {name: version[0] for name, version in versions.items()
                              if not any(part.casefold() in _IGNORED_PARTS for part in Path(name).parts)}
                if actual != validation["files"]:
                    raise SandboxDenied("FILE_CHANGED", "The private test inputs differ from the formal validation witness")
                if record.get("input_versions_after") != validation["files"]:
                    raise SandboxDenied("FILE_CHANGED", "The tests modified their private validation inputs; no current result was recorded")
            return record["result"]
        finally:
            self.last_render = dict(producer.last_render)
            self.producers.append({"role": "registered_test", **self.last_render, "diagnostics": diagnostics})
