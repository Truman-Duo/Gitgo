"""Opt-in Linux transaction tests with TWO actual independently built versions.

Real programs, managed bootstrap and kernel cleanup are used. An injected HTTP
stream supplies ephemeral signed releases; this is not public HTTPS/CDN or
visible-terminal acceptance. No paid provider or daily installation is used.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from backend.core.update import handoff
from backend.core.update.health import isolated_environment
from backend.core.update.installation import initialize_managed, open_managed
from backend.core.update.preparation_service import UpdatePreparationService
from backend.core.update.tree import inventory, tree_digest
from backend.core.update.version import Version
from tests.test_update_manifest import signing, signed
from tests.test_update_transaction import Crash
from tests.test_update_preparation_service import Stream, httpx_transport

pytestmark = pytest.mark.skipif(os.name != "posix" or not os.environ.get("GITGO_UPDATE_PREVIOUS_STAGE")
    or not os.environ.get("GITGO_UPDATE_CANDIDATE_STAGE"), reason="Requires two fresh actual Unix frozen bundles")


def refresh_inventory(root):
    path = root / "build-manifest.json"
    build = json.loads(path.read_bytes())
    build["files"] = [{"path": item["path"], **({"link": item["link"]} if item["kind"] == "symlink"
        else {"bytes": item["bytes"], "sha256": item["sha256"]})} for item in inventory(root)
        if item["kind"] != "directory" and item["path"] != "build-manifest.json"]
    path.write_text(json.dumps(build)); path.chmod(0o400)
    if os.name == "nt": path.chmod(0o600)


@pytest.fixture
def actual_lab(tmp_path_factory, signing, monkeypatch):
    from datetime import datetime, timezone, timedelta
    base = tmp_path_factory.resolve()
    profile = base / "profile"; profile.mkdir(mode=0o700)
    protected = base / "protected"; protected.mkdir(mode=0o700)
    sentinels = {name: ("keep-" + name).encode() for name in ("project", "worktree", "config", "db", "db-wal", "db-shm", "secrets")}
    for name, data in sentinels.items(): (protected / name).write_bytes(data)
    seed = base / "seed"
    if os.name == "nt":
        from backend.core.update.windows_storage import copy_tree
        copy_tree(Path(os.environ["GITGO_UPDATE_PREVIOUS_STAGE"]).resolve(strict=True), seed)
    else: shutil.copytree(Path(os.environ["GITGO_UPDATE_PREVIOUS_STAGE"]).resolve(strict=True), seed, symlinks=True)
    seed.chmod(0o700)
    candidate = base / "candidate"
    if os.name == "nt": copy_tree(Path(os.environ["GITGO_UPDATE_CANDIDATE_STAGE"]).resolve(strict=True), candidate)
    else: shutil.copytree(Path(os.environ["GITGO_UPDATE_CANDIDATE_STAGE"]).resolve(strict=True), candidate, symlinks=True)
    candidate.chmod(0o700)
    old_product = json.loads((seed / "product.json").read_bytes())
    product = json.loads((candidate / "product.json").read_bytes())
    assert product["platform"] == old_product["platform"] == {"darwin": "macos", "win32": "windows", "linux": "linux"}[sys.platform]
    assert Version.parse(product["version"]) > Version.parse(old_product["version"])
    key = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = {"schema_version": 1, "product_id": "gitgo-terminal", "platform": product["platform"], "architecture": product["architecture"],
        "allowed_hosts": ["releases.example.test"], "public_keys": [{"key_id": "ephemeral-test", "ed25519_base64": __import__("base64").b64encode(key).decode()}],
        "channels": {"preview": {"manifest_url": "https://releases.example.test/manifest.json", "signature_url": "https://releases.example.test/manifest.sig"}}}
    policy_path = seed / "release-trust.json"
    policy_path.write_text(json.dumps(policy)); policy_path.chmod(0o400)
    if os.name == "nt": policy_path.chmod(0o600)
    refresh_inventory(seed)
    root = base / "managed"; root.mkdir(mode=0o700)
    store = initialize_managed(root, seed, protected_paths=(protected, profile))
    before = tree_digest(root / "active")
    # Each test publishes its own signed candidate copy, including a deliberate
    # startup fault when requested. Keys never leave this isolated test process.
    def prepare(broken=False, *, lifetime_days=1):
        if broken:
            program = candidate / ("gitgo.exe" if os.name == "nt" else "gitgo")
            program.chmod(0o700)
            program.write_bytes(b"not-an-executable-test-fault")
            refresh_inventory(candidate)
        suffix = "zip" if os.name == "nt" else "tar.gz"
        archive = base / ("candidate." + suffix)
        if os.name == "nt":
            import zipfile
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                for path in candidate.rglob("*"):
                    if path.is_file(): package.write(path, path.relative_to(candidate).as_posix())
        else:
            with tarfile.open(archive, "w:gz") as package: package.add(candidate, arcname="release")
        contract = json.loads((candidate / "update-data-contract.json").read_bytes())
        build = json.loads((candidate / "build-manifest.json").read_bytes())
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with archive.open("rb") as handle: archive_sha = hashlib.file_digest(handle, "sha256").hexdigest()
        raw, signature = signed({"schema_version": 1, "product_id": "gitgo-terminal", "release_id": "lab-two-version",
            "channel": "preview", "sequence": 1, "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(days=lifetime_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": product["version"], "platform": product["platform"], "architecture": product["architecture"],
            "archive_format": suffix, "url": "https://releases.example.test/package." + suffix,
            "bytes": archive.stat().st_size, "sha256": archive_sha,
            "source": {key: build["source"][key] for key in ("commit", "tree")},
            "data_compatibility": contract["data_compatibility"], "minimum_updater_version": "0.1.0"}, signing)
        def serve(request):
            return httpx.Response(200, stream=Stream(raw if str(request.url).endswith(".json") else signature
                if str(request.url).endswith(".sig") else archive.read_bytes()))
        service = UpdatePreparationService(store_loader=lambda: open_managed(root),
            transport_factory=lambda hosts, **options: httpx_transport(hosts, serve, **options))
        selected = service.check("preview", "a" * 32)["candidate"]["candidate_id"]
        service.download("preview", selected, "b" * 32)
        return service, selected
    env = {**isolated_environment(profile), "HOME": str(profile)}
    def boot_version(expected):
        command = ([str(root / "bootstrap/gitgo-host/gitgo-host.exe"), "--gitgo-internal-role", "managed-launch"]
                   if os.name == "nt" else [str(root / "gitgo")])
        result = subprocess.run([*command, "--version"], env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "Gitgo " + expected
    boot_version(old_product["version"])
    yield store, prepare, boot_version, before, old_product["version"], product["version"], env
    assert all((protected / name).read_bytes() == data for name, data in sentinels.items())
    assert not (profile / "providers.json").exists() and not (profile / "secrets.json").exists()


def finish_disposable_handoff():
    """Actual parent-owned cleanup; worker stands in for a confirmed UI exit.

    This tests transaction/real-product startup, not a visible Dashboard or the
    public consume_request physical-bootstrap guard (covered separately).
    """
    from backend.core.process_control import spawn_managed, terminate_tree
    from backend.core.process_output import collect_output
    proc = spawn_managed([sys.executable, "-c", "raise SystemExit(75)"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    collect_output(proc, timeout=10, limit=4096, terminate=lambda: terminate_tree(proc))
    assert proc.returncode == 75 and proc._gitgo_cleanup["complete"] is True
    return proc._gitgo_cleanup


@pytest.mark.skipif(sys.platform != "linux", reason="Actual immediate Linux handoff")
def test_actual_upgrade_and_explicit_rollback_keep_real_program_bootable(actual_lab, monkeypatch):
    store, prepare, boot, before, old_version, new_version, _ = actual_lab
    service, selected = prepare()
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        # Source coordinator only; actual candidate health/cleanup is not mocked.
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "committed", result
    boot(new_version)
    watermarks = store._ledger()["watermarks"]
    rollback = store.rollback_selection()
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_rollback(rollback["rollback_id"], "d" * 32)
        assert handoff._consume_request(store, nonce, 75, finish_disposable_handoff()).status == "rolled_back"
    boot(old_version)
    assert tree_digest(store.root / "active") == before
    assert store._ledger()["watermarks"] == watermarks


@pytest.mark.skipif(sys.platform != "linux", reason="Actual immediate Linux handoff")
def test_actual_bad_frontend_restores_the_actual_predecessor(actual_lab, monkeypatch):
    store, prepare, boot, before, old_version, _, _ = actual_lab
    service, selected = prepare(broken=True)
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        result = handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
        assert result.status == "rolled_back" and result.error_code == "UPDATE_HEALTH_FAILED"
    boot(old_version)
    assert tree_digest(store.root / "active") == before


@pytest.mark.skipif(sys.platform != "linux", reason="Actual immediate Linux handoff")
def test_actual_frozen_bootstrap_recovers_an_interrupted_switch(actual_lab, monkeypatch):
    store, prepare, boot, before, old_version, _, env = actual_lab
    service, selected = prepare()
    save = store._save
    def crash(ledger, state=None, **kwargs):
        result = save(ledger, state, **kwargs)
        if state == "CHECK_NEW": raise Crash()
        return result
    monkeypatch.setattr(store, "_save", crash)
    with handoff.launch_session(store) as nonce:
        monkeypatch.setenv(handoff.SESSION_ENV, nonce)
        service.request_apply("preview", selected, "c" * 32)
        with pytest.raises(Crash): handoff._consume_request(store, nonce, 75, finish_disposable_handoff())
    result = subprocess.run([str(store.root / "bootstrap/gitgo-host/gitgo-host"), "--gitgo-internal-role", "managed-recover"],
        env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "rolled_back"
    boot(old_version)
    assert tree_digest(store.root / "active") == before


@pytest.mark.skipif(sys.platform != "linux", reason="Actual immediate Linux handoff")
def test_actual_dashboard_confirmation_exits_and_bootstrap_applies_then_rolls_back(actual_lab):
    """Actual compiled UI -> actual Host -> physical stable bootstrap.

    Automated hidden PTY is process/input evidence, NOT formal visible terminal
    acceptance. Preparation uses the fixture's signed injected HTTP stream.
    """
    import pty
    import select
    import time
    import fcntl
    import struct
    import termios
    import re
    from tests.terminal_screen import TerminalScreen
    store, prepare, _, before, old_version, new_version, env = actual_lab
    prepare()
    profile = Path(env["GITGO_CONFIG_PATH"]).parent
    # Isolated test preference, no terminal selection dialog or external UI.
    Path(env["GITGO_CONFIG_PATH"]).write_text(json.dumps({"projects": [],
        "launcher": {"terminal": "current", "configured": True}}))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = subprocess.Popen([str(store.root / "gitgo")], stdin=slave, stdout=slave, stderr=slave,
        env={**env, "TERM": "xterm-256color"}, start_new_session=True)
    os.close(slave)
    output = bytearray()
    screen = TerminalScreen(40, 120)
    last_frame = 0
    def wait_for(text, *, after=0, timeout=120):
        nonlocal last_frame
        deadline = time.monotonic() + timeout
        def displayed():
            # Ink can reuse old cells in the middle of words. Stripping ANSI
            # cannot reconstruct a display; apply actual cursor/erase commands.
            if '"update_status"' in text:
                return output[after:].decode("utf-8", "replace")
            # Fullscreen writes can scroll the title offscreen while the
            # version/status remains visible. Keep the reader's bounded actual
            # scrollback, just as the real macOS UI acceptance already does.
            return screen.transcript() if last_frame > after else ""
        while text not in displayed():
            known_states = [value for value in ("Reading local update status", "Update status unavailable",
                "Current version", "Downloaded and verified", "Restart/update is unavailable")
                if value in screen.transcript()]
            codes = sorted(set(re.findall(r"\bUPDATE_[A-Z_]{1,64}\b", screen.transcript())))
            assert time.monotonic() < deadline, f"actual UI did not reach {text!r}; states={known_states}; codes={codes}"
            assert proc.poll() is None, "actual managed UI stopped unexpectedly"
            if select.select([master], [], [], .1)[0]:
                chunk = os.read(master, 65536)
                output.extend(chunk)
                screen.feed(chunk)
                last_frame = len(output)
                assert len(output) < 4 * 1024**2, "bounded test terminal output exceeded limit"
    def send(text): os.write(master, text.encode()); time.sleep(.15)
    def update_panel():
        start = len(output)
        send("/"); send("update"); send("\r")
        wait_for("Gitgo Software Update", after=start)
        # The title renders before the Host's full status/inventory response.
        # While loading, the product correctly ignores channel/apply hotkeys.
        # Wait for actual data instead of assuming a worker finishes in 0.5s.
        wait_for("Current version", after=start)
    try:
        wait_for("Providers")  # Expected first launch without API credentials.
        send("\x1b")
        wait_for("Projects")
        update_panel()
        send("c")  # Select the configured preview channel, local refresh only.
        wait_for("Downloaded and verified")
        send("a"); wait_for("Close this Gitgo session")
        start = len(output); send("\r")
        wait_for('"update_status": "committed"', after=start)
        wait_for("Providers", after=start); send("\x1b")
        wait_for("Projects", after=start)
        assert store._ledger()["current"]["version"] == new_version
        update_panel()
        wait_for("Retained version")
        send("b"); wait_for("restore Gitgo " + old_version)
        start = len(output); send("\r")
        wait_for('"update_status": "rolled_back"', after=start)
        wait_for("Providers", after=start); send("\x1b")
        wait_for("Projects", after=start)
        assert store._ledger()["current"]["version"] == old_version
        assert tree_digest(store.root / "active") == before
    finally:
        # Optional local diagnostics contain only this disposable test profile.
        # Do not publish raw terminal transcripts as release evidence.
        if os.environ.get("GITGO_UPDATE_LAB_LOG"):
            try:
                fd = os.open(os.environ["GITGO_UPDATE_LAB_LOG"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle: handle.write(output)
            except OSError: pass  # Diagnostic failure must never skip cleanup.
        # Directly owned disposable launcher only; EOF tells its owning Linux
        # supervisor to clean the child tree. Never signal a sampled PID/group.
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
        os.close(master)
        deadline = time.monotonic() + 10
        while open_managed(store.root).recover().status == "busy" and time.monotonic() < deadline:
            time.sleep(.1)
        assert open_managed(store.root).recover().status != "busy"
