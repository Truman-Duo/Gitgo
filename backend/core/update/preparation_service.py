"""Manual check/download application adapter. No candidate execution or switch.

Release high water lives in the existing installation ledger. The preparation
receipts contain reverified signed bytes, not a second release authority. A
separate pinned preparation lock serializes network jobs without excluding the
running Host or ordinary project children for the duration of a transfer.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import binascii
import os
from pathlib import Path
import shutil
import sys
import threading
import time

from .checker import CurrentInstallation, check_for_update
from .contracts import verify_bundle_contract
from .installation import open_managed, physical_installation
from .manifest import DataCompatibility, MAX_MANIFEST_BYTES, MAX_SIGNATURE_BYTES
from .persistence import atomic_control, filesystem_identity, lock_file, read_control, open_readonly
from .release_policy import installed_release_policy
from .staging import prepare_update, _verify_file
from .transaction import TERMINAL, _watermark, _watermark_wire
from .transport import HttpsTransport
from .tree import tree_digest, sync_tree
from .validation import fields, reject, text, UpdateError


def executing_store():
    from .installation import _ownership_supported
    if not _ownership_supported() or not getattr(sys, "frozen", False): return None
    physical = physical_installation(Path(sys.executable))
    if physical is None or physical[1] != "active": return None
    return open_managed(physical[0])


def _channel(channel):
    if type(channel) is not str or channel not in ("stable", "preview"):
        reject("UPDATE_POLICY_INVALID", "Select stable or preview explicitly")
    return channel


def _decode(encoded, maximum):
    if type(encoded) is not str or len(encoded) > ((maximum + 2) // 3) * 4:
        reject("UPDATE_CONTROL_INVALID", "Cached release evidence exceeds its limit")
    try: raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error): reject("UPDATE_CONTROL_INVALID", "Cached release evidence encoding is invalid")
    if len(raw) > maximum or base64.b64encode(raw).decode() != encoded:
        reject("UPDATE_CONTROL_INVALID", "Cached release evidence is not canonical")
    return raw


class _SelectedTransport:
    """Download exactly the release the user reviewed, not a newly fetched one."""
    def __init__(self, policy, raw, signature, transport):
        self.allowed_hosts = policy.allowed_hosts
        self.policy, self.raw, self.signature, self.transport = policy, raw, signature, transport

    def fetch_bytes(self, url, maximum, *, cancelled):
        if cancelled(): reject("UPDATE_CANCELLED", "Update preparation was cancelled")
        if url == self.policy.manifest_url: return self.raw
        if url == self.policy.signature_url: return self.signature
        reject("UPDATE_FETCH_FAILED", "Unexpected release metadata request")

    def receive(self, *args, **kwargs): return self.transport.receive(*args, **kwargs)


class UpdatePreparationService:
    def __init__(self, *, store_loader=executing_store, transport_factory=HttpsTransport,
                 clock=lambda: datetime.now(timezone.utc)):
        self._store_loader, self._transport_factory, self._clock = store_loader, transport_factory, clock
        self._job_lock = threading.Lock()
        self._jobs = {}
        self._cancelled = {}
        self._closing = False

    @contextmanager
    def _job(self, operation_id):
        text(operation_id, r"[0-9a-f]{32}", 32)
        event = threading.Event()
        with self._job_lock:
            self._cancelled = {key: stamp for key, stamp in self._cancelled.items() if time.monotonic() - stamp < 120}
            if self._closing or operation_id in self._cancelled: event.set()
            if self._jobs: reject("UPDATE_BUSY", "Another update preparation is already running")
            self._jobs[operation_id] = event
        try:
            if event.is_set(): reject("UPDATE_CANCELLED", "Update preparation was cancelled")
            yield event.is_set
        finally:
            with self._job_lock: self._jobs.pop(operation_id, None)

    def cancel(self, operation_id):
        text(operation_id, r"[0-9a-f]{32}", 32)
        with self._job_lock:
            event = self._jobs.get(operation_id)
            if event: event.set()
            # A bounded short-lived tombstone closes cancel-before-admission.
            if len(self._cancelled) >= 32: self._cancelled.pop(next(iter(self._cancelled)))
            self._cancelled[operation_id] = time.monotonic()
        return {"cancelled": True}

    def close(self):
        with self._job_lock:
            self._closing = True
            for event in self._jobs.values(): event.set()

    @contextmanager
    def _preparation(self, store, *, allow_pending=False):
        store._guard()
        with lock_file(store.root / "control/preparation.lock",
                       expected_identity=store.registration["lock_identities"]["preparation.lock"]):
            with store.runtime_lease():
                from .deferred import pending_status
                if not allow_pending and pending_status(store) is not None:
                    reject("UPDATE_DEFERRED_EXISTS", "Cancel the pending restart request first")
                yield

    def _local(self, store, channel):
        store._guard()
        ledger = store._ledger()
        transaction = ledger["transaction"]
        if transaction is not None and transaction["state"] not in TERMINAL:
            reject("UPDATE_RECOVERY_REQUIRED", "Recover the installation before preparing updates")
        local = store.registration
        current = CurrentInstallation(local["product_id"], ledger["current"]["version"], local["platform"],
            local["architecture"], local["updater_version"], DataCompatibility.parse(local["data_compatibility"]), True)
        previous = next((_watermark(mark) for mark in ledger["watermarks"] if mark["channel"] == channel), None)
        return ledger, current, previous

    def _checked(self, store, configuration, channel, receipt):
        receipt = fields(receipt, {"schema_version", "installation_id", "current", "channel", "candidate_id", "raw", "signature"})
        ledger, current, previous = self._local(store, channel)
        if (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or receipt["installation_id"] != store.registration["installation_id"]
                or receipt["channel"] != channel or receipt["current"] != ledger["current"]):
            reject("UPDATE_SELECTION_STALE", "Installation or selected release changed; check again")
        raw, signature = _decode(receipt["raw"], MAX_MANIFEST_BYTES), _decode(receipt["signature"], MAX_SIGNATURE_BYTES)
        policy = configuration.channel(channel)
        result = check_for_update(policy, current, configuration.verifier,
            lambda url, _limit: raw if url == policy.manifest_url else signature,
            now=self._clock(), previous=previous)
        if receipt["candidate_id"] != result.watermark.manifest_sha256:
            reject("UPDATE_CONTROL_INVALID", "Cached selection differs from its signed bytes")
        return result, raw, signature

    def _receipt_path(self, store, channel, kind):
        return store.root / "control/preparation" / (kind + "-" + channel + ".json")

    def _read_receipt(self, store, channel, kind):
        path = self._receipt_path(store, channel, kind)
        if not path.exists() and not path.is_symlink(): return None
        return read_control(path)

    def _prepared(self, store, manifest, value):
        fields(value, {"schema_version", "installation_id", "candidate_id", "directory", "identity", "bundle", "tree_sha256"})
        text(value["directory"], r"gitgo-update-[a-z0-9_-]{1,64}", 80)
        from .archive import _parts
        parts = _parts(value["bundle"])
        if not 1 <= len(parts) <= 2 or parts[0] != "payload":
            reject("UPDATE_PREPARATION_CHANGED", "Prepared payload must stay in its fixed layout")
        work = store.root / "control/preparation" / value["directory"]
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["installation_id"] != store.registration["installation_id"]
                or value["candidate_id"] != manifest.manifest_sha256
                or work != work.resolve(strict=True) or work.is_symlink()
                or value["identity"] != filesystem_identity(work)):
            reject("UPDATE_PREPARATION_CHANGED", "Prepared download identity changed; check and download again")
        bundle = work / value["bundle"]
        if bundle != bundle.resolve(strict=True) or not bundle.is_relative_to(work):
            reject("UPDATE_PREPARATION_CHANGED", "Prepared payload is redirected")
        fd = open_readonly(work / "archive.verified")
        with os.fdopen(fd, "rb") as archive: _verify_file(archive, manifest, lambda: False)
        verify_bundle_contract(bundle, manifest)
        if tree_digest(bundle) != value["tree_sha256"]:
            reject("UPDATE_PREPARATION_CHANGED", "Prepared payload changed")

    def status(self, channel="stable"):
        channel = _channel(channel)
        store = self._store_loader()
        if store is None:
            from .status import legacy_status
            return legacy_status()
        _, current, _ = self._local(store, channel)
        configuration = installed_release_policy(store)
        from .handoff import request_available, platform_ready, strategy
        from .deferred import pending_status
        result = {"schema_version": 5, "product_id": "gitgo-terminal", "version": current.version,
            "status": "unconfigured", "enabled": False, "automatic_check": False, "channel": channel,
            "can_check": configuration is not None and channel in configuration.channels,
            "can_download": False, "can_apply": False, "can_request_apply": False,
            "blockers": ["PLATFORM_ACCEPTANCE_REQUIRED"],
            "candidate": None, "latest_version": None,
            "rollback": store.rollback_selection(), "can_request_rollback": False,
            "apply_strategy": strategy(store), "pending_restart": pending_status(store)}
        result["can_request_rollback"] = result["rollback"] is not None and request_available(store)
        if not request_available(store): result["blockers"].append("UPDATE_HANDOFF_REQUIRED")
        if not platform_ready(store): result["blockers"].append("UPDATE_SCOPE_UNAVAILABLE")
        if not result["can_check"]:
            result["blockers"].insert(0, "RELEASE_TRUST_REQUIRED")
            return result
        result["status"] = "idle"
        receipt = self._read_receipt(store, channel, "check")
        if receipt is None: return result
        try:
            checked, _, _ = self._checked(store, configuration, channel, receipt)
            result["status"] = checked.status
            result["latest_version"] = checked.watermark.version
            manifest = checked.candidate
            if manifest is not None:
                result["candidate"] = {"candidate_id": manifest.manifest_sha256, "version": manifest.version,
                    "release_id": manifest.release_id, "bytes": manifest.bytes, "channel": channel}
                result["can_download"] = True
                prepared = self._read_receipt(store, channel, "prepared")
                if prepared is not None and prepared.get("candidate_id") == manifest.manifest_sha256:
                    self._prepared(store, manifest, prepared)
                    result["status"] = "downloaded"; result["can_download"] = False
                    result["can_request_apply"] = request_available(store)
        except (UpdateError, OSError, ValueError):
            result.update(status="stale", candidate=None, latest_version=None, can_download=False)
        return result

    def request_apply(self, channel, candidate_id, operation_id):
        text(candidate_id, r"[0-9a-f]{64}", 64)
        with self._job(operation_id) as cancelled:
            store, configuration = self._required(channel)
            with self._preparation(store):
                from .handoff import queue_request
                return queue_request(self, store, configuration, channel, candidate_id, cancelled)

    def _required(self, channel):
        channel = _channel(channel)
        store = self._store_loader()
        if store is None: reject("UPDATE_INSTALL_UNMANAGED", "Manual updates require a new managed installation")
        from .deferred import pending_status
        if pending_status(store) is not None: reject("UPDATE_DEFERRED_EXISTS", "Cancel the pending restart request first")
        configuration = installed_release_policy(store)
        if configuration is None: reject("UPDATE_TRUST_MISSING", "Publisher release addresses and public keys are not configured")
        configuration.channel(channel)
        return store, configuration

    def request_rollback(self, rollback_id, operation_id):
        text(rollback_id, r"[0-9a-f]{64}", 64)
        with self._job(operation_id) as cancelled:
            store = self._store_loader()
            if store is None: reject("UPDATE_INSTALL_UNMANAGED", "Rollback requires a managed installation")
            with self._preparation(store):
                from .handoff import queue_rollback
                return queue_rollback(store, rollback_id, cancelled)

    def cancel_pending(self, request_id, operation_id):
        with self._job(operation_id) as cancelled:
            store = self._store_loader()
            if store is None: reject("UPDATE_INSTALL_UNMANAGED", "Pending replacement requires a managed installation")
            with self._preparation(store, allow_pending=True):
                from .deferred import cancel_pending
                return cancel_pending(store, request_id, cancelled)

    def check(self, channel, operation_id):
        with self._job(operation_id) as cancelled:
            store, configuration = self._required(channel)
            with self._preparation(store):
                ledger, current, previous = self._local(store, channel)
                policy = configuration.channel(channel)
                transport = self._transport_factory(policy.allowed_hosts, deadline=60)
                raw = {}
                def fetch(url, maximum):
                    value = transport.fetch_bytes(url, maximum, cancelled=cancelled)
                    raw[url] = value
                    return value
                checked = check_for_update(policy, current, configuration.verifier, fetch,
                    now=self._clock(), previous=previous)
                checked = check_for_update(policy, current, configuration.verifier,
                    lambda url, _limit: raw[url], now=self._clock(), previous=previous)
                if cancelled(): reject("UPDATE_CANCELLED", "Update preparation was cancelled")
                receipt = {"schema_version": 1, "installation_id": store.registration["installation_id"],
                    "current": ledger["current"], "channel": channel, "candidate_id": checked.watermark.manifest_sha256,
                    "raw": base64.b64encode(raw[policy.manifest_url]).decode(),
                    "signature": base64.b64encode(raw[policy.signature_url]).decode()}
                # High water becomes durable before a displayed receipt can
                # permit download. It is never lowered by a cancelled download.
                with lock_file(store.root / "control/operation.lock", expected_identity=store.registration["lock_identities"]["operation.lock"]):
                    latest, _, _ = self._local(store, channel)
                    if latest["current"] != ledger["current"]: reject("UPDATE_SELECTION_STALE", "Installation changed during check")
                    mark = _watermark_wire(checked.watermark)
                    marks = sorted([item for item in latest["watermarks"] if item["channel"] != channel] + [mark], key=lambda item: item["channel"])
                    if latest["watermarks"] != marks:
                        latest["watermarks"] = marks; store._save(latest)
                    atomic_control(self._receipt_path(store, channel, "check"), receipt)
            return self.status(channel)

    def download(self, channel, candidate_id, operation_id):
        text(candidate_id, r"[0-9a-f]{64}", 64)
        with self._job(operation_id) as cancelled:
            store, configuration = self._required(channel)
            with self._preparation(store):
                receipt = self._read_receipt(store, channel, "check")
                if receipt is None or receipt.get("candidate_id") != candidate_id:
                    reject("UPDATE_SELECTION_STALE", "Check and review the selected release before downloading")
                checked, raw, signature = self._checked(store, configuration, channel, receipt)
                if checked.candidate is None: reject("UPDATE_NO_CANDIDATE", "No newer release is available")
                existing = self._read_receipt(store, channel, "prepared")
                if existing is not None and existing.get("candidate_id") == candidate_id:
                    self._prepared(store, checked.candidate, existing)
                    return self.status(channel)
                parent = store.root / "control/preparation"
                with os.scandir(parent) as entries:
                    for index, _ in enumerate(entries):
                        if index >= 15: reject("UPDATE_RETAIN_LIMIT", "Private preparation storage requires separate maintenance")
                _, current, previous = self._local(store, channel)
                policy = configuration.channel(channel)
                transport = _SelectedTransport(policy, raw, signature, self._transport_factory(policy.allowed_hosts))
                result = prepare_update(policy, current, configuration.verifier, staging_parent=parent,
                    protected_paths=store.protected_paths, transport=transport, previous=previous,
                    clock=self._clock, cancelled=cancelled)
                if result.status != "staged" or result.staging_directory is None:
                    reject("UPDATE_SELECTION_STALE", "Selected release is no longer available")
                work, identity, retain = result.staging_directory, filesystem_identity(result.staging_directory), False
                try:
                    if result.status != "staged" or result.candidate.manifest_sha256 != candidate_id:
                        reject("UPDATE_SELECTION_STALE", "Selected release changed during preparation")
                    verify_bundle_contract(result.bundle_directory, result.candidate)
                    if cancelled(): reject("UPDATE_CANCELLED", "Update preparation was cancelled")
                    sync_tree(work)
                    self._checked(store, configuration, channel, receipt)
                    prepared = {"schema_version": 1, "installation_id": store.registration["installation_id"],
                        "candidate_id": candidate_id, "directory": work.name, "identity": identity,
                        "bundle": str(result.bundle_directory.relative_to(work)), "tree_sha256": tree_digest(result.bundle_directory)}
                    store._guard()
                    retain = True  # Even a post-replace fsync failure retains evidence.
                    atomic_control(self._receipt_path(store, channel, "prepared"), prepared)
                finally:
                    if not retain:
                        if work.is_symlink() or filesystem_identity(work) != identity:
                            reject("UPDATE_CLEANUP_FAILED", "Private preparation identity changed; evidence retained")
                        shutil.rmtree(work)
            return self.status(channel)
