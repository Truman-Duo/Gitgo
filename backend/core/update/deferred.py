"""macOS reboot-bound intent, not release or active-installation authority.

The stable bootstrap alone publishes a confirmed exit request. A different
kernel boot session is required before one consumption attempt. Release bytes,
current identity and leases are rechecked by the existing transaction driver.
No reboot is initiated, no process is sampled/killed, and no user data migrates.
"""
import ctypes
import hashlib
import sys
import uuid

from .persistence import atomic_control, read_control, sync_directory, lock_file
from .tree import encode
from .validation import fields, reject, text, UpdateError
from .version import Version
from .transaction import TransactionResult


def boot_session():
    """Read the kernel's random boot UUID; never use time, files or env hints."""
    if sys.platform != "darwin":
        reject("UPDATE_REBOOT_ID_UNAVAILABLE", "Kernel boot identity is unavailable")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.sysctlbyname
        call.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        call.restype = ctypes.c_int
        data = ctypes.create_string_buffer(64)
        size = ctypes.c_size_t(len(data))
        if call(b"kern.bootsessionuuid", data, ctypes.byref(size), None, 0) != 0 or size.value != 37:
            raise ValueError("Boot UUID read failed")
        value = data.raw[:36].decode("ascii")
        canonical = str(uuid.UUID(value))
        if value.lower() != canonical or canonical == "00000000-0000-0000-0000-000000000000":
            raise ValueError("Invalid boot UUID")
        return canonical
    except (OSError, AttributeError, ValueError, UnicodeError):
        reject("UPDATE_REBOOT_ID_UNAVAILABLE", "Kernel boot identity is unavailable")


def _pending_path(store): return store.root / "control/preparation/deferred.json"
def _claimed_path(store): return store.root / "control/preparation/deferred-claimed.json"


def _validate(store, value):
    fields(value, {"schema_version", "boot_session", "request", "version"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        reject("UPDATE_DEFERRED_INVALID", "Deferred request format is unavailable")
    text(value["boot_session"], r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", 36)
    if value["boot_session"] == "00000000-0000-0000-0000-000000000000":
        reject("UPDATE_DEFERRED_INVALID", "Deferred boot identity is invalid")
    Version.parse(value["version"])
    from .handoff import _validate_request
    _validate_request(store, value["request"])
    return value


def _read(store):
    path = _pending_path(store)
    if not path.exists() and not path.is_symlink(): return None
    return _validate(store, read_control(path))


def _request_id(value): return hashlib.sha256(encode(value)).hexdigest()


def pending_status(store):
    """Offline projection only. Exposes no boot UUID, nonce or local paths."""
    value = _read(store)
    if value is None: return None
    return {"request_id": _request_id(value), "action": value["request"].get("action", "apply"),
            "version": value["version"]}


def _read_claim(store):
    path = _claimed_path(store)
    if not path.exists() and not path.is_symlink(): return None
    value = fields(read_control(path), {"schema_version", "installation_id", "request_id"})
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["installation_id"] != store.registration["installation_id"]):
        reject("UPDATE_DEFERRED_INVALID", "Deferred attempt identity is invalid")
    text(value["request_id"], r"[0-9a-f]{64}", 64)
    return value


def publish(store, request):
    """Called only after stable-launcher exit 75 and under preparation lock."""
    store._guard()
    if sys.platform != "darwin" or store.registration["platform"] != "macos" or store.registration.get("bootstrap_api") != 6:
        reject("UPDATE_SCOPE_UNAVAILABLE", "Reboot-bound replacement requires the macOS bootstrap")
    from .handoff import _perform_request
    version = _perform_request(store, request, verify_only=True)
    value = _validate(store, {"schema_version": 1, "boot_session": boot_session(),
                             "request": request, "version": version})
    existing = _read(store)
    if existing is not None and existing != value:
        reject("UPDATE_DEFERRED_EXISTS", "Cancel the pending restart request before confirming another")
    _read_claim(store)  # Do not overwrite an invalid/foreign diagnostic entry.
    if existing is None: atomic_control(_pending_path(store), value)
    return TransactionResult("restart_required", version=version)


def cancel_pending(store, request_id, cancelled):
    """Caller owns preparation lock/shared lease; exact reviewed intent only."""
    text(request_id, r"[0-9a-f]{64}", 64)
    value = _read(store)
    if value is None or _request_id(value) != request_id:
        reject("UPDATE_SELECTION_STALE", "The pending restart request changed")
    if cancelled(): reject("UPDATE_CANCELLED", "Pending cancellation was cancelled")
    store._guard()
    if _read(store) != value: reject("UPDATE_SELECTION_STALE", "The pending restart request changed")
    _pending_path(store).unlink(); sync_directory(_pending_path(store).parent)
    return {"schema_version": 1, "status": "cancelled", "request_id": request_id}


def consume_after_reboot(store):
    """Only the physical frozen bootstrap may attempt a scheduled replacement."""
    from pathlib import Path
    from .installation import physical_installation
    if (sys.platform != "darwin" or store.registration["platform"] != "macos" or store.registration.get("bootstrap_api") != 6
            or not getattr(sys, "frozen", False) or physical_installation(Path(sys.executable)) != (store.root, "bootstrap")):
        reject("UPDATE_BOOTSTRAP_ROLE", "Deferred application requires the fixed installed bootstrap")
    return _consume_after_reboot(store)


def _consume_after_reboot(store):
    from backend.core.installation_lease import runtime_descriptors
    if runtime_descriptors():
        return TransactionResult("blocked", error_code="UPDATE_SCOPE_UNAVAILABLE")
    try:
        with lock_file(store.root / "control/preparation.lock",
                expected_identity=store.registration["lock_identities"]["preparation.lock"]):
            store._guard()
            value = _read(store)
            if value is None: return None
            if boot_session() == value["boot_session"]:
                return TransactionResult("restart_required", version=value["version"])
            # Claim once BEFORE running any transaction. If the process dies
            # here, do not replay intent on another launch. Sole ledger recovery
            # handles a transaction that actually began. Otherwise old stays.
            request_id = _request_id(value)
            claimed = _read_claim(store)
            repeated = claimed is not None and claimed["request_id"] == request_id
            if not repeated:
                atomic_control(_claimed_path(store), {"schema_version": 1,
                    "installation_id": store.registration["installation_id"], "request_id": request_id})
            if _read(store) != value: reject("UPDATE_SELECTION_STALE", "Deferred request changed before consumption")
            _pending_path(store).unlink(); sync_directory(_pending_path(store).parent)
            if repeated:
                return TransactionResult("blocked", error_code="UPDATE_DEFERRED_CONSUMED")
            from .handoff import _perform_request
            if _perform_request(store, value["request"], verify_only=True) != value["version"]:
                reject("UPDATE_SELECTION_STALE", "Deferred display identity differs from the verified selection")
            return _perform_request(store, value["request"])
    except UpdateError as error:
        return TransactionResult("blocked", error_code=error.code)
    except Exception:
        return TransactionResult("blocked", error_code="UPDATE_DEFERRED_FAILED")
