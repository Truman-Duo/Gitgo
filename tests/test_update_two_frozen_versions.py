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

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("GITGO_UPDATE_PREVIOUS_STAGE")
    or not os.environ.get("GITGO_UPDATE_CANDIDATE_STAGE"), reason="Requires two fresh actual Linux frozen bundles")


def refresh_inventory(root):
    path = root / "build-manifest.json"
    build = json.loads(path.read_bytes())
    build["files"] = [{"path": item["path"], **({"link": item["link"]} if item["kind"] == "symlink"
        else {"bytes": item["bytes"], "sha256": item["sha256"]})} for item in inventory(root)
        if item["kind"] != "directory" and item["path"] != "build-manifest.json"]
    path.write_text(json.dumps(build)); path.chmod(0o400)


@pytest.fixture
def actual_lab(tmp_path_factory, signing, monkeypatch):
    from datetime import datetime, timezone, timedelta
    base = tmp_path_factory.resolve()
    profile = base / "profile"; profile.mkdir(mode=0o700)
    protected = base / "protected"; protected.mkdir(mode=0o700)
    sentinels = {name: ("keep-" + name).encode() for name in ("project", "worktree", "config", "db", "db-wal", "db-shm", "secrets")}
    for name, data in sentinels.items(): (protected / name).write_bytes(data)
    seed = base / "seed"
    shutil.copytree(Path(os.environ["GITGO_UPDATE_PREVIOUS_STAGE"]).resolve(strict=True), seed, symlinks=True)
    seed.chmod(0o700)
    candidate = base / "candidate"
    shutil.copytree(Path(os.environ["GITGO_UPDATE_CANDIDATE_STAGE"]).resolve(strict=True), candidate, symlinks=True)
    candidate.chmod(0o700)
    old_product = json.loads((seed / "product.json").read_bytes())
    product = json.loads((candidate / "product.json").read_bytes())
    assert product["platform"] == old_product["platform"] == "linux"
    assert Version.parse(product["version"]) > Version.parse(old_product["version"])
    key = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = {"schema_version": 1, "product_id": "gitgo-terminal", "platform": "linux", "architecture": product["architecture"],
        "allowed_hosts": ["releases.example.test"], "public_keys": [{"key_id": "ephemeral-test", "ed25519_base64": __import__("base64").b64encode(key).decode()}],
        "channels": {"preview": {"manifest_url": "https://releases.example.test/manifest.json", "signature_url": "https://releases.example.test/manifest.sig"}}}
    policy_path = seed / "release-trust.json"
    policy_path.write_text(json.dumps(policy)); policy_path.chmod(0o400)
    refresh_inventory(seed)
    root = base / "managed"; root.mkdir(mode=0o700)
    store = initialize_managed(root, seed, protected_paths=(protected, profile))
    before = tree_digest(root / "active")
    # Each test publishes its own signed candidate copy, including a deliberate
    # startup fault when requested. Keys never leave this isolated test process.
    def prepare(broken=False):
        if broken:
            (candidate / "gitgo").chmod(0o700)
            (candidate / "gitgo").write_bytes(b"not-an-executable-test-fault")
            refresh_inventory(candidate)
        archive = base / "candidate.tar.gz"
        with tarfile.open(archive, "w:gz") as package: package.add(candidate, arcname="release")
        contract = json.loads((candidate / "update-data-contract.json").read_bytes())
        build = json.loads((candidate / "build-manifest.json").read_bytes())
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with archive.open("rb") as handle: archive_sha = hashlib.file_digest(handle, "sha256").hexdigest()
        raw, signature = signed({"schema_version": 1, "product_id": "gitgo-terminal", "release_id": "lab-two-version",
            "channel": "preview", "sequence": 1, "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": product["version"], "platform": "linux", "architecture": product["architecture"],
            "archive_format": "tar.gz", "url": "https://releases.example.test/package.tar.gz",
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
        result = subprocess.run([str(root / "gitgo"), "--version"], env=env, capture_output=True, text=True, timeout=90)
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
