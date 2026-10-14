"""Explicit independent recovery for registered POSIX LAB installations only.

No shipping installer, startup hook, enable switch or network operation uses
this role. It cannot adopt a production installation or execute a candidate.
The existing SimulationStore ledger/locks remain the only transaction authority.
Requests contain captured LOCAL identity, never paths from release metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import uuid

from .health import MAX_REPORT_BYTES, cleanup_report, isolated_environment
from .persistence import filesystem_identity, validate_device
from .staging import _staging_parent
from .transaction import SimulationStore
from .tree import encode, tree_digest
from .validation import UpdateError, digest, fields, json_object, number, reject, text
from .version import Version


ROLE = "update-recover-simulation"
MAX_REQUEST_BYTES = 64 * 1024
RESULTS = {"idle", "committed", "rolled_back", "recovery_required", "busy"}


def _path(value: str) -> Path:
    if type(value) is not str or not value or len(value) > 4096 or "\0" in value:
        reject("UPDATE_HELPER_REQUEST_INVALID", "Invalid local recovery path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        reject("UPDATE_HELPER_REQUEST_INVALID", "Recovery paths must be explicit absolute paths")
    return path


@dataclass(frozen=True)
class RecoveryRequest:
    root: Path
    installation_id: str
    device: int | str
    inode: int
    protected_paths: tuple[Path, ...]
    nonce: str

    def wire(self) -> dict:
        return {"schema_version": 1, "root": str(self.root), "installation_id": self.installation_id,
                "device": self.device, "inode": self.inode,
                "protected_paths": [str(p) for p in self.protected_paths], "nonce": self.nonce}

    @classmethod
    def parse(cls, value: dict) -> "RecoveryRequest":
        fields(value, {"schema_version", "root", "installation_id", "device", "inode", "protected_paths", "nonce"})
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            reject("UPDATE_HELPER_REQUEST_INVALID", "Unsupported recovery request schema")
        text(value["installation_id"], r"[0-9a-f]{32}", 32)
        text(value["nonce"], r"[0-9a-f]{32}", 32)
        validate_device(value["device"])
        number(value["inode"], maximum=2**64 - 1)
        paths = value["protected_paths"]
        if (type(paths) is not list or not 1 <= len(paths) <= 256
                or any(type(p) is not str for p in paths) or len(set(paths)) != len(paths)):
            reject("UPDATE_HELPER_REQUEST_INVALID", "Explicit bounded protected paths are required")
        return cls(_path(value["root"]), value["installation_id"], value["device"], value["inode"],
                   tuple(_path(p) for p in paths), value["nonce"])

    @classmethod
    def capture(cls, store: SimulationStore) -> "RecoveryRequest":
        store._guard()
        local = store.registration
        return cls.parse({"schema_version": 1, "root": str(store.root),
                          "installation_id": local["installation_id"], "device": local["device"],
                          "inode": local["inode"], "protected_paths": local["protected_paths"],
                          "nonce": uuid.uuid4().hex})


def recover_request(request: RecoveryRequest) -> dict:
    """Reopen captured authority and recover; no trust in an earlier store object."""
    if os.name != "posix":
        reject("UPDATE_TRANSACTION_PLATFORM", "Recovery ownership adapter is unavailable")
    try:
        return _recover_request(request)
    except UpdateError:
        raise
    except Exception:
        reject("UPDATE_CONTROL_INVALID", "Recovery authority could not be validated")


def _recover_request(request: RecoveryRequest) -> dict:
    request = RecoveryRequest.parse(request.wire())
    _staging_parent(request.root, request.protected_paths)
    if filesystem_identity(request.root) != {"device": request.device, "inode": request.inode}:
        reject("UPDATE_CONTROL_INVALID", "Recovery root differs from captured identity")
    store = SimulationStore(request.root, protected_paths=request.protected_paths)
    if store.registration["installation_id"] != request.installation_id:
        reject("UPDATE_CONTROL_INVALID", "Recovery registration differs from captured identity")
    result = store.recover()
    return {"schema_version": 1, "nonce": request.nonce, "installation_id": request.installation_id,
            "status": result.status, "version": result.version, "error_code": result.error_code}


def helper_main(argv: list[str], *, stdin=None, stdout=None) -> int:
    """Bounded stdin request/JSON result; never echo paths or arbitrary errors."""
    import sys
    from backend.core.protocol_io import dump_protocol_json
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        if argv:
            reject("UPDATE_HELPER_REQUEST_INVALID", "Recovery role accepts only a stdin request")
        request = RecoveryRequest.parse(json_object(stdin.read(MAX_REQUEST_BYTES + 1), MAX_REQUEST_BYTES))
        result = recover_request(request)
    except Exception:
        print(dump_protocol_json({"schema_version": 1, "error_code": "UPDATE_HELPER_REQUEST_INVALID"}),
              file=stdout, flush=True)
        return 2
    print(dump_protocol_json(result), file=stdout, flush=True)
    return 0


def recover_in_helper(host: Path, expected_helper_tree: str, request: RecoveryRequest,
                      profile: Path, *, timeout: int = 30) -> dict:
    """Explicit lab coordinator using a pinned, stable frozen Host outside root.

    Parent-owned cleanup is returned separately. complete=False is not upgraded
    to success evidence and can never authorize a production update commitment.
    """
    if os.name != "posix":
        reject("UPDATE_TRANSACTION_PLATFORM", "Recovery ownership adapter is unavailable")
    try:
        return _recover_in_helper(host, expected_helper_tree, request, profile, timeout=timeout)
    except UpdateError:
        raise
    except Exception:
        reject("UPDATE_HELPER_INVALID", "Recovery helper inputs could not be validated")


def _recover_in_helper(host, expected_helper_tree, request, profile, *, timeout):
    number(timeout, minimum=1, maximum=120)
    digest(expected_helper_tree)
    request = RecoveryRequest.parse(request.wire())
    wire = encode(request.wire())
    if len(wire) > MAX_REQUEST_BYTES:
        reject("UPDATE_HELPER_REQUEST_INVALID", "Recovery request exceeds its size limit")
    _staging_parent(request.root, request.protected_paths)
    if (not isinstance(host, Path) or not host.is_absolute() or host != host.resolve(strict=True)
            or host.name != "gitgo-host" or host.is_symlink() or not host.is_file()
            or not os.access(host, os.X_OK) or host.is_relative_to(request.root)):
        reject("UPDATE_HELPER_INVALID", "Recovery requires a stable frozen Host outside the installation")
    _staging_parent(profile, (request.root, host.parent, *request.protected_paths))
    if any(profile.iterdir()):
        reject("UPDATE_HELPER_INVALID", "Recovery profile must be empty")
    _staging_parent(host.parent, (request.root, *request.protected_paths))
    if tree_digest(host.parent) != expected_helper_tree:
        reject("UPDATE_HELPER_INVALID", "Recovery Host bundle differs from local pinned identity")
    from backend.core.process_control import spawn_managed, terminate_tree, attach_kill_job, close_job
    from backend.core.process_output import collect_output
    proc, job = None, None
    try:
        proc = spawn_managed([str(host), "--gitgo-internal-role", ROLE], cwd=str(profile),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="strict", env=isolated_environment(profile))
        job = attach_kill_job(proc)
        output, _, _ = collect_output(proc, timeout=timeout, limit=MAX_REPORT_BYTES, input=wire.decode("ascii"),
                                     reject_overflow=True, terminate=lambda: terminate_tree(proc, job))
        if proc.returncode != 0:
            reject("UPDATE_HELPER_FAILED", "Recovery helper exited unsuccessfully")
        result = json_object(output.encode("utf-8"), MAX_REPORT_BYTES)
        fields(result, {"schema_version", "nonce", "installation_id", "status", "version", "error_code"})
        if (type(result["schema_version"]) is not int or result["schema_version"] != 1
                or result["nonce"] != request.nonce or result["installation_id"] != request.installation_id
                or result["status"] not in RESULTS):
            reject("UPDATE_HELPER_FAILED", "Recovery helper report differs from captured request")
        if result["version"] is not None:
            Version.parse(result["version"])
        if result["error_code"] is not None:
            text(result["error_code"], r"UPDATE_[A-Z_]+", 128)
        if result["status"] in {"busy", "recovery_required"}:
            expected_error = "UPDATE_BUSY" if result["status"] == "busy" else "UPDATE_RECOVERY_REQUIRED"
            if result["version"] is not None or result["error_code"] != expected_error:
                reject("UPDATE_HELPER_FAILED", "Recovery failure report is inconsistent")
        elif result["status"] in {"idle", "committed"} and result["error_code"] is not None:
            reject("UPDATE_HELPER_FAILED", "Successful recovery cannot report a failure")
        # A child report alone is not proof that the durable state is usable.
        if tree_digest(host.parent) != expected_helper_tree:
            reject("UPDATE_HELPER_INVALID", "Recovery helper bundle changed during execution")
        local = SimulationStore(request.root, protected_paths=request.protected_paths)
        if (filesystem_identity(request.root) != {"device": request.device, "inode": request.inode}
                or local.registration["installation_id"] != request.installation_id):
            reject("UPDATE_CONTROL_INVALID", "Recovery authority changed during execution")
        if result["status"] in {"idle", "committed", "rolled_back"}:
            with local.runtime_session():
                ledger = local._ledger()
                transaction = ledger["transaction"]
                observed_status = "idle" if transaction is None else {
                    "COMMITTED": "committed", "ROLLED_BACK": "rolled_back",
                }.get(transaction["state"])
                if (ledger["current"]["version"] != result["version"] or observed_status != result["status"]
                        or (transaction is not None and transaction["failure_code"] != result["error_code"])):
                    reject("UPDATE_HELPER_FAILED", "Recovery report differs from admitted current version")
        return {"recovery": result, "cleanup": cleanup_report(proc, job)}
    except Exception:
        if proc is not None and (proc.poll() is None or getattr(proc, "_gitgo_scope_socket", None) is not None):
            terminate_tree(proc, job)
        reject("UPDATE_HELPER_FAILED", "Recovery helper failed or exceeded its bounds; inspect local ledger")
    finally:
        close_job(job)
