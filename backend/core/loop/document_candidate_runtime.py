"""Task-runtime ownership of one private native document producer.

The file service receives bytes only after native cleanup and access retirement.
It neither grants ACLs nor runs third-party document libraries in the service.
"""
import os
from pathlib import Path
import shutil

from backend.core.sandbox import SandboxDenied
from backend.core.sandbox_read import ReadRoot


from .file_candidate_runtime import FileCandidateRuntime


class DocumentCandidateRuntime(FileCandidateRuntime):
    def render(self, specification, suffix):
        from backend.core.sandbox_access import WorkspaceAccess, native_runtime_access
        from .execution_contract import NativeInvocation
        from .read_approval import native_cleanup_pending, runtime_cleanup_report
        if native_cleanup_pending(self.ctx.process):
            raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "Prior native resources require inspection before another document producer")
        from .candidate_workspace import allocate_candidate_workspace
        root, stage = allocate_candidate_workspace(self.storage.paths, "document", workspace=self.workspace)
        access = None
        cleanup_ok = False
        diagnostics = {}
        metadata = {"owner": self.owner,
                    "registration": self.registration}
        self.last_render = {"producer": "native_document_render", "launch_state": "not_started", "cleanup": {"complete": True}, "access_retired": False}
        try:
            if os.name == "nt":
                runtimes, shallow = native_runtime_access()
                record = self.storage.paths.project_root / "sandbox_access" / (stage.name + ".json")
                record.parent.mkdir(parents=True, exist_ok=True)
                access = WorkspaceAccess(stage, runtimes, record, shallow_runtimes=shallow)
                access.prepare()
            args = {**specification, "path": "candidate" + suffix, "_workspace": str(stage)}
            invocation = NativeInvocation.compile("document_render", args, 120,
                owner=metadata["owner"], registration=self.registration)
            result = self.pipeline._execute_isolated("document_render", args, 120,
                cancellation_event=self.ctx.cancellation, runtime_metadata=metadata, diagnostics=diagnostics, native_invocation=invocation)
            if result.get("error"):
                raise SandboxDenied(str(result["error"]), str(result.get("error_info", {}).get("message", result.get("detail", "Document producer failed"))))
            if metadata.get("launch_state") != "started" or metadata.get("exit_code") != 0:
                raise SandboxDenied("SANDBOX_EXECUTION_FAILED", "Document producer did not confirm a successful native launch and exit")
            if metadata.get("cleanup", {}).get("complete") is not True:
                raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "Document producer cleanup is unconfirmed; no formal commit")
            with ReadRoot(stage) as acquired, acquired.open("candidate" + suffix) as opened:
                raw = opened.read_bytes(max_bytes=8 * 1024 * 1024)
        finally:
            self.last_render["diagnostics"] = diagnostics
            self.last_render.update({key: metadata[key] for key in ("launch_state", "exit_code", "execution_basis") if key in metadata})
            cleanup = runtime_cleanup_report(metadata.get("cleanup"))
            if cleanup is not None:
                self.last_render["cleanup"] = cleanup
            # Preserve the workspace and access journal if lifecycle retirement
            # is unknown. Never remove files while an unretired producer owns them.
            try:
                if metadata.get("launch_state") in {"started", "unknown"} and (cleanup or {}).get("complete") is not True:
                    self.last_render["cleanup"] = {"complete": False, "mode": "unconfirmed"}
                    raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "Producer lifecycle is unconfirmed; retain private workspace and access")
                if access is not None:
                    try:
                        access.close()
                    except Exception:
                        self.last_render["cleanup"] = {"complete": False, "mode": "access_retirement_failed"}
                        raise
                cleanup_ok = self.last_render.get("cleanup", {}).get("complete") is True
                self.last_render["access_retired"] = cleanup_ok
            finally:
                if cleanup_ok and stage.resolve().parent == root and not stage.is_symlink():
                    shutil.rmtree(stage)
        return raw
