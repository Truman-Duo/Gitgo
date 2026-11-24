"""Stable bootstrap dispatch to an admitted, replaceable updater generation.

The immutable launcher retains process-cleanup authority and release trust.
The worker comes from the exact admitted application tree, copied out of the
active slot so its image cannot block replacement. Recovery uses that SAME
retained worker; it never replays a queued UI request after interruption.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .persistence import atomic_control, filesystem_identity, lock_file, read_control, pinned_registration
from contextlib import ExitStack
from .staging import _staging_parent
from .tree import tree_digest, sync_tree
from .transaction import TransactionResult
from .validation import fields, reject, text

TICKET = "engine-request.json"


def result_wire(result):
    """Keep the immutable API 10/11 receiver contract, including nulls.

    Transaction diagnostics belong to the local transaction evidence. Adding a
    dataclass field must never implicitly extend an installed launcher's wire.
    """
    return {name: getattr(result, name) for name in ("status", "version", "error_code")}


def _read(store):
    value = read_control(store.root / "control" / TICKET)
    version = value.get("schema_version") if type(value) is dict else None
    fields(value, {"schema_version", "installation_id", "nonce", "engine", "tree_sha256", "exit_code", "cleanup_complete", "automatic", "deferred"}
           | ({"cleanup"} if version == 2 else set()))
    if (type(version) is not int or version not in (1, 2)
            or value["installation_id"] != store.registration["installation_id"]
            or type(value["exit_code"]) is not int or value["exit_code"] not in (0, 75)
            or type(value["cleanup_complete"]) is not bool or type(value["automatic"]) is not bool
            or type(value["deferred"]) is not bool):
        reject("UPDATE_ENGINE_INVALID", "Updater dispatch ticket is invalid")
    from .handoff import strategy, exit_scope_ready
    if version == 2:
        from .bootstrap_contract import MACOS_PROBE_BROKER_API
        if (store.registration.get("bootstrap_api") != MACOS_PROBE_BROKER_API
                or not exit_scope_ready(store, value["cleanup"])
                or value["cleanup_complete"] != value["cleanup"]["complete"]):
            reject("UPDATE_ENGINE_INVALID", "Updater cleanup capability is invalid")
    elif not value["cleanup_complete"] and strategy(store) != "after_reboot":
        reject("UPDATE_ENGINE_INVALID", "Immediate updates require complete application cleanup")
    text(value["nonce"], r"[0-9a-f]{32}", 32)
    text(value["tree_sha256"], r"[0-9a-f]{64}", 64)
    ledger = store._ledger()
    admitted = [ledger["current"]]
    if ledger["transaction"] is not None:
        admitted.extend([ledger["transaction"]["old"], ledger["transaction"]["new"]])
    if value["tree_sha256"] not in {item["tree_sha256"] for item in admitted}:
        reject("UPDATE_ENGINE_INVALID", "Updater generation is not bound to the installation ledger")
    if value["engine"] != "engines/" + value["tree_sha256"]:
        reject("UPDATE_ENGINE_INVALID", "Updater generation is not in its fixed slot")
    cache = store.root / "control" / value["engine"]
    _staging_parent(cache, store.protected_paths)
    if tree_digest(cache) != value["tree_sha256"]:
        reject("UPDATE_ENGINE_INVALID", "Retained updater generation changed")
    return value, cache


def _lock(store):
    path = store.root / "control/engine.lock"
    if not path.exists() and not path.is_symlink():
        try:
            if os.name == "nt":
                from .windows_storage import open_file
                descriptor = open_file(path, create=True, write=True)
            else: descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        except FileExistsError: pass
    return lock_file(path, expected_identity=filesystem_identity(path))


def _environment(profile):
    from .health import isolated_environment
    from backend.core.config import ConfigManager
    from backend.core.llm_config import LLMConfigManager
    from backend.core.storage.paths import _default_state_home
    env = isolated_environment(profile)
    env.update(GITGO_CONFIG_PATH=str(ConfigManager.default_path()), GITGO_STATE_HOME=str(_default_state_home()),
               GITGO_LLM_CONFIG_PATH=str(LLMConfigManager._config_path().resolve()),
               GITGO_LLM_SECRET_PATH=str(LLMConfigManager._secret_path().resolve()))
    return env


def _invoke(store, program, arguments):
    from backend.core.process_control import spawn_managed, terminate_tree, attach_kill_job, close_job
    from backend.core.process_output import collect_output
    from .health import cleanup_report
    from .validation import json_object
    proc, job = None, None
    with tempfile.TemporaryDirectory(prefix="engine-profile-", dir=store.root / "control") as raw, ExitStack() as scope:
        profile = Path(raw).resolve()
        try:
            command = [str(program), "--gitgo-internal-role", "update-coordinator", "--root", str(store.root), *arguments]
            from .bootstrap_contract import MACOS_PROBE_BROKER_API
            broker = None
            options = {}
            if sys.platform == "darwin" and store.registration.get("bootstrap_api") == MACOS_PROBE_BROKER_API:
                from .probe_broker import ProbeServer
                broker = scope.enter_context(ProbeServer(store))
                descriptor = broker.child.fileno()
                command.extend(["--probe-fd", str(descriptor)])
                options["pass_fds"] = (descriptor,)
            proc = spawn_managed(command, cwd=str(profile), env=_environment(profile), stdin=-3,
                stdout=-1, stderr=-1, text=True, encoding="utf-8", errors="strict", macos_forkless=sys.platform == "darwin", **options)
            if broker is not None:
                broker.child.close()
            job = attach_kill_job(proc)
            collect_options = {"on_poll": broker.poll} if broker is not None else {}
            output, _, _ = collect_output(proc, timeout=600, limit=65536, reject_overflow=True,
                                          terminate=lambda: terminate_tree(proc, job), **collect_options)
            if (proc.returncode != 0 or cleanup_report(proc, job).get("complete") is not True
                    or (broker is not None and broker.cleanup_complete is not True)):
                reject("UPDATE_ENGINE_FAILED", "Updater process or cleanup failed; recovery record retained")
            result = fields(json_object(output.encode("utf-8"), 65536), {"status", "version", "error_code"})
            if result["status"] not in {"idle", "exit_required", "disabled", "up_to_date", "failed", "blocked", "busy", "committed", "rolled_back", "restart_required", "recovery_required"}:
                reject("UPDATE_ENGINE_FAILED", "Updater returned an unknown result")
            return TransactionResult(**result)
        except BaseException:
            if proc is not None and (proc.poll() is None or getattr(proc, "_gitgo_scope_socket", None) is not None):
                terminate_tree(proc, job)
            raise
        finally:
            close_job(job)


def _run(store, *, recovering):
    value, cache = _read(store)
    name = "gitgo-host.exe" if os.name == "nt" else "gitgo-host"
    answer = _invoke(store, cache / "internal/gitgo-host" / name, ["--recover"] if recovering else [])
    if tree_digest(cache) != value["tree_sha256"]:
        reject("UPDATE_ENGINE_INVALID", "Updater changed its own admitted code")
    if answer.status not in {"busy", "recovery_required"}:
        path = store.root / "control" / TICKET
        if read_control(path) != value: reject("UPDATE_ENGINE_INVALID", "Updater dispatch identity changed")
        path.unlink()
        # This is a verified disposable code snapshot, never a project store.
        # Scope cleanup above proves its worker/guardians have retired.
        shutil.rmtree(cache)
    return answer


@pinned_registration
def recover_if_pending(store):
    path = store.root / "control" / TICKET
    if not path.exists() and not path.is_symlink(): return None
    with _lock(store): return _run(store, recovering=True)


@pinned_registration
def dispatch(store, nonce, exit_code, cleanup, *, automatic=False, deferred=False):
    from backend.core.installation_lease import runtime_descriptors
    from .handoff import _session, strategy, exit_scope_ready
    if (runtime_descriptors() or type(cleanup) is not dict or type(cleanup.get("complete")) is not bool
            or not exit_scope_ready(store, cleanup)
            or exit_code not in (0, 75) or (automatic and exit_code != 0)):
        reject("UPDATE_SCOPE_UNAVAILABLE", "Updater dispatch requires a clean retired application")
    with _lock(store):
        if (store.root / "control" / TICKET).exists():
            reject("UPDATE_RECOVERY_REQUIRED", "Recover the previous updater generation first")
        if deferred:
            from .deferred import pending_status
            if strategy(store) != "after_reboot": reject("UPDATE_SCOPE_UNAVAILABLE", "Deferred updater requires macOS")
            if pending_status(store) is None: return None
        else: _session(store, nonce)
        if automatic:
            # Query the admitted active engine first. No update means no code
            # copy or retained binary on an ordinary quit.
            from backend.core.installation_lease import retain_descriptor
            with store.runtime_lease() as (active, descriptor), retain_descriptor(descriptor):
                program = active / "internal/gitgo-host" / ("gitgo-host.exe" if os.name == "nt" else "gitgo-host")
                proposal = _invoke(store, program, ["--propose", "--nonce", nonce])
            if proposal.status != "exit_required": return proposal
            exit_code = 75
        with store.runtime_lease() as (active, _descriptor):
            digest = store._ledger()["current"]["tree_sha256"]
            parent = store.root / "control/engines"
            if not parent.exists(): parent.mkdir(mode=0o700)
            _staging_parent(parent, store.protected_paths)
            cache = parent / digest
            if not cache.exists():
                if os.name == "nt":
                    from .windows_storage import copy_tree
                    copy_tree(active, cache)
                else: shutil.copytree(active, cache, symlinks=True)
                sync_tree(cache)
            if tree_digest(cache) != digest or tree_digest(active) != digest:
                reject("UPDATE_ENGINE_INVALID", "Updater copy differs from the admitted application")
        ticket = {"schema_version": 1,
            "installation_id": store.registration["installation_id"], "nonce": nonce,
            "engine": "engines/" + digest, "tree_sha256": digest, "exit_code": exit_code,
            "cleanup_complete": cleanup["complete"], "automatic": automatic, "deferred": deferred}
        from .bootstrap_contract import MACOS_PROBE_BROKER_API
        if store.registration.get("bootstrap_api") == MACOS_PROBE_BROKER_API:
            ticket.update(schema_version=2, cleanup=cleanup)
        atomic_control(store.root / "control" / TICKET, ticket)
        return _run(store, recovering=False)


def main(arguments):
    import argparse
    from .installation import open_managed
    from .handoff import _consume_request, HANDOFF_EXIT
    from backend.core.protocol_io import dump_protocol_json
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--propose", action="store_true")
    parser.add_argument("--nonce")
    parser.add_argument("--probe-fd", type=int)
    args = parser.parse_args(arguments)
    store = open_managed(Path(args.root))
    from .bootstrap_contract import MACOS_PROBE_BROKER_API
    needs_broker = sys.platform == "darwin" and store.registration.get("bootstrap_api") == MACOS_PROBE_BROKER_API
    if needs_broker != (args.probe_fd is not None):
        reject("UPDATE_PROBE_CHANNEL", "Updater probe capability does not match its launcher")
    from .probe_broker import engine_client
    with engine_client(args.probe_fd):
        return _execute_main(store, args)


def _execute_main(store, args):
    from .handoff import _consume_request
    from backend.core.protocol_io import dump_protocol_json
    if args.propose:
        expected = store.root / "active/internal/gitgo-host" / ("gitgo-host.exe" if os.name == "nt" else "gitgo-host")
        if args.recover or not getattr(sys, "frozen", False) or Path(sys.executable).resolve() != expected.resolve():
            reject("UPDATE_ENGINE_INVALID", "Automatic proposal requires the admitted active engine")
        from .automatic import queue_on_clean_exit
        from .preparation_service import UpdatePreparationService
        queued = queue_on_clean_exit(UpdatePreparationService(store_loader=lambda: store), args.nonce or "")
        print(dump_protocol_json(result_wire(TransactionResult("exit_required" if queued else "idle"))), flush=True)
        return 0
    value, cache = _read(store)
    expected = cache / "internal/gitgo-host" / ("gitgo-host.exe" if os.name == "nt" else "gitgo-host")
    if not getattr(sys, "frozen", False) or Path(sys.executable).resolve() != expected.resolve():
        reject("UPDATE_ENGINE_INVALID", "Updater execution image is not its retained generation")
    if args.recover:
        answer = store.recover()
    elif value["deferred"]:
        from .deferred import _consume_after_reboot
        answer = _consume_after_reboot(store) or TransactionResult("idle")
    else:
        answer = _consume_request(store, value["nonce"], value["exit_code"], value.get("cleanup", {"complete": value["cleanup_complete"]}))
    if answer is None: reject("UPDATE_ENGINE_FAILED", "Updater request produced no result")
    print(dump_protocol_json(result_wire(answer)), flush=True)
    return 0
