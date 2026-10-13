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
from .persistence import filesystem_identity, sync_directory, read_control
from .staging import _staging_parent
from .transaction import InstallationStore, TransactionResult, _initialize_store, TERMINAL
from .tree import inventory, tree_digest, sync_tree
from .validation import UpdateError, digest, fields, json_object, number, reject


class ManagedStore(InstallationStore):
    SCHEMA = 2
    SIMULATION = False
    EXTRA_FIELDS = frozenset({"bootstrap", "updates_enabled"})

    @classmethod
    def _provision_extra(cls, root, seed):
        bootstrap = root / "bootstrap"
        bootstrap.mkdir(mode=0o700)
        shutil.copytree(seed / "internal/gitgo-host", bootstrap / "gitgo-host", symlinks=True)
        (bootstrap / "gitgo-host").chmod(0o700)
        sync_tree(bootstrap)
        # Stable wrapper is installed once, not supplied by a remote candidate.
        wrapper = root / "gitgo"
        content = "#!/bin/sh\nexec " + shlex.quote(str(bootstrap / "gitgo-host/gitgo-host")) + ' --gitgo-internal-role managed-launch "$@"\n'
        with wrapper.open("x", encoding="utf-8") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        wrapper.chmod(0o700)
        descriptor = os.open(wrapper, os.O_RDONLY | os.O_NOFOLLOW)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        return {"bootstrap": {"directory_identity": filesystem_identity(bootstrap),
                              "tree_sha256": tree_digest(bootstrap),
                              "launcher_sha256": _file_digest(wrapper)}, "updates_enabled": False}

    def _validate_extra(self, value):
        if type(value["updates_enabled"]) is not bool or value["updates_enabled"] is not False:
            reject("UPDATE_CONTROL_INVALID", "This managed installation does not support activation yet")
        bootstrap = fields(value["bootstrap"], {"directory_identity", "tree_sha256", "launcher_sha256"})
        identity = fields(bootstrap["directory_identity"], {"device", "inode"})
        number(identity["device"], minimum=0, maximum=2**64 - 1)
        number(identity["inode"], maximum=2**64 - 1)
        digest(bootstrap["tree_sha256"]); digest(bootstrap["launcher_sha256"])
        _staging_parent(self.root / "bootstrap", self.protected_paths)
        if (identity != filesystem_identity(self.root / "bootstrap") or identity["device"] != value["device"]
                or tree_digest(self.root / "bootstrap") != bootstrap["tree_sha256"]
                or _file_digest(self.root / "gitgo") != bootstrap["launcher_sha256"]):
            reject("UPDATE_CONTROL_INVALID", "Stable bootstrap or launcher changed")

    def apply(self, policy, verifier, raw, detached, archive, *, clock=None, checkpoint=None):
        # No injected callable health checks in the managed mode. Activation
        # and locally pinned release trust are a separate, not-yet-open API.
        return TransactionResult("disabled")


def _file_digest(path):
    import hashlib
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 4096):
            reject("UPDATE_CONTROL_INVALID", "Stable launcher ownership is invalid")
        return hashlib.file_digest(handle, "sha256").hexdigest()


def initialize_managed(root: Path, seed: Path, *, protected_paths: tuple[Path, ...]) -> ManagedStore:
    """New local installation only; validate package and actual bootstrap API."""
    if os.name != "posix":
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
        build = _metadata(seed, "build-manifest.json", records)
        contract = _metadata(seed, "update-data-contract.json", records)
        compatibility = DataCompatibility.parse(contract["data_compatibility"])
        identity = SimpleNamespace(**{key: product[key] for key in ("product_id", "version", "platform", "architecture")},
            source_commit=build["source"]["commit"], source_tree=build["source"]["tree"], data_compatibility=compatibility)
        verify_bundle_contract(seed, identity)
        with tempfile.TemporaryDirectory(prefix="gitgo-managed-incoming-", dir=root.parent) as temporary:
            temporary = Path(temporary).resolve()
            private_seed = temporary / "package"
            shutil.copytree(seed, private_seed, symlinks=True); private_seed.chmod(0o700)
            if inventory(private_seed) != before or inventory(seed) != before:
                reject("UPDATE_INSTALL_INVALID", "Package changed during private preparation")
            probe = temporary / "probe"; probe.mkdir(mode=0o700)
            report = probe_managed_host(private_seed / "internal/gitgo-host/gitgo-host", probe)["runtime"]
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
    if os.name != "posix" or not isinstance(root, Path) or root != root.resolve(strict=True):
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
        fd = os.open(config, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024 or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o022):
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
        directory.mkdir(mode=0o700)
    return missing


def install_from_frozen_package(root_value: str, bin_value: str, protected_values: list[str]):
    """Explicit initial-install role. No overwrite, PATH profile edits or adoption."""
    if os.name != "posix" or not getattr(sys, "frozen", False):
        reject("UPDATE_INSTALL_INVALID", "Managed installer requires a packaged POSIX Host")
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
    command = bin_dir / "gitgo"
    if command.exists() or command.is_symlink():
        reject("UPDATE_INSTALL_INVALID", "Public command already exists; choose a separate command directory")
    if len(protected_values) > 128: reject("UPDATE_PROTECTED_PATHS", "Too many protected workspace paths")
    _create_private_directories(root)
    _create_private_directories(bin_dir)
    # Never publish a public symlink before final registration and validation.
    store = initialize_managed(root, seed, protected_paths=protected)
    _new_path(str(bin_dir))
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
    if executable.name != "gitgo-host": return None
    if executable.parent.name == "gitgo-host" and executable.parent.parent.name == "bootstrap":
        return executable.parents[2], "bootstrap"
    if (executable.parent.name == "gitgo-host" and executable.parents[1].name == "internal"
            and executable.parents[2].name == "active"):
        return executable.parents[3], "active"
    return None


def assert_external_resource(path: Path):
    """Projects must never become payload files that a software update replaces."""
    if not getattr(sys, "frozen", False) or os.name != "posix": return
    physical = physical_installation(Path(sys.executable))
    if physical is None: return
    root = physical[0]
    canonical = path.expanduser().resolve()
    if canonical == root or canonical.is_relative_to(root) or root.is_relative_to(canonical):
        reject("UPDATE_PROTECTED_PATHS", "Project workspace must stay outside the managed installation")


@contextmanager
def installed_runtime(role: str | None):
    """Every managed frozen Host/daemon/tool role owns a lease before imports."""
    if not getattr(sys, "frozen", False) or os.name != "posix":
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
    if role in {"update-data-probe", "managed-capabilities"}:
        ledger = store._ledger()
        if ledger["transaction"] is not None and ledger["transaction"]["state"] == "CHECK_NEW":
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
                     and {"device": info.st_dev, "inode": info.st_ino} == identity)
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
    with store.runtime_lease() as (active, descriptor), retain_descriptor(descriptor):
        env = {**os.environ, "GITGO_INSTALL_ROOT": str(active),
               "GITGO_HOST_EXECUTABLE": str(active / "internal/gitgo-host/gitgo-host")}
        proc = spawn_managed([str(active / "gitgo"), *arguments], cwd=str(active), env=env)
        try:
            proc.wait()
            finish_scope(proc)
            return int(proc.returncode)
        except BaseException:
            terminate_tree(proc)
            raise


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
