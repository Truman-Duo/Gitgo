"""Shared POSIX transaction engine with distinct, validated installation modes.

Concrete modes provision only initially empty private roots and validate their
own registration schema. SimulationStore remains lab-only; the opt-in managed
installation adapter adds its bootstrap and admission, with activation closed.
The sole ledger commits current identity and replay evidence in one record.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import uuid

from .archive import ExtractionLimits, _extract_archive
from .checker import CurrentInstallation, ReplayState, UpdatePolicy, check_for_update
from .contracts import verify_bundle_contract
from .manifest import DataCompatibility, ManifestVerifier
from .persistence import atomic_control, filesystem_identity, lock_file, read_control, sync_directory, validate_device
from .staging import _staging_parent, _verify_file
from .tree import sync_tree, tree_digest
from .validation import UpdateError, digest, fields, json_object, number, reject, text, timestamp
from .version import Version


TERMINAL = {"COMMITTED", "ROLLED_BACK"}
STATES = {"PREPARED", "SAVE_OLD", "PUBLISH_NEW", "CHECK_NEW", "ROLLBACK"} | TERMINAL


@dataclass(frozen=True)
class HealthReport:
    product_id: str
    version: str
    platform: str
    architecture: str
    source_commit: str
    source_tree: str
    passed: bool


@dataclass(frozen=True)
class TransactionResult:
    status: str
    version: str | None = None
    error_code: str | None = None


def _compat_wire(value: DataCompatibility) -> dict:
    result = asdict(value)
    for name in ("state_migrations", "observability_migrations"):
        result[name] = [{"version": version, "sha256": sha} for version, sha in getattr(value, name)]
    return result


def _watermark_wire(value: ReplayState) -> dict:
    result = asdict(value)
    result["published_at"] = value.published_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return result


def _watermark(value: dict) -> ReplayState:
    fields(value, {"product_id", "channel", "platform", "architecture", "sequence", "manifest_sha256",
                   "artifact_sha256", "version", "published_at"})
    return ReplayState(**{**value, "published_at": timestamp(value["published_at"])})


def _identity(value):
    fields(value, {"version", "tree_sha256"})
    Version.parse(value["version"])
    digest(value["tree_sha256"])


def initialize_simulation(root: Path, seed: Path, current: CurrentInstallation, *,
                          protected_paths: tuple[Path, ...]) -> "SimulationStore":
    """Explicit test/lab provisioning only; refuse every nonempty directory."""
    return _initialize_store(root, seed, current, protected_paths=protected_paths, store_type=SimulationStore)


def _initialize_store(root, seed, current, *, protected_paths, store_type):
    _staging_parent(root, protected_paths)
    _staging_parent(seed, protected_paths)
    if any(root.iterdir()) or root == seed or root.is_relative_to(seed) or seed.is_relative_to(root):
        reject("UPDATE_SIMULATION_INVALID", "Simulation root must be empty and separate from its seed")
    if current.managed is not True or not isinstance(current.data_compatibility, DataCompatibility):
        reject("UPDATE_SIMULATION_INVALID", "Simulation requires explicit current compatibility evidence")
    seed_hash = tree_digest(seed)
    for name in ("control", "transactions"):
        (root / name).mkdir(mode=0o700)
    for name in store_type.LOCK_NAMES:
        fd = os.open(root / "control" / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fsync(fd)
        os.close(fd)
    shutil.copytree(seed, root / "active", symlinks=True)
    if tree_digest(root / "active") != seed_hash:
        reject("UPDATE_SIMULATION_INVALID", "Seed changed during simulation provisioning")
    sync_tree(root / "active")
    extra = store_type._provision_extra(root, seed)
    installation_id = uuid.uuid4().hex
    atomic_control(root / "control/ledger.json", {"schema_version": 1, "installation_id": installation_id,
                   "revision": 1, "current": {"version": current.version, "tree_sha256": seed_hash},
                   "watermarks": [], "transaction": None})
    # Registration is written LAST. Interrupted provisioning cannot be adopted
    # from orphan folders; it is not an installation with valid authority.
    registration = {"schema_version": store_type.SCHEMA, "simulation": store_type.SIMULATION, "installation_id": installation_id,
                    "root": str(root), **filesystem_identity(root),
                    "product_id": current.product_id, "platform": current.platform,
                    "architecture": current.architecture, "updater_version": current.updater_version,
                    "data_compatibility": _compat_wire(current.data_compatibility),
                    "directory_identities": {name: filesystem_identity(root / name) for name in ("control", "transactions")},
                    "lock_identities": {name: filesystem_identity(root / "control" / name) for name in store_type.LOCK_NAMES},
                    "protected_paths": [str(p.resolve()) for p in protected_paths], **extra}
    atomic_control(root / "control/registration.json", registration)
    sync_directory(root / "transactions")
    sync_directory(root)
    return store_type(root, protected_paths=protected_paths)


class InstallationStore:
    """One ledger/lock authority. Concrete modes validate their own registration."""
    SCHEMA = 1
    SIMULATION = True
    EXTRA_FIELDS = frozenset()
    LOCK_NAMES = ("operation.lock", "runtime.lock")

    @classmethod
    def _provision_extra(cls, root, seed):
        return {}

    def _validate_extra(self, value):
        pass

    def __init__(self, root: Path, *, protected_paths: tuple[Path, ...]):
        self.root, self.protected_paths = root, protected_paths
        try:
            self._registered = self._registration()
        except UpdateError:
            raise
        except Exception:
            raise UpdateError("UPDATE_CONTROL_INVALID", "Simulation ownership could not be validated") from None

    def _registration(self):
        _staging_parent(self.root, self.protected_paths)
        _staging_parent(self.root / "control", self.protected_paths)
        _staging_parent(self.root / "transactions", self.protected_paths)
        value = read_control(self.root / "control/registration.json")
        fields(value, {"schema_version", "simulation", "installation_id", "root", "device", "inode",
                       "product_id", "platform", "architecture", "updater_version", "data_compatibility", "protected_paths",
                       "directory_identities", "lock_identities"} | self.EXTRA_FIELDS)
        validate_device(value["device"])
        if (type(value["schema_version"]) is not int or value["schema_version"] != self.SCHEMA
                or value["simulation"] is not self.SIMULATION or value["root"] != str(self.root)
                or value["device"] != filesystem_identity(self.root)["device"]
                or type(value["inode"]) is not int or value["inode"] != self.root.stat().st_ino
                or value["protected_paths"] != [str(p.resolve()) for p in self.protected_paths]):
            reject("UPDATE_CONTROL_INVALID", "Simulation registration differs from local ownership")
        text(value["installation_id"], r"[0-9a-f]{32}", 32)
        for key, parent, names in (("directory_identities", self.root, {"control", "transactions"}),
                                  ("lock_identities", self.root / "control", set(self.LOCK_NAMES))):
            identities = fields(value[key], names)
            for name in names:
                identity = fields(identities[name], {"device", "inode"})
                validate_device(identity["device"])
                number(identity["inode"], maximum=2**64 - 1)
                if identity != filesystem_identity(parent / name) or identity["device"] != value["device"]:
                    reject("UPDATE_CONTROL_INVALID", "Registered directory or lock identity changed")
        self._validate_extra(value)
        CurrentInstallation(value["product_id"], "0.0.0", value["platform"], value["architecture"],
                            value["updater_version"], DataCompatibility.parse(value["data_compatibility"]), True)
        return value

    def _guard(self):
        if self._registration() != self._registered:
            reject("UPDATE_CONTROL_INVALID", "Simulation ownership changed")

    @property
    def registration(self):
        # A caller's inspection must not mutate the captured authority in place.
        return deepcopy(self._registered)

    @contextmanager
    def _operation(self):
        self._guard()
        with lock_file(self.root / "control/operation.lock", expected_identity=self.registration["lock_identities"]["operation.lock"]):
            with lock_file(self.root / "control/runtime.lock", expected_identity=self.registration["lock_identities"]["runtime.lock"]) as descriptor:
                self._guard()
                if self.SIMULATION:
                    yield
                else:
                    from backend.core.installation_lease import retain_descriptor
                    with retain_descriptor(descriptor):
                        yield

    def _ledger(self):
        value = read_control(self.root / "control/ledger.json")
        self._validate_ledger(value)
        return value

    def _validate_ledger(self, value):
        fields(value, {"schema_version", "installation_id", "revision", "current", "watermarks", "transaction"})
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["installation_id"] != self.registration["installation_id"]):
            reject("UPDATE_CONTROL_INVALID", "Ledger belongs to a different installation")
        number(value["revision"])
        _identity(value["current"])
        watermarks = value["watermarks"]
        if type(watermarks) is not list or len(watermarks) > 2:
            reject("UPDATE_CONTROL_INVALID", "Invalid channel watermarks")
        channels = set()
        for wire in watermarks:
            mark = _watermark(wire)
            if mark.channel in channels or (mark.product_id, mark.platform, mark.architecture) != (
                    self.registration["product_id"], self.registration["platform"], self.registration["architecture"]):
                reject("UPDATE_CONTROL_INVALID", "Watermark scope differs from installation")
            channels.add(mark.channel)
        transaction = value["transaction"]
        if transaction is None:
            return
        fields(transaction, {"id", "state", "old", "new", "watermark", "release", "failure_code"})
        text(transaction["id"], r"[0-9a-f]{32}", 32)
        if transaction["state"] not in STATES:
            reject("UPDATE_CONTROL_INVALID", "Unknown transaction state")
        _identity(transaction["old"])
        _identity(transaction["new"])
        mark = _watermark(transaction["watermark"])
        accepted = next((_watermark(w) for w in watermarks if w["channel"] == mark.channel), None)
        if (accepted is None or accepted.sequence < mark.sequence
                or accepted.published_at < mark.published_at or Version.parse(accepted.version) < Version.parse(mark.version)
                or (accepted.sequence == mark.sequence and accepted != mark)
                or (Version.parse(accepted.version) == Version.parse(mark.version) and accepted.artifact_sha256 != mark.artifact_sha256)
                or mark.version != transaction["new"]["version"]):
            reject("UPDATE_CONTROL_INVALID", "Transaction release evidence differs from high water")
        if transaction["old"]["tree_sha256"] == transaction["new"]["tree_sha256"]:
            reject("UPDATE_CONTROL_INVALID", "Old and new payload identities must differ")
        if Version.parse(transaction["new"]["version"]) <= Version.parse(transaction["old"]["version"]):
            reject("UPDATE_CONTROL_INVALID", "Transaction is not an ordinary upgrade")
        expected = transaction["new"] if transaction["state"] == "COMMITTED" else transaction["old"]
        if value["current"] != expected:
            reject("UPDATE_CONTROL_INVALID", "Current identity disagrees with transaction state")
        release = fields(transaction["release"], {"product_id", "version", "platform", "architecture", "source_commit", "source_tree"})
        if (release["product_id"], release["version"], release["platform"], release["architecture"]) != (
                mark.product_id, mark.version, mark.platform, mark.architecture):
            reject("UPDATE_CONTROL_INVALID", "Health target differs from transaction evidence")
        for name in ("source_commit", "source_tree"):
            text(release[name], r"[0-9a-f]{40}", 40)
        if transaction["failure_code"] is not None:
            text(transaction["failure_code"], r"UPDATE_[A-Z_]+", 128)

    def _save(self, ledger, state=None, *, failure=None):
        self._guard()
        ledger["revision"] += 1
        if state is not None:
            ledger["transaction"]["state"] = state
            if failure is not None:
                ledger["transaction"]["failure_code"] = failure
        self._validate_ledger(ledger)
        atomic_control(self.root / "control/ledger.json", ledger)

    def _matches(self, path, expected):
        return path.exists() and not path.is_symlink() and tree_digest(path) == expected["tree_sha256"]

    def _move(self, source, target, expected=None):
        self._guard()
        for parent in (source.parent, target.parent):
            _staging_parent(parent, self.protected_paths)
            if not parent.is_relative_to(self.root):
                reject("UPDATE_RECOVERY_REQUIRED", "Directory switch must stay inside the simulation installation")
        if not source.is_dir() or source.is_symlink() or target.exists() or target.is_symlink():
            reject("UPDATE_RECOVERY_REQUIRED", "Directory switch target is not empty or source changed")
        if source.stat().st_dev != self.root.stat().st_dev:
            reject("UPDATE_RECOVERY_REQUIRED", "Directory switch must remain on the installation volume")
        if expected is not None and not self._matches(source, expected):
            reject("UPDATE_RECOVERY_REQUIRED", "Directory switch source differs from recorded identity")
        os.rename(source, target)
        sync_directory(source.parent)
        if target.parent != source.parent:
            sync_directory(target.parent)

    def _slots(self, transaction):
        directory = self.root / "transactions" / transaction["id"]
        _staging_parent(directory, self.protected_paths)
        return self.root / "active", directory / "old", directory / "new", directory / "failed"

    def rollback_selection(self):
        """Only the retained predecessor of the last committed transaction.

        Caller holds preparation/runtime or operation locks. The original
        locally recorded tree is the authority, not a newly supplied manifest.
        No database/config rollback, directory scanning or arbitrary downgrade.
        """
        from types import SimpleNamespace
        import hashlib
        from .tree import encode
        self._guard()
        ledger = self._ledger()
        txn = ledger["transaction"]
        if txn is None or txn["state"] != "COMMITTED": return None
        active, old, new, failed = self._slots(txn)
        if (not self._matches(active, txn["new"]) or not self._matches(old, txn["old"])
                or new.exists() or new.is_symlink() or failed.exists() or failed.is_symlink()):
            reject("UPDATE_RECOVERY_REQUIRED", "Retained rollback layout changed")
        product = json_object((old / "product.json").read_bytes(), 64 * 1024)
        build = json_object((old / "build-manifest.json").read_bytes(), 2 * 1024**2)
        expected = SimpleNamespace(product_id=self.registration["product_id"], version=txn["old"]["version"],
            platform=self.registration["platform"], architecture=self.registration["architecture"],
            source_commit=build["source"]["commit"], source_tree=build["source"]["tree"],
            data_compatibility=DataCompatibility.parse(self.registration["data_compatibility"]))
        if product.get("version") != expected.version:
            reject("UPDATE_PACKAGE_CONTRACT", "Retained version differs from recorded predecessor")
        verify_bundle_contract(old, expected)
        identity = {"installation_id": ledger["installation_id"], "transaction_id": txn["id"],
                    "current": ledger["current"], "previous": txn["old"]}
        return {"rollback_id": hashlib.sha256(encode(identity)).hexdigest(), "version": expected.version}

    def _rollback(self, rollback_id, *, health_check, checkpoint=lambda _: None):
        """Fixed-bootstrap driver. Preflight predecessor before durable intent."""
        digest(rollback_id)
        with self._operation():
            selection = self.rollback_selection()
            if selection is None or selection["rollback_id"] != rollback_id:
                reject("UPDATE_SELECTION_STALE", "Retained rollback selection changed")
            ledger = self._ledger()
            txn = ledger["transaction"]
            active, old, _, _ = self._slots(txn)
            # New unique private profile; never reuse the candidate's prior I/O.
            with os.scandir(old.parent) as entries:
                if sum(1 for entry in entries if entry.name.startswith("rollback-probe-")) >= 8:
                    reject("UPDATE_RETAIN_LIMIT", "Rollback diagnostic retention requires separate maintenance")
            profile = old.parent / ("rollback-probe-" + uuid.uuid4().hex)
            profile.mkdir(mode=0o700); sync_directory(profile.parent)
            try:
                report = health_check(old, profile)
                product = json_object((old / "product.json").read_bytes(), 64 * 1024)
                build = json_object((old / "build-manifest.json").read_bytes(), 2 * 1024**2)
                expected = {"product_id": product["product_id"], "version": selection["version"],
                    "platform": product["platform"], "architecture": product["architecture"],
                    "source_commit": build["source"]["commit"], "source_tree": build["source"]["tree"], "passed": True}
                if type(report) is not HealthReport or asdict(report) != expected:
                    reject("UPDATE_HEALTH_FAILED", "Retained predecessor startup failed")
                if self.rollback_selection() != selection:
                    reject("UPDATE_RECOVERY_REQUIRED", "Payload changed during rollback preflight")
            except Exception:
                return TransactionResult("failed", ledger["current"]["version"], "UPDATE_HEALTH_FAILED")
            checkpoint("user_rollback_health_passed")
            # Reuse the original transaction's known old/new slots and sole
            # ledger. Its high water is preserved. Crash recovery now completes
            # the explicit rollback; no second journal or guessed old payload.
            ledger["current"] = dict(txn["old"])
            try:
                self._save(ledger, "ROLLBACK", failure="UPDATE_USER_ROLLBACK")
                checkpoint("user_rollback_intent")
                return self._recover_locked(checkpoint=checkpoint)
            except Exception:
                result = self._recover_locked(checkpoint=checkpoint)
                if result.status == "committed":
                    return TransactionResult("failed", result.version, "UPDATE_OPERATION_FAILED")
                return result

    def _recover_locked(self, *, checkpoint=lambda _: None, failure_code="UPDATE_INTERRUPTED"):
        try:
            ledger = self._ledger()
            transaction = ledger["transaction"]
            active = self.root / "active"
            if transaction is None:
                if not self._matches(active, ledger["current"]):
                    reject("UPDATE_RECOVERY_REQUIRED", "Current payload changed")
                return TransactionResult("idle", ledger["current"]["version"])
            active, old, new, failed = self._slots(transaction)
            old_id, new_id = transaction["old"], transaction["new"]
            # Inspect all recorded slots. Never remove, overwrite, or adopt an
            # unknown directory, including a symlink/dangling link at a slot.
            identities = []
            for path in (active, old, new, failed):
                if path.is_symlink():
                    reject("UPDATE_RECOVERY_REQUIRED", "Recorded slot is redirected")
                identities.append(tree_digest(path) if path.exists() else None)
            a, o, n, f = identities
            oh, nh = old_id["tree_sha256"], new_id["tree_sha256"]
            if transaction["state"] == "COMMITTED":
                if (a, o, n, f) != (nh, oh, None, None):
                    reject("UPDATE_RECOVERY_REQUIRED", "Committed layout changed")
                return TransactionResult("committed", new_id["version"])
            allowed = {(oh, None, nh, None), (None, oh, nh, None), (nh, oh, None, None),
                       (None, oh, None, nh), (oh, None, None, nh)}
            if (a, o, n, f) not in allowed:
                reject("UPDATE_RECOVERY_REQUIRED", "Recorded payload evidence is missing or ambiguous")
            if transaction["state"] == "ROLLED_BACK":
                if a != oh:
                    reject("UPDATE_RECOVERY_REQUIRED", "Rollback record does not match current payload")
                return TransactionResult("rolled_back", old_id["version"],
                    None if transaction["failure_code"] == "UPDATE_USER_ROLLBACK" else transaction["failure_code"])
            self._save(ledger, "ROLLBACK", failure=transaction["failure_code"] or failure_code)
            checkpoint("rollback_intent")
            if a == nh:
                self._move(active, failed, new_id)
                checkpoint("failed_renamed")
                a = None
            if a is None:
                self._move(old, active, old_id)
                checkpoint("old_restored")
            if not self._matches(active, old_id):
                reject("UPDATE_RECOVERY_REQUIRED", "Restored payload differs from previous identity")
            self._save(ledger, "ROLLED_BACK")
            checkpoint("rollback_written")
            return TransactionResult("rolled_back", old_id["version"],
                None if transaction["failure_code"] == "UPDATE_USER_ROLLBACK" else transaction["failure_code"])
        except Exception:
            # Do not turn corruption into a new baseline or delete diagnostics.
            return TransactionResult("recovery_required", error_code="UPDATE_RECOVERY_REQUIRED")

    def recover(self, *, checkpoint=lambda _: None) -> TransactionResult:
        try:
            with self._operation():
                return self._recover_locked(checkpoint=checkpoint)
        except UpdateError as error:
            if error.code == "UPDATE_BUSY":
                return TransactionResult("busy", error_code=error.code)
            return TransactionResult("recovery_required", error_code="UPDATE_RECOVERY_REQUIRED")
        except Exception:
            return TransactionResult("recovery_required", error_code="UPDATE_RECOVERY_REQUIRED")

    @contextmanager
    def runtime_lease(self):
        """Admit a verified payload and retain its kernel lease through child use."""
        self._guard()
        with lock_file(self.root / "control/runtime.lock", shared=True,
                       expected_identity=self.registration["lock_identities"]["runtime.lock"]) as descriptor:
            with lock_file(self.root / "control/operation.lock", expected_identity=self.registration["lock_identities"]["operation.lock"]):
                ledger = self._ledger()
                transaction = ledger["transaction"]
                if transaction is not None and transaction["state"] not in TERMINAL:
                    reject("UPDATE_RECOVERY_REQUIRED", "Pending update must recover before runtime admission")
                if self._recover_locked().status == "recovery_required":
                    reject("UPDATE_RECOVERY_REQUIRED", "Terminal installation evidence changed")
                active = self.root / "active"
                if not self._matches(active, ledger["current"]):
                    reject("UPDATE_RECOVERY_REQUIRED", "Current payload differs from installation evidence")
                self._guard()
            # Keep the shared runtime inode locked for the whole admitted use.
            yield active, descriptor

    @contextmanager
    def runtime_session(self):
        with self.runtime_lease() as (active, _descriptor):
            yield active

    def apply(self, policy: UpdatePolicy, verifier: ManifestVerifier, raw: bytes, detached: bytes,
              archive: Path, *, health_check, clock=lambda: datetime.now(timezone.utc),
              checkpoint=lambda _: None) -> TransactionResult:
        try:
            return self._apply(policy, verifier, raw, detached, archive, health_check=health_check,
                               clock=clock, checkpoint=checkpoint)
        except UpdateError:
            raise
        except Exception:
            raise UpdateError("UPDATE_TRANSACTION_FAILED", "Simulation update failed; recover from recorded authority") from None

    def _apply(self, policy, verifier, raw, detached, archive, *, health_check, clock, checkpoint):
        if not policy.enabled:
            return TransactionResult("disabled")
        if not callable(health_check):
            reject("UPDATE_HEALTH_DRIVER", "An explicit trusted simulation health driver is required")
        with self._operation():
            ledger = self._ledger()
            transaction = ledger["transaction"]
            if transaction is not None and transaction["state"] not in TERMINAL:
                reject("UPDATE_RECOVERY_REQUIRED", "Previous transaction must recover first")
            if self._recover_locked().status == "recovery_required":
                reject("UPDATE_RECOVERY_REQUIRED", "Current or retained installation evidence changed")
            local = self.registration
            current = CurrentInstallation(local["product_id"], ledger["current"]["version"], local["platform"],
                       local["architecture"], local["updater_version"], DataCompatibility.parse(local["data_compatibility"]), True)
            previous = next((_watermark(w) for w in ledger["watermarks"] if w["channel"] == policy.channel), None)

            def checked():
                return check_for_update(policy, current, verifier,
                    lambda url, _: raw if url == policy.manifest_url else detached,
                    now=clock(), previous=previous)

            result = checked()
            if result.status != "available":
                if result.watermark is not None:
                    mark = _watermark_wire(result.watermark)
                    ledger["watermarks"] = sorted([w for w in ledger["watermarks"] if w["channel"] != policy.channel] + [mark],
                                                   key=lambda w: w["channel"])
                    self._save(ledger)
                return TransactionResult(result.status, current.version)
            manifest = result.candidate
            # No PreparedResult, bool approval or caller-provided tree hash can
            # authorize a switch. Reverify archive and re-extract locally.
            _staging_parent(archive.parent, self.protected_paths)
            if archive.is_symlink() or not archive.is_file():
                reject("UPDATE_PACKAGE_CONTRACT", "Archive must be a regular local file")
            with os.scandir(self.root / "transactions") as entries:
                for index, _ in enumerate(entries):
                    if index >= 31:
                        reject("UPDATE_RETAIN_LIMIT", "Retained transaction limit requires separate maintenance")
            directory = self.root / "transactions" / uuid.uuid4().hex
            directory.mkdir(mode=0o700)
            sync_directory(directory.parent)
            # Orphans from interrupted preparation are retained, never adopted.
            fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source, (directory / "archive").open("x+b") as copy:
                _verify_file(source, manifest, lambda: False)
                remaining = manifest.bytes
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        reject("UPDATE_DOWNLOAD_SIZE", "Archive changed during copy")
                    copy.write(chunk)
                    remaining -= len(chunk)
                copy.flush()
                os.fsync(copy.fileno())
                _verify_file(copy, manifest, lambda: False)
                bundle = _extract_archive(copy, manifest.archive_format, directory / "payload", directory,
                                          ExtractionLimits(), lambda: False)
            verify_bundle_contract(bundle, manifest)
            checkpoint("candidate_validated")
            if not self._matches(self.root / "active", ledger["current"]):
                reject("UPDATE_RECOVERY_REQUIRED", "Current payload changed before switch")
            self._move(bundle, directory / "new")
            sync_tree(directory / "new")
            new_identity = {"version": manifest.version, "tree_sha256": tree_digest(directory / "new")}
            (directory / "probe").mkdir(mode=0o700)
            sync_directory(directory)
            checkpoint("candidate_synced")
            checked()  # freshness/compatibility immediately before durable intent
            mark = _watermark_wire(result.watermark)
            ledger["watermarks"] = sorted([w for w in ledger["watermarks"] if w["channel"] != policy.channel] + [mark],
                                           key=lambda w: w["channel"])
            release = {k: getattr(manifest, k) for k in ("product_id", "version", "platform", "architecture", "source_commit", "source_tree")}
            ledger["transaction"] = {"id": directory.name, "state": "PREPARED", "old": dict(ledger["current"]),
                 "new": new_identity, "watermark": mark, "release": release, "failure_code": None}
            # The transaction and its channel high water become authoritative
            # together BEFORE moving the old program. Rollback keeps high water.
            try:
                self._save(ledger)
                checkpoint("prepared")
                self._save(ledger, "SAVE_OLD")
                checkpoint("save_old_intent")
                self._move(self.root / "active", directory / "old", ledger["transaction"]["old"])
                checkpoint("old_renamed")
                self._save(ledger, "PUBLISH_NEW")
                checkpoint("publish_new_intent")
                self._move(directory / "new", self.root / "active", new_identity)
                checkpoint("new_renamed")
                self._save(ledger, "CHECK_NEW")
                checkpoint("check_new_intent")
                # This is an injected trusted SIMULATION driver, not an arbitrary
                # command from the archive or a shipping isolated helper.
                _staging_parent(directory / "probe", self.protected_paths)
                report = health_check(self.root / "active", directory / "probe")
                if type(report) is not HealthReport or report.passed is not True or asdict(report) != {**release, "passed": True}:
                    reject("UPDATE_HEALTH_FAILED", "Simulated startup identity/health did not match")
                if not self._matches(self.root / "active", new_identity):
                    reject("UPDATE_HEALTH_FAILED", "Simulated probe changed the candidate payload")
                if not self._matches(directory / "old", ledger["transaction"]["old"]):
                    reject("UPDATE_RECOVERY_REQUIRED", "Retained previous payload changed during health check")
                checked()
                checkpoint("health_passed")
                ledger["current"] = dict(new_identity)
                self._save(ledger, "COMMITTED")
                checkpoint("commit_written")
                return TransactionResult("committed", manifest.version)
            except Exception as error:
                # Reload durable authority; an fsync/write error may have taken
                # effect even if the call raised. Never trust the in-memory step.
                failure = "UPDATE_HEALTH_FAILED" if isinstance(error, UpdateError) and error.code == "UPDATE_HEALTH_FAILED" else "UPDATE_OPERATION_FAILED"
                recovered = self._recover_locked(checkpoint=checkpoint, failure_code=failure)
                if recovered.status == "idle" or (recovered.status == "committed" and recovered.version != manifest.version):
                    return TransactionResult("failed", current.version, failure)
                return recovered


class SimulationStore(InstallationStore):
    """Test/lab mode never accepts a managed or production registration."""
