"""Real installation layout/leases; fake seed probe tests are not frozen acceptance."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from backend.core.installation_lease import retain_descriptor, runtime_descriptors
from backend.core.update import UpdateError
from backend.core.update import installation
from backend.core.update.installation import ManagedStore, initialize_managed, installed_runtime, open_managed
from backend.core.update.persistence import atomic_control, read_control
from backend.core.update.transaction import InstallationStore, SimulationStore
from tests.test_update_transaction import build_fixture, archive_fixture, Crash, crash_at, inspect_health, signing
from tests.test_update_discovery import NOW, POLICY

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Managed ownership/lease adapter currently POSIX-only")


@pytest.fixture
def managed(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.resolve()
    protected = base / "protected"; protected.mkdir(mode=0o700)
    (protected / "project.txt").write_bytes(b"protected project")
    seed = base / "source-package"; build_fixture(seed, version="1.0.0-alpha.2")
    root = base / "new installation"; root.mkdir(mode=0o700)
    def probe(host, profile):
        product = json.loads((host.parents[2] / "product.json").read_bytes())
        contract = json.loads((host.parents[2] / "update-data-contract.json").read_bytes())
        return {"runtime": {**{k: product[k] for k in ("product_id", "version", "platform", "architecture")},
                            "data_compatibility": contract["data_compatibility"]},
                "cleanup": {"complete": False}}
    monkeypatch.setattr(installation, "probe_managed_host", probe)
    store = initialize_managed(root, seed, protected_paths=(protected,))
    yield store, seed, protected
    assert (protected / "project.txt").read_bytes() == b"protected project"


def test_managed_and_simulation_modes_cannot_adopt_one_another(managed):
    store, _, protected = managed
    assert store.registration["schema_version"] == 3
    assert store.registration["simulation"] is False
    assert store.registration["updates_enabled"] is False
    assert (store.root / "bootstrap/gitgo-host/gitgo-host").is_file()
    assert "managed-launch" in (store.root / "gitgo").read_text()
    assert not (store.root / ".gitgo-install-owner").exists()
    assert open_managed(store.root).recover().status == "idle"
    with pytest.raises(UpdateError): SimulationStore(store.root, protected_paths=(protected,))
    assert store.apply(None, None, None, None, None).status == "disabled"


def test_initial_install_never_overwrites_nonempty_or_legacy_root(managed):
    store, seed, protected = managed
    before = (store.root / "control/ledger.json").read_bytes()
    with pytest.raises(UpdateError): initialize_managed(store.root, seed, protected_paths=(protected,))
    assert (store.root / "control/ledger.json").read_bytes() == before
    legacy = store.root.parent / "legacy"; legacy.mkdir(mode=0o700)
    (legacy / ".gitgo-install-owner").write_text("gitgo-install-v1")
    with pytest.raises(UpdateError): initialize_managed(legacy, seed, protected_paths=(protected,))
    assert (legacy / ".gitgo-install-owner").read_text() == "gitgo-install-v1"


@pytest.mark.parametrize("slot", ["bootstrap", "launcher", "lock", "enable"])
def test_changed_bootstrap_launcher_or_lock_is_not_an_installation_authority(managed, slot):
    store, _, _ = managed
    if slot == "bootstrap": (store.root / "bootstrap/gitgo-host/gitgo-host").write_bytes(b"modified bootstrap")
    if slot == "launcher": (store.root / "gitgo").write_text("#!/bin/sh\nexit 0\n")
    if slot == "lock":
        path = store.root / "control/runtime.lock"; path.rename(path.with_suffix(".old")); path.write_bytes(b""); path.chmod(0o600)
    if slot == "enable":
        value = read_control(store.root / "control/registration.json"); value["updates_enabled"] = True
        atomic_control(store.root / "control/registration.json", value)
    with pytest.raises(UpdateError): open_managed(store.root)
    assert store.recover().status == "recovery_required"


def test_source_environment_cannot_claim_a_managed_lease(managed, monkeypatch):
    store, _, _ = managed
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("GITGO_INSTALL_ROOT", str(store.root))
    monkeypatch.setenv("GITGO_INSTALL_LEASE_FDS", "9999")
    with installed_runtime(None): assert runtime_descriptors() == ()
    assert store.recover().status == "idle"


def test_frozen_host_role_admission_holds_runtime_lock_before_initialization(managed, monkeypatch):
    store, _, _ = managed
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(store.root / "active/internal/gitgo-host/gitgo-host"))
    for role in (None, "daemon", "tool-runner", "python", "unix-scope"):
        with installed_runtime(role):
            assert len(runtime_descriptors()) == 1
            assert store.recover().status == "busy"
        assert runtime_descriptors() == ()
    assert store.recover().status == "idle"


def pending_transaction(store, signing):
    candidate = store.root.parent / "candidate"; build_fixture(candidate)
    download = store.root.parent / "download"; download.mkdir(mode=0o700)
    raw, detached, archive = archive_fixture(candidate, download, signing)
    # Unit state-machine exercise only: production ManagedStore.apply has no
    # activation API. Do not execute these placeholder components.
    with pytest.raises(Crash):
        InstallationStore.apply(store, POLICY, signing[1], raw, detached, archive,
            health_check=inspect_health, clock=lambda: NOW, checkpoint=crash_at("check_new_intent"))


def test_pending_installation_blocks_ordinary_roles_and_env_only_probes(managed, monkeypatch, signing):
    store, _, _ = managed
    pending_transaction(store, signing)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(store.root / "active/internal/gitgo-host/gitgo-host"))
    monkeypatch.setenv("GITGO_INSTALL_LEASE_FDS", "9999")
    with pytest.raises(UpdateError, match="UPDATE_RECOVERY_REQUIRED"):
        with installed_runtime(None): pytest.fail("pending candidate admitted")
    with pytest.raises(UpdateError, match="UPDATE_PROBE_LEASE"):
        with installed_runtime("update-data-probe"): pytest.fail("env alone admitted a candidate probe")
    assert store.recover().status == "rolled_back"


def test_candidate_probe_requires_actual_pinned_exclusive_descriptor(managed, monkeypatch, signing):
    store, _, _ = managed
    pending_transaction(store, signing)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(store.root / "active/internal/gitgo-host/gitgo-host"))
    with store._operation():
        descriptors = runtime_descriptors()
        assert len(descriptors) == 1
        monkeypatch.setenv("GITGO_INSTALL_LEASE_FDS", str(descriptors[0]))
        with installed_runtime("update-data-probe"):
            assert runtime_descriptors() == descriptors
        # Exceptions in probe code must not be swallowed into another admission.
        with pytest.raises(OSError, match="probe body"):
            with installed_runtime("update-data-probe"): raise OSError("probe body")
    assert runtime_descriptors() == ()
    assert store.recover().status == "rolled_back"


def test_owned_worker_and_supervisor_retain_lease_after_launching_parent_releases_it(managed):
    from backend.core.process_control import spawn_managed, finish_scope, terminate_tree
    store, _, _ = managed
    proc = None
    try:
        with store.runtime_lease() as (_active, descriptor), retain_descriptor(descriptor):
            script = "import os,sys; fd=int(os.environ['GITGO_INSTALL_LEASE_FDS']); os.fstat(fd); print('lease-retained',flush=True); sys.stdin.read()"
            proc = spawn_managed([sys.executable, "-B", "-c", script], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert proc.stdout.readline().strip() == "lease-retained"
        assert runtime_descriptors() == ()
        assert store.recover().status == "busy"
        proc.stdin.close(); proc.wait(timeout=10); finish_scope(proc)
        assert store.recover().status == "idle"
    finally:
        if proc is not None:
            terminate_tree(proc)
            for stream in (proc.stdout, proc.stderr): stream.close()


def test_bootstrap_recovers_before_launch_and_rejects_arbitrary_roles(managed, monkeypatch, signing):
    store, _, _ = managed
    pending_transaction(store, signing)
    monkeypatch.setattr(sys, "executable", str(store.root / "bootstrap/gitgo-host/gitgo-host"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    before = (store.root / "control/ledger.json").read_bytes()
    with pytest.raises(UpdateError): installation.bootstrap_main("managed-recover", ["unexpected"])
    assert (store.root / "control/ledger.json").read_bytes() == before
    with pytest.raises(UpdateError):
        with installed_runtime("python"): pytest.fail("stable helper admitted arbitrary execution")
    assert installation.bootstrap_main("managed-recover", []) == 0
    assert store._ledger()["transaction"]["state"] == "ROLLED_BACK"


def test_bad_seed_runtime_cannot_publish_an_initial_installation(managed, monkeypatch):
    store, seed, protected = managed
    root = store.root.parent / "another"; root.mkdir(mode=0o700)
    monkeypatch.setattr(installation, "probe_managed_host", lambda *_: {"runtime": {"version": "99.0.0"}})
    with pytest.raises(UpdateError): initialize_managed(root, seed, protected_paths=(protected,))
    assert not list(root.iterdir())


def test_frozen_install_role_publishes_only_a_new_owned_command(managed, monkeypatch):
    store, seed, protected = managed
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(seed / "internal/gitgo-host/gitgo-host"))
    root, bins = store.root.parent / "role-install", store.root.parent / "role-bin"
    result = installation.install_from_frozen_package(str(root), str(bins), [str(protected)])
    assert result["managed"] is True and result["updates_enabled"] is False
    assert (bins / "gitgo").is_symlink() and (bins / "gitgo").readlink() == root / "gitgo"
    before = (root / "control/ledger.json").read_bytes()
    with pytest.raises(UpdateError): installation.install_from_frozen_package(str(root), str(bins), [])
    assert (root / "control/ledger.json").read_bytes() == before


def test_install_role_cannot_write_into_registered_projects_or_foreign_command(managed, monkeypatch):
    from backend.core.config import ConfigManager
    store, seed, protected = managed
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(seed / "internal/gitgo-host/gitgo-host"))
    config = ConfigManager.default_path(); config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"projects": [{"workspace_path": str(protected)}]}))
    root, bins = protected / "new-program", store.root.parent / "role-bin"
    with pytest.raises(UpdateError): installation.install_from_frozen_package(str(root), str(bins), [])
    assert not root.exists() and not bins.exists()
    bins.mkdir(mode=0o700); (bins / "gitgo").write_text("existing unrelated program")
    root = store.root.parent / "role-install"
    with pytest.raises(UpdateError): installation.install_from_frozen_package(str(root), str(bins), [])
    assert not root.exists()
    assert (bins / "gitgo").read_text() == "existing unrelated program"


def test_managed_runtime_and_project_service_reject_data_inside_program(managed, monkeypatch):
    from backend.core.application import ApplicationServices, OperationError
    store, _, _ = managed
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(store.root / "active/internal/gitgo-host/gitgo-host"))
    workspace = store.root / "active/user-project"
    with pytest.raises(OperationError, match="outside the managed"):
        ApplicationServices().project_create(name="unsafe", workspace_path=str(workspace), workspace_mode="create_new")
    assert not workspace.exists()
    monkeypatch.setenv("GITGO_CONFIG_PATH", str(store.root / "active/config.json"))
    with pytest.raises(UpdateError, match="UPDATE_PROTECTED_PATHS"):
        with installed_runtime(None): pytest.fail("profile inside active admitted")


def test_status_reports_registered_layout_without_claiming_update_activation(managed, monkeypatch):
    from backend.core.update.status import update_status
    store, _, _ = managed
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(store.root / "active/internal/gitgo-host/gitgo-host"))
    result = update_status()
    assert result["enabled"] is False and result["can_apply"] is False
    assert "MANAGED_INSTALLATION_REQUIRED" not in result["blockers"]
    assert "UPDATE_HANDOFF_REQUIRED" in result["blockers"]


def test_frontend_keeper_holds_real_lease_until_input_eof(managed, monkeypatch, capsys):
    store, _, _ = managed
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(store.root / "bootstrap/gitgo-host/gitgo-host"))
    class Input:
        @property
        def buffer(self): return self
        def read(self, count):
            assert count == 1
            assert len(runtime_descriptors()) == 1
            assert store.recover().status == "busy"
            return b""
    monkeypatch.setattr(sys, "stdin", Input())
    assert installation.frontend_lease("a" * 32) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"schema_version": 1, "nonce": "a" * 32, "version": "1.0.0-alpha.2"}
    assert store.recover().status == "idle"
    with pytest.raises(UpdateError): installation.frontend_lease("not-a-nonce")


@pytest.mark.parametrize("format", ["modern", "legacy", "single-legacy"])
def test_protection_reads_all_local_repository_roles_without_migrating_config(tmp_path_factory, monkeypatch, format):
    from backend.core.config import ConfigManager, Config, ProjectConfig
    from backend.models import RepoNode, FileAccess
    base = tmp_path_factory.resolve()
    workspace, release, trial = (base / name for name in ("workspace", "release", "trial"))
    config = ConfigManager.default_path()
    if format == "modern":
        ConfigManager.save(Config(projects=[ProjectConfig(name="test",
            workspace=RepoNode(file_access=FileAccess(path=str(workspace))),
            release=RepoNode(file_access=FileAccess(path=str(release))),
            trial=RepoNode(file_access=FileAccess(path=str(trial))))]))
    else:
        config.parent.mkdir(parents=True, exist_ok=True)
        value = {"workspace_path": str(workspace), "backup_path": str(release)}
        config.write_text(json.dumps({"projects": [value]} if format == "legacy" else value))
    before = config.read_bytes()
    monkeypatch.setattr(ConfigManager, "load", lambda *_args, **_kwargs: pytest.fail("protection must not migrate/load config"))
    paths = installation.protected_install_paths()
    assert workspace in paths and release in paths
    if format == "modern": assert trial in paths
    assert config.read_bytes() == before
    assert not (config.parent / "migrations").exists()


def test_protection_does_not_treat_remote_paths_as_local_but_retains_local_ssh_key(tmp_path_factory):
    base = tmp_path_factory.resolve()
    key = base / "key"
    paths = installation._local_repository_paths({"workspace": {"file_access": {"kind": "local", "path": str(base / "work")}},
        "release": {"file_access": {"kind": "ssh", "path": "/", "key_path": str(key)}},
        "trial": {"file_access": {"kind": "smb", "path": "relative-remote-path"}}})
    assert paths == [base / "work", key]
    with pytest.raises(UpdateError): installation._local_repository_paths({"workspace": {"file_access": {"kind": "unknown", "path": "relative-local"}}})


@pytest.mark.skipif(not os.environ.get("GITGO_MANAGED_TEST_STAGE"), reason="Requires an explicitly built new managed-capable Unix package")
def test_actual_frozen_managed_install_can_boot_without_source_or_python(tmp_path_factory):
    base = tmp_path_factory.resolve()
    protected = base / "protected"; protected.mkdir(mode=0o700)
    (protected / "sentinel").write_bytes(b"keep")
    root = base / "managed"
    bins = base / "commands"
    profile = base / "profile"; profile.mkdir(mode=0o700)
    stage = Path(os.environ["GITGO_MANAGED_TEST_STAGE"]).resolve(strict=True)
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": str(base), "HOME": str(profile),
           "GITGO_CONFIG_PATH": str(profile / "config.json"), "GITGO_STATE_HOME": str(profile / "state"),
           "GITGO_LLM_CONFIG_PATH": str(profile / "providers.json"), "GITGO_LLM_SECRET_PATH": str(profile / "secrets.json")}
    result = subprocess.run(["/bin/bash", str(stage / "install.sh"), "--managed", "--no-path-update",
        "--install-root", str(root), "--bin-dir", str(bins)], capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    store = open_managed(root)
    assert not list(profile.iterdir())
    # Actual stable bootstrap -> actual installed Dashboard -> explicit version.
    result = subprocess.run([str(bins / "gitgo"), "--version"], capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stderr
    assert "Gitgo " + store._ledger()["current"]["version"] in result.stdout
    assert store.recover().status == "idle"


    assert (protected / "sentinel").read_bytes() == b"keep"
    assert not list(profile.iterdir())

    # Real stable keeper holds a real kernel lease, not an injected health mock.
    import selectors
    keeper = subprocess.Popen([str(root / "bootstrap/gitgo-host/gitgo-host"), "--gitgo-internal-role",
        "managed-front-lease", "--nonce", "b" * 32], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(keeper.stdout, selectors.EVENT_READ)
            assert selector.select(60), "actual stable keeper did not report admission"
        report = json.loads(keeper.stdout.readline())
        assert report == {"schema_version": 1, "nonce": "b" * 32, "version": store._ledger()["current"]["version"]}
        assert store.recover().status == "busy"
        keeper.stdin.close(); keeper.wait(timeout=30)
        assert keeper.returncode == 0
        assert store.recover().status == "idle"
    finally:
        if keeper.poll() is None: keeper.kill(); keeper.wait(timeout=10)
        keeper.stdout.close(); keeper.stderr.close()

    # Both actual compiled Bun and actual Node companion perform early admission.
    commands = [[str(root / "active/gitgo"), "--runtime-diagnostics"]]
    if os.environ.get("GITGO_MANAGED_TEST_NODE"):
        commands.append([os.environ["GITGO_MANAGED_TEST_NODE"], str(root / "active/dashboard/dist/cli.js"), "--runtime-diagnostics"])
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["runtime"] in {"bun", "node"}
        assert store.recover().status == "idle"
        assert not list(profile.iterdir())

    # A changed owned registration must block before launch-session/profile I/O.
    registration = read_control(root / "control/registration.json")
    atomic_control(root / "control/registration.json", {**registration, "updates_enabled": True})
    try:
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
            assert result.returncode != 0
            assert "could not safely start" in result.stderr
            assert not list(profile.iterdir())
    finally: atomic_control(root / "control/registration.json", registration)
    assert store.recover().status == "idle"

    # Actual ordinary Host initialization also retains the installation lease.
    # Only local, read-only product/status operations; never a model request.
    host = subprocess.Popen([str(root / "active/internal/gitgo-host/gitgo-host")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    buffered = bytearray()
    def event():
        deadline = time.monotonic() + 60
        with selectors.DefaultSelector() as selector:
            selector.register(host.stdout, selectors.EVENT_READ)
            while b"\n" not in buffered:
                assert len(buffered) <= 65536, "Host protocol report exceeded its limit"
                assert selector.select(max(0, deadline - time.monotonic())), "actual Host handshake timed out"
                chunk = os.read(host.stdout.fileno(), 4096)
                assert chunk, "actual Host exited before its protocol report"
                buffered.extend(chunk)
        line, _, remainder = buffered.partition(b"\n")
        buffered[:] = remainder
        return json.loads(line)
    try:
        assert event()["type"] == "host_started"
        assert store.recover().status == "busy"
        for operation in ("product.version", "update.status"):
            host.stdin.write((json.dumps({"protocol_version": 1, "type": "request", "request_id": operation,
                "operation": operation, "arguments": {}}) + "\n").encode()); host.stdin.flush()
            response = event()
            assert response["request_id"] == operation and "error" not in response
            assert response["result"]["version"] == store._ledger()["current"]["version"]
            if operation == "update.status":
                assert response["result"]["can_apply"] is False
                assert "MANAGED_INSTALLATION_REQUIRED" not in response["result"]["blockers"]
        host.stdin.close(); host.wait(timeout=30)
        assert host.returncode == 0
    finally:
        if host.poll() is None: host.kill(); host.wait(timeout=10)
        host.stdout.close(); host.stderr.close()
    assert store.recover().status == "idle"
    assert not (profile / "providers.json").exists() and not (profile / "secrets.json").exists()
    assert (protected / "sentinel").read_bytes() == b"keep"


@pytest.mark.skipif(not os.environ.get("GITGO_MANAGED_TEST_STAGE"), reason="Requires an explicitly built new managed-capable Unix package")
def test_actual_frozen_installer_preserves_modern_registered_projects_and_profile(tmp_path_factory):
    base = tmp_path_factory.resolve()
    profile = base / "profile"; profile.mkdir(mode=0o700)
    work, release, trial = (base / name for name in ("work", "release", "trial"))
    for path in (work, release, trial):
        path.mkdir(mode=0o700); (path / "sentinel").write_bytes(b"existing project data")
    config = profile / "config.json"
    config.write_text(json.dumps({"projects": [{"name": "existing", **{
        name: {"file_access": {"kind": "local", "path": str(path)}}
        for name, path in (("workspace", work), ("release", release), ("trial", trial))}}]}))
    config.chmod(0o600)
    before = config.read_bytes()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(profile), "TMPDIR": str(base),
        "GITGO_CONFIG_PATH": str(config), "GITGO_STATE_HOME": str(profile / "state"),
        "GITGO_LLM_CONFIG_PATH": str(profile / "providers.json"), "GITGO_LLM_SECRET_PATH": str(profile / "secrets.json")}
    stage = Path(os.environ["GITGO_MANAGED_TEST_STAGE"]).resolve(strict=True)
    bins = base / "commands"
    rejected_root = release / "unsafe-install"
    result = subprocess.run(["/bin/bash", str(stage / "install.sh"), "--managed", "--no-path-update",
        "--install-root", str(rejected_root), "--bin-dir", str(bins)], capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode != 0
    assert not rejected_root.exists() and not bins.exists()
    root = base / "managed"
    result = subprocess.run(["/bin/bash", str(stage / "install.sh"), "--managed", "--no-path-update",
        "--install-root", str(root), "--bin-dir", str(bins)], capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    store = open_managed(root)
    assert all(str(path) in store.registration["protected_paths"] for path in (work, release, trial))
    result = subprocess.run([str(root / "active/gitgo"), "--runtime-diagnostics"], capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == before
    assert sorted(path.name for path in profile.iterdir()) == ["config.json"]
    assert all((path / "sentinel").read_bytes() == b"existing project data" for path in (work, release, trial))
