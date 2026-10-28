"""Native build lifecycle on the exact tree accepted by Publish.

Build output is sealed into private CAS before its disposable native view is
retired. Runtime records retain identities and digests, never grant authority.
"""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile

from backend.core.publish.package import MAX_ARTIFACT, MAX_BUILD_TOTAL, recipe
from backend.core.publish.state import PublishError
from backend.core.sandbox import SandboxDenied
from .publish_runtime import PublishCheckRuntime
from .publish_recovery import save
from .workspace_candidate_runtime import WorkspaceCandidateRuntime


class _BuildProducer(WorkspaceCandidateRuntime):
    def capture_preview_outputs(self, stage):
        from backend.core.sandbox_read import ReadRoot
        artifacts, total = [], 0
        with ReadRoot(stage) as acquired:
            for name in self.build_outputs:
                with acquired.open(name) as handle:
                    raw = handle.read_bytes(max_bytes=MAX_ARTIFACT)
                total += len(raw)
                if total > MAX_BUILD_TOTAL:
                    raise PublishError("PUBLISH_BUILD_LIMIT")
                artifacts.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                    "blob_ref": self.storage.put_blob(raw)})
        return artifacts


class PublishBuildRuntime(PublishCheckRuntime):
    def bind_operation(self, operation, item_id, repository):
        key = super().bind_operation(operation, item_id, repository)
        if operation.get("kind") == "package":
            self.ctx = replace(self.ctx, cancellation=_BuildCancellation(self.ctx.cancellation, repository, item_id))
        return key

    def build_tree(self, image, declared):
        from backend.core.publish.environment import snapshot
        declared = recipe(declared)
        stages = (self.storage.paths.project_root / "publish-inputs").resolve()
        if stages.is_relative_to(Path(self.workspace).resolve()):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Build input overlaps workspace")
        stages.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="build-", dir=stages)).resolve()
        producer, diagnostics = None, {}
        try:
            environment = snapshot()
            self.record.update(source_stage=str(stage), state="running")
            save(self.storage, self.runtime_key, self.record)
            image.materialize(stage)
            context = replace(self.ctx, workspace_path=str(stage), storage=self.storage)
            producer = _BuildProducer(self.pipeline, context, self.registration, self.owner, self.tool)
            producer.build_outputs = declared["outputs"]
            producer.lifecycle = self.lifecycle
            observed = producer.preview("run_publish_build", {"recipe": declared}, declared["timeout"],
                trusted_arguments={}, copy_inputs=True, diagnostics=diagnostics, input_names=tuple(name for name, _, _ in image.files))
            if observed.get("input_versions_after") != {name: hashlib.sha256(raw).hexdigest() for name, raw, _ in image.files}:
                raise PublishError("PUBLISH_BUILD_SOURCE_CHANGED")
            if self.ctx.is_cancelled() or not self.is_current():
                raise PublishError("PUBLISH_BUILD_CANCELLED")
            if snapshot()["digest"] != environment["digest"]:
                raise PublishError("PUBLISH_ENVIRONMENT_CHANGED")
            result = observed.get("result", {})
            if not isinstance(result, dict) or result.get("passed") is not True or result.get("error"):
                raise PublishError("PUBLISH_BUILD_FAILED")
            artifacts = observed.get("preview_outputs")
            if (result.get("outputs") != declared["outputs"] or not isinstance(artifacts, list)
                    or [row.get("path") for row in artifacts] != declared["outputs"]):
                raise PublishError("PUBLISH_BUILD_RESULT_INVALID")
            result = {"passed": True, "artifacts": artifacts, "environment": environment,
                      "output_tail": str(result.get("output_tail", ""))[-8000:]}
        except (OSError, RuntimeError, ValueError, SandboxDenied) as exc:
            code = getattr(exc, "code", "PUBLISH_BUILD_FAILED")
            if code == "TOOL_CANCELLED":
                code = "PUBLISH_BUILD_CANCELLED"
            result = {"passed": False, "error": code, "cleanup_pending": code == "SANDBOX_CLEANUP_PENDING"}
        finally:
            if producer is not None:
                self.last_render = producer.last_render
                self.producers.append({"role": "release_build", **producer.last_render, "diagnostics": diagnostics})
            WorkspaceCandidateRuntime._discard_stage(stage, stages)
        self.record.update(state="finished", result=result, native_producers=self.producers)
        save(self.storage, self.runtime_key, self.record)
        return result


class _BuildCancellation:
    def __init__(self, parent, repository, item_id):
        self.parent, self.repository, self.item_id = parent, repository, item_id

    def is_set(self):
        if self.parent.is_set():
            return True
        ledger, _ = self.repository.load()
        return bool(ledger["items"][self.item_id].get("distribution_operation", {}).get("cancel_requested"))
