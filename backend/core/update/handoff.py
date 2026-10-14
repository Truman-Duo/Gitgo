"""Confirmed, session-bound handoff to the immutable installed bootstrap.

Tickets are requests, never installation/release authority. Only the launcher
which created a session may consume its request, after a deliberate exit code,
owned process cleanup and release of ALL its shared descriptors. The transaction
then re-verifies pinned trust and archive bytes under the exclusive runtime lock.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
import sys
import uuid

from .persistence import atomic_control, lock_file, read_control, sync_directory
from .validation import fields, reject, text, UpdateError

SESSION_ENV = "GITGO_UPDATE_LAUNCH_SESSION"
HANDOFF_EXIT = 75


def _path(store, nonce, kind):
    text(nonce, r"[0-9a-f]{32}", 32)
    return store.root / "control/preparation" / f"{kind}-{nonce}.json"


def _session(store, nonce):
    value = fields(read_control(_path(store, nonce, "session")),
        {"schema_version", "nonce", "installation_id", "current"})
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["nonce"] != nonce or value["installation_id"] != store.registration["installation_id"]
            or value["current"] != store._ledger()["current"]):
        reject("UPDATE_HANDOFF_STALE", "Launch session no longer matches this installation")
    return value


def platform_ready(store):
    if sys.platform == "linux" and store.registration["platform"] == "linux": return True
    if strategy(store) == "after_reboot":
        from .deferred import boot_session
        try: boot_session(); return True
        except UpdateError: return False
    return False


def strategy(store):
    # An older immutable bootstrap must not gain a capability it cannot run.
    return ("after_reboot" if sys.platform == "darwin" and store.registration["platform"] == "macos"
            and store.registration.get("bootstrap_api") == 6 else "immediate")


def request_available(store):
    if not platform_ready(store): return False
    try:
        store._guard()
        from .deferred import pending_status
        if pending_status(store) is not None: return False
        _session(store, os.environ.get(SESSION_ENV, ""))
        return True
    except (UpdateError, OSError, ValueError): return False


@contextmanager
def launch_session(store):
    """Created by the stable launcher, never selected by an RPC caller."""
    nonce = uuid.uuid4().hex
    try:
        with lock_file(store.root / "control/preparation.lock",
                       expected_identity=store.registration["lock_identities"]["preparation.lock"]):
            store._guard()
            parent = store.root / "control/preparation"
            # Hard-killed sessions are retained, never adopted on a later launch.
            with os.scandir(parent) as entries:
                if sum(1 for entry in entries if entry.name.startswith("session-")) >= 16:
                    reject("UPDATE_RETAIN_LIMIT", "Launch handoff retention requires separate maintenance")
            value = {"schema_version": 1, "nonce": nonce,
                     "installation_id": store.registration["installation_id"], "current": store._ledger()["current"]}
            path = _path(store, nonce, "session")
            atomic_control(path, value)
    except UpdateError as error:
        if error.code not in {"UPDATE_BUSY", "UPDATE_RETAIN_LIMIT"}: raise
        # Background preparation must not prevent ordinary application startup.
        # This launch has no update handoff capability; never inherit an old one.
        yield None
        return
    try:
        yield nonce
    finally:
        # Only our exact ticket; do not remove an altered/orphaned request.
        try:
            with lock_file(store.root / "control/preparation.lock",
                           expected_identity=store.registration["lock_identities"]["preparation.lock"]):
                store._guard()
                if read_control(path) == value:
                    request_path = _path(store, nonce, "request")
                    if request_path.exists() and not request_path.is_symlink():
                        request = read_control(request_path)
                        if all(request.get(key) == value[key] for key in ("nonce", "installation_id", "current")):
                            request_path.unlink()
                    path.unlink(); sync_directory(path.parent)
        except UpdateError as error:
            # Another preparation may be in flight during normal quit. Retain
            # our bounded orphan rather than turning a successful quit into a failure.
            if error.code != "UPDATE_BUSY": raise


def queue_request(service, store, configuration, channel, candidate_id, cancelled):
    """Called inside the pinned preparation lock and an ordinary shared lease."""
    if not platform_ready(store):
        reject("UPDATE_SCOPE_UNAVAILABLE", "Platform process cleanup is not complete; cannot apply updates")
    from .deferred import pending_status
    if pending_status(store) is not None:
        reject("UPDATE_DEFERRED_EXISTS", "Cancel the pending restart request first")
    nonce = os.environ.get(SESSION_ENV, "")
    session = _session(store, nonce)
    check = service._read_receipt(store, channel, "check")
    if check is None or check.get("candidate_id") != candidate_id:
        reject("UPDATE_SELECTION_STALE", "Review the exact selected download again")
    selected, _, _ = service._checked(store, configuration, channel, check)
    if selected.candidate is None: reject("UPDATE_NO_CANDIDATE", "No newer selected release")
    prepared = service._read_receipt(store, channel, "prepared")
    if prepared is None: reject("UPDATE_PREPARATION_CHANGED", "Download and verify the selected package first")
    service._prepared(store, selected.candidate, prepared)
    if cancelled(): reject("UPDATE_CANCELLED", "Update handoff was cancelled")
    # Revalidate freshness/current identity immediately before publishing intent.
    service._checked(store, configuration, channel, check)
    _session(store, nonce)
    path = _path(store, nonce, "request")
    value = {"schema_version": 1, "installation_id": session["installation_id"], "nonce": nonce,
             "current": session["current"], "channel": channel, "candidate_id": candidate_id}
    if path.exists() or path.is_symlink():
        if read_control(path) != value:
            reject("UPDATE_HANDOFF_STALE", "A different update has already been confirmed for this launch")
    else: atomic_control(path, value)
    return {"schema_version": 2, "status": "exit_required", "candidate_id": candidate_id,
            "channel": channel, "exit_code": HANDOFF_EXIT, "apply_strategy": strategy(store)}


def consume_request(store, nonce, exit_code, cleanup):
    """Stable launcher only; ordinary quit/crash never consumes a queued request."""
    from .installation import physical_installation
    from pathlib import Path
    physical = physical_installation(Path(sys.executable)) if getattr(sys, "frozen", False) else None
    if physical != (store.root, "bootstrap"):
        reject("UPDATE_BOOTSTRAP_ROLE", "Update application requires the fixed installed bootstrap")
    return _consume_request(store, nonce, exit_code, cleanup)


def queue_rollback(store, rollback_id, cancelled):
    """Offline explicit predecessor request, under preparation/shared locks."""
    if not platform_ready(store):
        reject("UPDATE_SCOPE_UNAVAILABLE", "Platform process cleanup cannot authorize rollback")
    from .deferred import pending_status
    if pending_status(store) is not None:
        reject("UPDATE_DEFERRED_EXISTS", "Cancel the pending restart request first")
    nonce = os.environ.get(SESSION_ENV, "")
    session = _session(store, nonce)
    selected = store.rollback_selection()
    if selected is None or selected["rollback_id"] != rollback_id:
        reject("UPDATE_SELECTION_STALE", "Review the retained predecessor again")
    if cancelled(): reject("UPDATE_CANCELLED", "Rollback handoff was cancelled")
    value = {"schema_version": 1, "installation_id": session["installation_id"], "nonce": nonce,
             "current": session["current"], "action": "rollback", "rollback_id": rollback_id}
    path = _path(store, nonce, "request")
    if path.exists() or path.is_symlink():
        if read_control(path) != value: reject("UPDATE_HANDOFF_STALE", "A different action was already confirmed")
    else: atomic_control(path, value)
    return {"schema_version": 2, "status": "exit_required", "action": "rollback",
            "rollback_id": rollback_id, "exit_code": HANDOFF_EXIT, "apply_strategy": strategy(store)}


def _consume_request(store, nonce, exit_code, cleanup):
    from backend.core.installation_lease import runtime_descriptors
    from .transaction import TransactionResult
    if exit_code != HANDOFF_EXIT: return None
    if (not platform_ready(store) or type(cleanup) is not dict
            or type(cleanup.get("complete")) is not bool
            or (strategy(store) == "immediate" and cleanup.get("complete") is not True)
            or runtime_descriptors()):
        return TransactionResult("blocked", error_code="UPDATE_SCOPE_UNAVAILABLE")
    try:
        # Serialize with preparation and hold it through the transaction. Never
        # inherit a shared lease: the engine itself obtains the exclusive gate.
        with lock_file(store.root / "control/preparation.lock",
                       expected_identity=store.registration["lock_identities"]["preparation.lock"]):
            store._guard()
            session = _session(store, nonce)
            path = _path(store, nonce, "request")
            request = read_control(path)
            _validate_request(store, request)
            if any(request[k] != session[k] for k in ("installation_id", "nonce", "current")):
                reject("UPDATE_HANDOFF_STALE", "Confirmed handoff does not match this launch")
            if strategy(store) == "after_reboot":
                from .deferred import publish
                result = publish(store, request)
            else:
                result = _perform_request(store, request)
            # Consume once only after a result; a crash does not resume a ticket
            # on the next launch (new nonce). Recovery uses the sole ledger.
            if read_control(path) == request:
                path.unlink(); sync_directory(path.parent)
            return result
    except UpdateError as error:
        return TransactionResult("blocked", error_code=error.code)
    except Exception:
        # No raw OS paths, network text or unexpected exceptions to the UI.
        return TransactionResult("blocked", error_code="UPDATE_HANDOFF_FAILED")


def _validate_request(store, request):
    from .transaction import _identity
    from .preparation_service import _channel
    rollback = request.get("action") == "rollback" if type(request) is dict else False
    fields(request, {"schema_version", "installation_id", "nonce", "current"} |
           ({"action", "rollback_id"} if rollback else {"channel", "candidate_id"}))
    if (type(request["schema_version"]) is not int or request["schema_version"] != 1
            or request["installation_id"] != store.registration["installation_id"]):
        reject("UPDATE_HANDOFF_STALE", "Confirmed request does not match this installation")
    text(request["nonce"], r"[0-9a-f]{32}", 32)
    _identity(request["current"])
    text(request["rollback_id" if rollback else "candidate_id"], r"[0-9a-f]{64}", 64)
    if not rollback: _channel(request["channel"])
    return request


def _perform_request(store, request, *, verify_only=False):
    """Fixed shared driver, under preparation lock; no caller-selected health."""
    from .transaction import InstallationStore
    from .health import frozen_health_check
    from .release_policy import installed_release_policy
    from .preparation_service import UpdatePreparationService, _channel
    _validate_request(store, request)
    store._guard()
    from .installation import protected_install_paths
    for protected in protected_install_paths():
        if protected == store.root or protected.is_relative_to(store.root) or store.root.is_relative_to(protected):
            reject("UPDATE_PROTECTED_PATHS", "Current profiles and projects must stay outside the managed installation")
    if request["current"] != store._ledger()["current"]:
        reject("UPDATE_HANDOFF_STALE", "Installation changed after confirmation")
    if request.get("action") == "rollback":
        selected = store.rollback_selection()
        if selected is None or selected["rollback_id"] != request["rollback_id"]:
            reject("UPDATE_SELECTION_STALE", "Retained predecessor changed after confirmation")
        if verify_only: return selected["version"]
        return store._rollback(request["rollback_id"], health_check=frozen_health_check)
    channel = _channel(request["channel"])
    configuration = installed_release_policy(store)
    if configuration is None: reject("UPDATE_TRUST_MISSING", "Publisher trust is unavailable")
    clock = lambda: datetime.now(timezone.utc)
    service = UpdatePreparationService(store_loader=lambda: store, clock=clock)
    receipt = service._read_receipt(store, channel, "check")
    if receipt is None or receipt.get("candidate_id") != request["candidate_id"]:
        reject("UPDATE_SELECTION_STALE", "Reviewed release was replaced before shutdown")
    selected, raw, detached = service._checked(store, configuration, channel, receipt)
    if selected.candidate is None: reject("UPDATE_NO_CANDIDATE", "Selected release is no longer newer")
    prepared = service._read_receipt(store, channel, "prepared")
    if prepared is None: reject("UPDATE_PREPARATION_CHANGED", "Prepared download is missing")
    service._prepared(store, selected.candidate, prepared)
    if verify_only: return selected.candidate.version
    archive = store.root / "control/preparation" / prepared["directory"] / "archive.verified"
    return InstallationStore._apply(store, configuration.channel(channel), configuration.verifier,
        raw, detached, archive, health_check=frozen_health_check, clock=clock, checkpoint=lambda _: None)
