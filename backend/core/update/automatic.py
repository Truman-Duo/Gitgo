"""Installation-owned update preferences and bounded background preparation.

Preferences authorize automation, never a package: every download and handoff
still traverses the existing signed selection, preparation and transaction path.
"""
from datetime import datetime, timezone
import threading
import uuid

from .persistence import atomic_control, read_control
from .validation import fields, reject, UpdateError

INTERVAL_SECONDS = 6 * 3600
RETRY_SECONDS = 15 * 60


def preferences(store):
    store._guard()
    path = store.root / "control/update-preferences.json"
    if not path.exists() and not path.is_symlink():
        from .version import Version
        channel = "preview" if Version.parse(store._ledger()["current"]["version"]).prerelease else "stable"
        return {"schema_version": 1, "revision": 0, "channel": channel,
                "automatic_check": True, "automatic_download": True, "automatic_install": True}
    value = fields(read_control(path), {"schema_version", "revision", "channel",
        "automatic_check", "automatic_download", "automatic_install"})
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["revision"]) is not int or not 0 <= value["revision"] <= 2**53 - 1
            or value["channel"] not in ("stable", "preview")
            or any(type(value[key]) is not bool for key in
                   ("automatic_check", "automatic_download", "automatic_install"))):
        reject("UPDATE_PREFERENCES_INVALID", "Update preferences are invalid; automation stopped")
    return value


def configure(service, channel, automatic_check, automatic_download, automatic_install, revision):
    from .preparation_service import _channel
    _channel(channel)
    if (type(revision) is not int or revision < 0 or revision >= 2**53 - 1
            or any(type(v) is not bool for v in (automatic_check, automatic_download, automatic_install))):
        reject("UPDATE_PREFERENCES_INVALID", "Expected exact update preferences and revision")
    store = service._store_loader()
    if store is None:
        reject("UPDATE_INSTALL_UNMANAGED", "Automatic updates require a managed installation")
    with service._preparation(store, allow_pending=True):
        before = preferences(store)
        if before["revision"] != revision:
            reject("UPDATE_SELECTION_STALE", "Update settings changed; refresh before editing")
        value = {"schema_version": 1, "revision": revision + 1, "channel": channel,
                 "automatic_check": automatic_check, "automatic_download": automatic_download,
                 "automatic_install": automatic_install}
        atomic_control(store.root / "control/update-preferences.json", value)
    with service._job_lock:
        job_id = getattr(service, "_automatic_job_id", None)
        if job_id in service._jobs: service._jobs[job_id].set()
    service._automatic_wake.set()
    return service.status(channel)


def observation(store, *, settings=None):
    path = store.root / "control/update-automatic.json"
    if not path.exists() and not path.is_symlink():
        return {"state": "not_checked", "last_attempt": None, "next_attempt": None, "error_code": None, "channel": None, "preferences_revision": None}
    value = fields(read_control(path), {"state", "last_attempt", "next_attempt", "error_code", "channel", "preferences_revision"})
    if (value["channel"] not in ("stable", "preview")
            or type(value["preferences_revision"]) is not int or value["preferences_revision"] < 0
            or value["state"] not in ("prepared", "up_to_date", "available", "blocked", "error")
            or any(type(value[k]) not in (int, float) or not 0 <= value[k] < 1e12
                   for k in ("last_attempt", "next_attempt"))
            or (value["error_code"] is not None and
                (type(value["error_code"]) is not str or not value["error_code"].startswith("UPDATE_")
                 or len(value["error_code"]) > 64 or not value["error_code"].replace("_", "").isalnum()))):
        reject("UPDATE_PREFERENCES_INVALID", "Automatic update observation is invalid")
    if settings is not None and (value["channel"] != settings["channel"]
                                  or value["preferences_revision"] != settings["revision"]):
        # Keep historical evidence on disk, but never describe an old channel
        # or preference revision as the currently enabled automation attempt.
        return {"state": "not_checked", "last_attempt": None, "next_attempt": None,
                "error_code": None, "channel": None, "preferences_revision": None}
    return value


