"""Native registration supervision within the existing author-tool invocation."""
import json
import time
from .file_candidate_runtime import FileCandidateRuntime
from .workspace_candidate_runtime import WorkspaceCandidateRuntime
from backend.core.sandbox import SandboxDenied
from backend.core.tools.workspace_projection import capture


class AuthoringRuntime(FileCandidateRuntime):
    def __init__(self, *args, timeout):
        super().__init__(*args)
        self.deadline = time.monotonic() + timeout
        self.phase = None
        self.producers = []

    def registration_test(self, spec, test, index):
        self.phase = "tests"
        remaining = min(float(spec["timeout"]), self.deadline - time.monotonic())
        if remaining <= 0:
            raise SandboxDenied("TOOL_TIMEOUT", "Registration self-tests exhausted the invocation allowance")
        mode = spec["execution_mode"]
        handler = {"authored_privileged_python": "authored_privileged_python",
            "authored_host_python": "authored_host_python", "authored_pure_python": "authored_python"}[mode]
        producer = WorkspaceCandidateRuntime(self.pipeline, self.ctx, self.registration, self.owner, self.tool)
        diagnostics = {}
        try:
            record = producer.preview(handler, dict(test["input"]), remaining,
                trusted_arguments={"_dynamic_spec": spec, "_host_results": test.get("host_results") or []},
                copy_inputs=mode == "authored_privileged_python", diagnostics=diagnostics)
            if not self.is_current() or self.ctx.is_cancelled() or getattr(self.ctx.process, "cancel_requested", False):
                raise SandboxDenied("TOOL_CANCELLED", "Task changed or cancelled before accepting a self-test")
            if mode == "authored_privileged_python":
                image = capture(self.workspace)
                if json.dumps(image.versions, sort_keys=True) != json.dumps(record["versions"], sort_keys=True) or list(image.directories) != list(record["directories"]):
                    raise SandboxDenied("FILE_CHANGED", "Registration inputs changed; no asset was saved")
            return record["result"]
        finally:
            self.last_render = dict(producer.last_render)
            self.producers.append({"test": index, **self.last_render, "diagnostics": diagnostics})

    def tests_finished(self):
        self.phase = "catalog"
