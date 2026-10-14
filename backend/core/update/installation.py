"""Opt-in POSIX managed installation, stable bootstrap and runtime admission.

Provision only a new empty private root from an explicitly supplied verified
local package. Never adopt legacy installation markers or project directories.
Automatic updates remain disabled; this is the real installation/lease layer,
not permission to bypass signature, health or platform acceptance gates.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from types import SimpleNamespace

from backend.core.installation_lease import retain_descriptor
from .checker import CurrentInstallation
from .contracts import verify_bundle_contract, _metadata
from .health import probe_managed_host
from .manifest import DataCompatibility
from .persistence import filesystem_identity, descriptor_identity, validate_device, sync_directory, read_control, open_readonly
from .staging import _staging_parent
from .transaction import InstallationStore, TransactionResult, _initialize_store, TERMINAL
from .tree import inventory, tree_digest, sync_tree
from .validation import UpdateError, digest, fields, json_object, number, reject


def _ownership_supported():
    if os.name == "posix": return True
    from .windows_storage import supported
    return supported()


def _host_name():
    return "gitgo-host.exe" if os.name == "nt" else "gitgo-host"


def _windows_launcher(host):
    # Only the installation-root wrapper uses this fixed relative layout.
    # Expanding %~dp0 preserves Unicode paths without changing console encoding
    # or interpolating a user path as batch syntax.
    if host.parts[-3:] != ("bootstrap", "gitgo-host", "gitgo-host.exe"):
        reject("UPDATE_INSTALL_INVALID", "Unsupported Windows bootstrap layout")
    return ('@echo off\n@setlocal DisableDelayedExpansion\n'
            '@"%~dp0bootstrap\\gitgo-host\\gitgo-host.exe" --gitgo-internal-role managed-launch %*\n'
            '@exit /b %errorlevel%\n')


class ManagedStore(InstallationStore):
    # Windows has a distinct immutable bootstrap contract. Never adopt a
    # POSIX registration or upgrade an old bootstrap's asserted capabilities.
    SCHEMA = 6 if os.name == "nt" else 5
    BOOTSTRAP_API = 7 if os.name == "nt" else 6
    SIMULATION = False
    EXTRA_FIELDS = frozenset({"bootstrap", "bootstrap_api", "updates_enabled", "preparation_directory_identity"})
    LOCK_NAMES = (*InstallationStore.LOCK_NAMES, "preparation.lock")

    @classmethod
    def _provision_extra(cls, root, seed):
        bootstrap = root / "bootstrap"
        bootstrap.mkdir(mode=0o700)
        if os.name == "nt":
            from .windows_storage import copy_tree, durable_copy
            copy_tree(seed / "internal/gitgo-host", bootstrap / "gitgo-host")
        else: shutil.copytree(seed / "internal/gitgo-host", bootstrap / "gitgo-host", symlinks=True)
        (bootstrap / "gitgo-host").chmod(0o700)
        from .release_policy import POLICY_FILE
        if (seed / POLICY_FILE).exists():
            if os.name == "nt": durable_copy(seed / POLICY_FILE, bootstrap / POLICY_FILE)
            else:
                shutil.copyfile(seed / POLICY_FILE, bootstrap / POLICY_FILE)
                (bootstrap / POLICY_FILE).chmod(0o400)
        sync_tree(bootstrap)
        preparation = root / "control/preparation"
        preparation.mkdir(mode=0o700)
        sync_directory(preparation)
        # Stable wrapper is installed once, not supplied by a remote candidate.
        wrapper = root / ("gitgo.cmd" if os.name == "nt" else "gitgo")
        content = (_windows_launcher(bootstrap / "gitgo-host/gitgo-host.exe") if os.name == "nt" else
            "#!/bin/sh\nexec " + shlex.quote(str(bootstrap / "gitgo-host/gitgo-host")) + ' --gitgo-internal-role managed-launch "$@"\n')
        with wrapper.open("x", encoding="utf-8") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        wrapper.chmod(0o700)
        if os.name == "posix":
            descriptor = open_readonly(wrapper)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        return {"bootstrap": {"directory_identity": filesystem_identity(bootstrap),
                              "tree_sha256": tree_digest(bootstrap),
                              "launcher_sha256": _file_digest(wrapper)}, "bootstrap_api": cls.BOOTSTRAP_API, "updates_enabled": False,
                "preparation_directory_identity": filesystem_identity(preparation)}

    def _validate_extra(self, value):
        if type(value["bootstrap_api"]) is not int or value["bootstrap_api"] != self.BOOTSTRAP_API:
            reject("UPDATE_CONTROL_INVALID", "Managed bootstrap API is unavailable")
        if type(value["updates_enabled"]) is not bool or value["updates_enabled"] is not False:
            reject("UPDATE_CONTROL_INVALID", "This managed installation does not support activation yet")
        preparation = self.root / "control/preparation"
        _staging_parent(preparation, self.protected_paths)
        preparation_identity = fields(value["preparation_directory_identity"], {"device", "inode"})
        if (preparation_identity != filesystem_identity(preparation)
                or preparation_identity["device"] != value["device"]):
            reject("UPDATE_CONTROL_INVALID", "Preparation directory identity changed")
        bootstrap = fields(value["bootstrap"], {"directory_identity", "tree_sha256", "launcher_sha256"})
        identity = fields(bootstrap["directory_identity"], {"device", "inode"})
        validate_device(identity["device"])
        number(identity["inode"], maximum=2**128 - 1 if os.name == "nt" else 2**64 - 1)
        digest(bootstrap["tree_sha256"]); digest(bootstrap["launcher_sha256"])
        _staging_parent(self.root / "bootstrap", self.protected_paths)
        if (identity != filesystem_identity(self.root / "bootstrap") or identity["device"] != value["device"]
                or tree_digest(self.root / "bootstrap") != bootstrap["tree_sha256"]
                or _file_digest(self.root / ("gitgo.cmd" if os.name == "nt" else "gitgo")) != bootstrap["launcher_sha256"]):
            reject("UPDATE_CONTROL_INVALID", "Stable bootstrap or launcher changed")

    def apply(self, policy, verifier, raw, detached, archive, *, clock=None, checkpoint=None):
        # No injected callable health checks in the managed mode. Activation
        # and locally pinned release trust are a separate, not-yet-open API.
        return TransactionResult("disabled")


def _file_digest(path):
    import hashlib
    import stat
    fd = open_readonly(path)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096
                or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022))):
            reject("UPDATE_CONTROL_INVALID", "Stable launcher ownership is invalid")
        return hashlib.file_digest(handle, "sha256").hexdigest()


def initialize_managed(root: Path, seed: Path, *, protected_paths: tuple[Path, ...]) -> ManagedStore:
    """New local installation only; validate package and actual bootstrap API."""
    if not _ownership_supported():
        reject("UPDATE_TRANSACTION_PLATFORM", "Managed installation ownership adapter is unavailable")
    try:
        _staging_parent(root, protected_paths)
        if any(root.iterdir()) or seed != seed.resolve(strict=True) or not seed.is_dir():
            reject("UPDATE_INSTALL_INVALID", "Managed installation requires an empty root and canonical package")
        if root == seed or root.is_relative_to(seed) or seed.is_relative_to(root):
            reject("UPDATE_INSTALL_INVALID", "Installation and package must be separate")
        for protected in protected_paths:
            if seed == protected.resolve() or seed.is_relative_to(protected.resolve()) or protected.resolve().is_relative_to(seed):
                reject("UPDATE_PROTECTED_PATHS", "Package overlaps protected data")
        # Read-only, bounded package identity. The full inventory below binds
        # these local initial-install declarations to their actual file bytes.
        before = inventory(seed)
        records = {record["path"]: record for record in before}
        product = _metadata(seed, "product.json", records)
        if os.name == "nt" and product.get("installation_mode") != "managed-windows-v1":
            reject("UPDATE_INSTALL_INVALID", "Windows managed installation requires its explicit package mode")
        build = _metadata(seed, "build-manifest.json", records)
        contract = _metadata(seed, "update-data-contract.json", records)
        compatibility = DataCompatibility.parse(contract["data_compatibility"])
        identity = SimpleNamespace(**{key: product[key] for key in ("product_id", "version", "platform", "architecture")},
            source_commit=build["source"]["commit"], source_tree=build["source"]["tree"], data_compatibility=compatibility)
        verify_bundle_contract(seed, identity)
        from .release_policy import POLICY_FILE, read_policy_file, parse_release_policy
        if POLICY_FILE in records:
            parse_release_policy(read_policy_file(seed / POLICY_FILE), product["platform"], product["architecture"])
        with tempfile.TemporaryDirectory(prefix="gitgo-managed-incoming-", dir=root.parent) as temporary:
            temporary = Path(temporary).resolve()
            private_seed = temporary / "package"
            if os.name == "nt":
                from .windows_storage import copy_tree
                copy_tree(seed, private_seed)
            else: shutil.copytree(seed, private_seed, symlinks=True)
            private_seed.chmod(0o700)
            if inventory(private_seed) != before or inventory(seed) != before:
                reject("UPDATE_INSTALL_INVALID", "Package changed during private preparation")
            probe = temporary / "probe"; probe.mkdir(mode=0o700)
            report = probe_managed_host(private_seed / "internal/gitgo-host" / _host_name(), probe)["runtime"]
            if (any(report[key] != product[key] for key in ("product_id", "version", "platform", "architecture"))
                    or DataCompatibility.parse(report["data_compatibility"]) != compatibility):
                reject("UPDATE_INSTALL_INVALID", "Initial frozen runtime differs from package evidence")
            if inventory(private_seed) != before:
                reject("UPDATE_INSTALL_INVALID", "Initial runtime changed its package")
            current = CurrentInstallation(product["product_id"], product["version"], product["platform"],
                product["architecture"], "0.1.0", compatibility, True)
            return _initialize_store(root, private_seed, current, protected_paths=protected_paths, store_type=ManagedStore)
    except UpdateError:
        raise
    except Exception:
        reject("UPDATE_INSTALL_INVALID", "Managed initial installation could not be validated")


def open_managed(root: Path) -> ManagedStore:
    """Read protection only from an owned registration, then validate everything."""
    if not _ownership_supported() or not isinstance(root, Path) or root != root.resolve(strict=True):
        reject("UPDATE_CONTROL_INVALID", "Managed installation ownership is unavailable")
    # Reading is not authority. ManagedStore revalidates canonical paths, mode,
    # captured inode identities, local registration type and stable bootstrap.
    value = read_control(root / "control/registration.json")
    paths = value.get("protected_paths")
    if type(paths) is not list or not 1 <= len(paths) <= 256 or any(type(p) is not str for p in paths):
        reject("UPDATE_CONTROL_INVALID", "Managed protection list is invalid")
    return ManagedStore(root, protected_paths=tuple(Path(p) for p in paths))


def profile_paths() -> tuple[Path, ...]:
    """Compute current profile locations without loading config or credentials."""
    from backend.core.config import ConfigManager
    from backend.core.llm_config import LLMConfigManager
    from backend.core.storage.paths import _default_state_home
    paths = [ConfigManager.default_path().parent, LLMConfigManager._config_path().resolve().parent,
             _default_state_home()]
    secret = os.environ.get("GITGO_LLM_SECRET_PATH", "").strip()
    if secret: paths.append(Path(secret).expanduser().resolve().parent)
    return tuple(dict.fromkeys(p.resolve() for p in paths))


def protected_install_paths() -> tuple[Path, ...]:
    """Read bounded local repository paths in both formats; never migrate config."""
    import json
    from backend.core.config import ConfigManager
    paths = list(profile_paths())
    config = ConfigManager.find_config()
    if config is not None:
        paths.append(config.parent.resolve())
        import stat
        fd = open_readonly(config)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024 or info.st_nlink != 1
                    or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022))):
                reject("UPDATE_PROTECTED_PATHS", "Existing profile cannot be safely inspected")
            value = json.loads(handle.read(256 * 1024 + 1).decode("utf-8"))
        if type(value) is not dict:
            reject("UPDATE_PROTECTED_PATHS", "Existing workspace protection list is unavailable")
        # Mirror the existing schema's read semantics without invoking its
        # load/save/migration or any provider/remote access implementation.
        projects = value.get("projects", [value] if "workspace_path" in value or "backup_path" in value else [])
        if type(projects) is not list or len(projects) > 128:
            reject("UPDATE_PROTECTED_PATHS", "Existing workspace protection list is unavailable")
        for project in projects:
            paths.extend(_local_repository_paths(project))
    result = tuple(dict.fromkeys(paths))
    if len(result) > 256: reject("UPDATE_PROTECTED_PATHS", "Protected repository list exceeds its limit")
    return result


def _local_repository_paths(project):
    if type(project) is not dict: reject("UPDATE_PROTECTED_PATHS", "Registered project is invalid")
    values = []
    if "workspace_path" in project or "backup_path" in project:
        values.extend((project.get("workspace_path", project.get("workspace", "")), project.get("backup_path", "")))
    else:
        for role in ("workspace", "release", "trial"):
            node = project.get(role)
            if node is None: continue
            if type(node) is not dict: reject("UPDATE_PROTECTED_PATHS", "Registered repository node is invalid")
            access = node.get("file_access") or {}
            if type(access) is not dict: reject("UPDATE_PROTECTED_PATHS", "Registered repository access is invalid")
            kind = access.get("kind", "local")
            # The current model treats unrecognized kinds as local. Do not let
            # such a declaration omit a filesystem path from protection.
            if kind not in ("ssh", "smb"): values.append(access.get("path", ""))
            if kind == "ssh": values.append(access.get("key_path", ""))
    paths = []
    for raw in values:
        if type(raw) is not str or len(raw) > 4096 or any(ord(c) < 32 for c in raw):
            reject("UPDATE_PROTECTED_PATHS", "Registered local repository path is invalid")
        if not raw: continue
        path = Path(raw).expanduser()
        if not path.is_absolute(): reject("UPDATE_PROTECTED_PATHS", "Repository protection requires absolute paths")
        paths.append(path.resolve())
    return paths


def _new_path(raw: str) -> Path:
    if not raw or len(raw) > 4096 or any(ord(c) < 32 for c in raw):
        reject("UPDATE_INSTALL_INVALID", "Invalid managed installation path")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or path != path.resolve():
        reject("UPDATE_INSTALL_INVALID", "Managed target must be a canonical absolute path")
    return path


def _create_private_directories(path: Path):
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink(): reject("UPDATE_INSTALL_INVALID", "Target has a redirected component")
        missing.append(current); current = current.parent
    if not current.is_dir(): reject("UPDATE_INSTALL_INVALID", "Target parent is not a directory")
    for directory in reversed(missing):
        if os.name == "nt":
            from .windows_storage import create_private_directory
            create_private_directory(directory)
        else: directory.mkdir(mode=0o700)
    return missing


def install_from_frozen_package(root_value: str, bin_value: str, protected_values: list[str]):
    """Explicit initial-install role. No overwrite, PATH profile edits or adoption."""
    if not _ownership_supported() or not getattr(sys, "frozen", False):
        reject("UPDATE_INSTALL_INVALID", "Managed installer requires a packaged Host with native ownership support")
    executable = Path(sys.executable)
    if (executable != executable.resolve(strict=True) or executable.parent.name != "gitgo-host"
            or executable.parents[1].name != "internal"):
        reject("UPDATE_INSTALL_INVALID", "Initial installer must come from a complete extracted package")
    seed = executable.parents[2]
    root, bin_dir = _new_path(root_value), _new_path(bin_value)
    protected = tuple(dict.fromkeys((*protected_install_paths(), *(_new_path(p) for p in protected_values))))
    for other in (seed, bin_dir, *protected):
        if root == other or root.is_relative_to(other) or other.is_relative_to(root):
            reject("UPDATE_PROTECTED_PATHS", "Installation target overlaps package, command directory or data")
    for other in (seed, *protected):
        if bin_dir == other or bin_dir.is_relative_to(other) or other.is_relative_to(bin_dir):
            reject("UPDATE_PROTECTED_PATHS", "Command directory overlaps package or protected data")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        reject("UPDATE_INSTALL_INVALID", "Initial managed target must be new or empty")
    command = bin_dir / ("gitgo.ps1" if os.name == "nt" else "gitgo")
    if command.exists() or command.is_symlink():
        reject("UPDATE_INSTALL_INVALID", "Public command already exists; choose a separate command directory")
    if len(protected_values) > 128: reject("UPDATE_PROTECTED_PATHS", "Too many protected workspace paths")
    _create_private_directories(root)
    _create_private_directories(bin_dir)
    # Never publish a public symlink before final registration and validation.
    store = initialize_managed(root, seed, protected_paths=protected)
    _new_path(str(bin_dir))
    if os.name == "nt":
        _staging_parent(bin_dir, protected)
        host = str(root / "bootstrap/gitgo-host/gitgo-host.exe").replace("'", "''")
        # UTF-8 BOM is supported by Windows PowerShell 5.1 and PowerShell 7.
        # Do not rewrite the user's global execution policy or PATH profile.
        content = ("param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitgoArguments)\n"
                   "$ErrorActionPreference = 'Stop'\n& '" + host +
                   "' --gitgo-internal-role managed-launch @GitgoArguments\nexit $LASTEXITCODE\n")
        from .windows_storage import open_file
        descriptor = open_file(command, create=True, write=True)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content.encode("utf-8-sig")); handle.flush(); os.fsync(handle.fileno())
        return {"schema_version": 1, "managed": True, "version": store._ledger()["current"]["version"],
                "updates_enabled": False}
    descriptor = os.open(bin_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            reject("UPDATE_INSTALL_INVALID", "Command directory is not owned or permits foreign replacement")
        os.symlink(str(root / "gitgo"), "gitgo", dir_fd=descriptor)
        os.fsync(descriptor)
    finally: os.close(descriptor)
    return {"schema_version": 1, "managed": True, "version": store._ledger()["current"]["version"],
            "updates_enabled": False}


def physical_installation(executable: Path) -> tuple[Path, str] | None:
    """Physical fixed layout, never GITGO_INSTALL_ROOT or other env assertions."""
    if not executable.is_absolute():
        reject("UPDATE_CONTROL_INVALID", "Runtime executable is not absolute")
    # A command symlink must not bypass admission. Derive the physical target's
    # fixed layout, rather than trusting either an environment root or argv[0].
    executable = executable.resolve(strict=True)
    if executable.name != _host_name(): return None
    if executable.parent.name == "gitgo-host" and executable.parent.parent.name == "bootstrap":
        return executable.parents[2], "bootstrap"
    if (executable.parent.name == "gitgo-host" and executable.parents[1].name == "internal"
            and executable.parents[2].name == "active"):
        return executable.parents[3], "active"
    return None


def assert_external_resource(path: Path):
    """Projects must never become payload files that a software update replaces."""
    if not getattr(sys, "frozen", False) or not _ownership_supported(): return
    physical = physical_installation(Path(sys.executable))
    if physical is None: return
    root = physical[0]
    canonical = path.expanduser().resolve()
    if canonical == root or canonical.is_relative_to(root) or root.is_relative_to(canonical):
        reject("UPDATE_PROTECTED_PATHS", "Project workspace must stay outside the managed installation")


@contextmanager
def installed_runtime(role: str | None):
    """Every managed frozen Host/daemon/tool role owns a lease before imports."""
    if not getattr(sys, "frozen", False) or not _ownership_supported():
        yield; return
    if os.name == "nt" and role in {"windows-scope", "windows-job-lease"}:
        # Fixed trusted transport/guardian only: no application, configuration
        # or project imports. Payload roles acquire their own admission below;
        # the supervisor retains explicitly inherited kernel sharing leases.
        yield; return
    physical = physical_installation(Path(sys.executable))
    if physical is None:
        yield; return
    root, position = physical
    store = open_managed(root)
    if position == "bootstrap":
        if role not in {"managed-launch", "managed-recover", "managed-front-lease", "unix-scope"}:
            reject("UPDATE_BOOTSTRAP_ROLE", "Stable bootstrap accepts only managed lifecycle roles")
        yield; return
    # Probe candidate code only while an inherited, locally pinned exclusive
    # descriptor protects the CHECK_NEW state; environment alone is not enough.
    if role in {"update-data-probe", "managed-capabilities", "update-startup-probe"}:
        ledger = store._ledger()
        if ledger["transaction"] is not None and ledger["transaction"]["state"] == "CHECK_NEW":
            if os.name == "nt":
                # Windows byte-range locks are not inherited authority. Health
                # executes a verified private snapshot, never the active path.
                reject("UPDATE_PROBE_LEASE", "Candidate probes must use the private Windows snapshot")
            with _inherited_probe_lease(store): yield
            return
    for path in protected_install_paths():
        if path == root or path.is_relative_to(root) or root.is_relative_to(path):
            reject("UPDATE_PROTECTED_PATHS", "Runtime profile must stay outside the managed installation")
    with store.runtime_lease() as (_active, descriptor), retain_descriptor(descriptor):
        yield


@contextmanager
def _inherited_probe_lease(store):
    import fcntl
    import stat
    raw = os.environ.get("GITGO_INSTALL_LEASE_FDS", "")
    if len(raw) > 128: reject("UPDATE_PROBE_LEASE", "Candidate probe lease is unavailable")
    identity = store.registration["lock_identities"]["runtime.lock"]
    for value in raw.split(","):
        if not value.isdigit() or len(value) > 10: continue
        fd = int(value)
        if fd < 3: continue
        try:
            info = os.fstat(fd)
            valid = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o600
                     and descriptor_identity(fd) == identity)
            if not valid: continue
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: continue
        with retain_descriptor(fd): yield
        return
    reject("UPDATE_PROBE_LEASE", "Candidate probe lacks its installation's exclusive lease")


def bootstrap_main(role: str, arguments: list[str]) -> int:
    if role not in {"managed-launch", "managed-recover"} or (role == "managed-recover" and arguments):
        reject("UPDATE_BOOTSTRAP_ROLE", "Invalid managed lifecycle arguments")
    if len(arguments) > 128 or sum(len(arg) for arg in arguments) > 65536:
        reject("UPDATE_BOOTSTRAP_ROLE", "Managed launch arguments exceed their limit")
    physical = physical_installation(Path(sys.executable))
    if physical is None or physical[1] != "bootstrap":
        reject("UPDATE_BOOTSTRAP_ROLE", "Managed lifecycle requires the stable installed bootstrap")
    store = open_managed(physical[0])
    pending = store._ledger()["transaction"]
    if role == "managed-recover" or (pending is not None and pending["state"] not in TERMINAL):
        result = store.recover()
        if result.status not in {"idle", "committed", "rolled_back"}:
            reject("UPDATE_RECOVERY_REQUIRED", "Installation must recover before launching")
        if role == "managed-recover":
            from backend.core.protocol_io import dump_protocol_json
            print(dump_protocol_json({"status": result.status, "version": result.version}), flush=True)
            return 0
    from backend.core.process_control import spawn_managed, finish_scope, terminate_tree
    from .handoff import launch_session, consume_request, SESSION_ENV, HANDOFF_EXIT
    from .handoff import strategy
    if strategy(store) == "after_reboot":
        from .deferred import consume_after_reboot
        deferred = consume_after_reboot(store)
        if deferred is not None:
            from backend.core.protocol_io import dump_protocol_json
            print(dump_protocol_json({"update_status": deferred.status, "version": deferred.version,
                                      "error_code": deferred.error_code}), file=sys.stderr, flush=True)
            if deferred.status == "restart_required":
                print("A confirmed Gitgo replacement is waiting. Restart macOS manually, then launch Gitgo again. No program has been replaced in this boot.", file=sys.stderr, flush=True)
            pending = store._ledger()["transaction"]
            if pending is not None and pending["state"] not in TERMINAL:
                reject("UPDATE_RECOVERY_REQUIRED", "Deferred attempt requires ledger recovery before launch")
    for _ in range(8):  # Bound this launcher lifecycle, never automatic retry.
        with launch_session(store) as nonce:
            with store.runtime_lease() as (active, descriptor), retain_descriptor(descriptor):
                env = {**os.environ, "GITGO_INSTALL_ROOT": str(active), SESSION_ENV: nonce or "",
                       "GITGO_HOST_EXECUTABLE": str(active / "internal/gitgo-host" / _host_name())}
                proc = spawn_managed([str(active / ("gitgo.exe" if os.name == "nt" else "gitgo")), *arguments], cwd=str(active), env=env)
                try:
                    proc.wait()
                    finish_scope(proc)
                except BaseException:
                    terminate_tree(proc)
                    raise
            # All launcher leases close before the updater obtains exclusivity.
            if proc.returncode != HANDOFF_EXIT: return int(proc.returncode)
            if nonce is None:
                reject("UPDATE_HANDOFF_STALE", "This launch has no update handoff session")
            result = consume_request(store, nonce, proc.returncode, getattr(proc, "_gitgo_cleanup", None))
            if result is None: return 2
            from backend.core.protocol_io import dump_protocol_json
            print(dump_protocol_json({"update_status": result.status, "version": result.version,
                                      "error_code": result.error_code}), flush=True)
            if result.status == "restart_required":
                print("Confirmation saved. Restart macOS manually, then use the installed Gitgo command. Signature, package and exclusive runtime locks will be checked again before replacement.", file=sys.stderr, flush=True)
                return 0
            recovered = store.recover()
            if recovered.status not in {"idle", "committed", "rolled_back"}:
                reject("UPDATE_RECOVERY_REQUIRED", "Update must recover before restarting")
        # Same terminal, new session; never replay task/approval/smoke arguments.
        arguments = []
    reject("UPDATE_RETAIN_LIMIT", "Too many confirmed restarts in one launcher lifecycle")


def frontend_lease(nonce: str) -> int:
    """Hold a real lease for a direct Bun/Node frontend before user-state I/O."""
    from .validation import text
    from backend.core.protocol_io import dump_protocol_json
    text(nonce, r"[0-9a-f]{32}", 32)
    physical = physical_installation(Path(sys.executable))
    if physical is None or physical[1] != "bootstrap":
        reject("UPDATE_BOOTSTRAP_ROLE", "Frontend admission requires the stable bootstrap")
    store = open_managed(physical[0])
    for path in protected_install_paths():
        if path == store.root or path.is_relative_to(store.root) or store.root.is_relative_to(path):
            reject("UPDATE_PROTECTED_PATHS", "Frontend profile and projects must stay outside the installation")
    with store.runtime_lease() as (_active, descriptor), retain_descriptor(descriptor):
        print(dump_protocol_json({"schema_version": 1, "nonce": nonce,
            "version": store._ledger()["current"]["version"]}), flush=True)
        sys.stdin.buffer.read(1)  # EOF on frontend exit releases this lease.
    return 0