def tick(service):
    """One non-overlapping attempt; no program execution and no task cancellation."""
    if not service._automatic_lock.acquire(blocking=False):
        return
    try:
        store = service._store_loader()
        if store is None or service._automatic_stop.is_set():
            return
        settings = preferences(store)
        if not settings["automatic_check"]:
            return
        from .release_policy import installed_release_policy
        policy = installed_release_policy(store)
        if policy is None or settings["channel"] not in policy.channels:
            return
        now = int(service._clock().timestamp())
        previous = observation(store)
        # Setting changes may request an earlier attempt, but never an unbounded
        # retry loop. The persisted deadline also bounds multiple Host instances.
        if previous["channel"] == settings["channel"] and previous["preferences_revision"] == settings["revision"] and previous["next_attempt"] is not None and now < previous["next_attempt"]:
            return
        result_state, error_code = "blocked", None
        try:
            # Reserve a deadline under the canonical preparation lock so another
            # Host cannot enqueue a duplicate transfer after ours releases it.
            with service._preparation(store):
                latest = observation(store)
                if latest["channel"] == settings["channel"] and latest["preferences_revision"] == settings["revision"] and latest["next_attempt"] is not None and now < latest["next_attempt"]:
                    return
                atomic_control(store.root / "control/update-automatic.json",
                    {"state": "blocked", "last_attempt": now, "next_attempt": now + RETRY_SECONDS, "error_code": None, "channel": settings["channel"], "preferences_revision": settings["revision"]})
            service._automatic_job_id = uuid.uuid4().hex
            result = service.check(settings["channel"], service._automatic_job_id)
            if preferences(store) != settings or service._automatic_stop.is_set():
                return
            if result.get("can_download") and settings["automatic_download"]:
                service._automatic_job_id = uuid.uuid4().hex
                if preferences(store) != settings: return
                result = service.download(settings["channel"], result["candidate"]["candidate_id"], service._automatic_job_id)
            result_state = "prepared" if result.get("status") == "downloaded" else result.get("status", "blocked")
            if result_state not in ("prepared", "up_to_date", "available"):
                result_state = "blocked"
        except UpdateError as exc:
            error_code, result_state = exc.code, "error"
        except Exception:
            error_code, result_state = "UPDATE_PREPARATION_FAILED", "error"
        if not service._automatic_stop.is_set() and preferences(store) == settings:
            with service._preparation(store, allow_pending=True):
                atomic_control(store.root / "control/update-automatic.json",
                    {"state": result_state, "last_attempt": now,
                     "next_attempt": now + (RETRY_SECONDS if error_code else INTERVAL_SECONDS),
                     "error_code": error_code, "channel": settings["channel"], "preferences_revision": settings["revision"]})
    finally:
        service._automatic_job_id = None
        service._automatic_lock.release()


def start(service):
    if service._automatic_thread is not None:
        return
    def run():
        while not service._automatic_stop.is_set():
            try:
                tick(service)
            except Exception:
                # Never leak proxy details or turn updater failure into Host
                # shutdown. status() reports invalid local state explicitly.
                pass
            service._automatic_wake.wait(60)
            service._automatic_wake.clear()
    service._automatic_thread = threading.Thread(target=run, name="update-preparation", daemon=True)
    service._automatic_thread.start()


def installation_supported(store):
    # Older immutable launchers cannot acquire capabilities by updating active/.
    from .bootstrap_contract import AUTOMATIC_EXIT_APIS
    return store.registration.get("bootstrap_api") in AUTOMATIC_EXIT_APIS


def candidate_blocker(store, candidate_id):
    """A terminal rollback already records the exact rejected manifest.

    Reuse that authority: ordinary exits must not undo an explicit rollback or
    retry a health-rejected release. Manual exact confirmation remains possible.
    """
    transaction = store._ledger().get("transaction")
    if (transaction and transaction["state"] == "ROLLED_BACK"
            and transaction["watermark"]["manifest_sha256"] == candidate_id):
        return "UPDATE_CANDIDATE_ROLLED_BACK"
    return None


def queue_on_clean_exit(service, nonce):
    """Stable launcher only, after its child tree has finished normally."""
    store = service._store_loader()
    if store is None:
        return None
    settings = preferences(store)
    if not settings["automatic_install"] or not installation_supported(store):
        return None
    status = service.status(settings["channel"])
    if status.get("status") != "downloaded":
        return None
    candidate_id = status["candidate"]["candidate_id"]
    if candidate_blocker(store, candidate_id):
        return None
    with service._job(uuid.uuid4().hex) as cancelled:
        store, policy = service._required(settings["channel"])
        with service._preparation(store):
            if preferences(store) != settings:
                return None
            from .handoff import queue_request
            return queue_request(service, store, policy, settings["channel"], candidate_id,
                                 cancelled, session_nonce=nonce, automatic_revision=settings["revision"])
