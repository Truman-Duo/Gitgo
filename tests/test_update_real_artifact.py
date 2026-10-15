"""Optional real Unix package preparation; NOT production update acceptance.

Explicitly supply a freshly built archive and its staging directory. Ephemeral
test signatures and injected HTTP streams exercise the real bounded pipeline.
No CDN, production trust, installed-product adoption or model API is involved.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from backend.core.update import CurrentInstallation, DataCompatibility, ManifestVerifier, UpdateError, UpdatePolicy, prepare_update
from backend.core.update.contracts import verify_bundle_contract
from backend.core.update.health import frozen_health_check
from backend.core.update.transport import HttpsTransport
from tests.test_update_manifest import sign_raw


pytestmark = pytest.mark.skipif(
    os.name != "posix" or not os.environ.get("GITGO_UPDATE_TEST_ARCHIVE") or not os.environ.get("GITGO_UPDATE_TEST_STAGE"),
    reason="Requires an explicitly built real Unix archive and its immutable staging directory",
)


class FileStream(httpx.SyncByteStream):
    def __init__(self, path, *, interrupted=False): self.path, self.interrupted = path, interrupted
    def __iter__(self):
        with self.path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                yield chunk
                if self.interrupted: raise OSError("private transport diagnostic must not be exposed")


class BytesStream(httpx.SyncByteStream):
    def __init__(self, data): self.data = data
    def __iter__(self): yield self.data


@pytest.fixture
def artifact(tmp_path_factory):
    archive = Path(os.environ["GITGO_UPDATE_TEST_ARCHIVE"]).resolve(strict=True)
    stage = Path(os.environ["GITGO_UPDATE_TEST_STAGE"]).resolve(strict=True)
    product = json.loads((stage / "product.json").read_bytes())
    build = json.loads((stage / "build-manifest.json").read_bytes())
    contract = json.loads((stage / "update-data-contract.json").read_bytes())
    assert build["source"]["dirty"] is False
    # The fake older identity is only a checker fixture, not an installed
    # product or evidence that two real versions have upgraded successfully.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    current = CurrentInstallation(product["product_id"], "0.0.0", product["platform"], product["architecture"],
        "0.1.0", DataCompatibility.parse(contract["data_compatibility"]), True)
    policy = UpdatePolicy(True, False, "preview", "https://artifact.example.test/manifest.json",
        "https://artifact.example.test/manifest.sig", ("artifact.example.test",))
    with archive.open("rb") as handle: sha = hashlib.file_digest(handle, "sha256").hexdigest()
    wire = {"schema_version": 1, "product_id": product["product_id"], "release_id": "local-artifact-test",
        "channel": "preview", "sequence": 1, "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": product["version"], "platform": product["platform"], "architecture": product["architecture"],
        "archive_format": "tar.gz", "url": "https://artifact.example.test/package.tar.gz",
        "bytes": archive.stat().st_size, "sha256": sha,
        "source": {k: build["source"][k] for k in ("commit", "tree")},
        "data_compatibility": contract["data_compatibility"], "minimum_updater_version": "0.1.0"}
    private = Ed25519PrivateKey.generate()
    verifier = ManifestVerifier({"ephemeral-test": private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)})
    base = tmp_path_factory.resolve()
    staging = base / "staging"; staging.mkdir(mode=0o700)
    protected = base / "protected-data"; protected.mkdir(mode=0o700)
    for name in ("project.txt", "config.json", "state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"):
        (protected / name).write_bytes(("protected-sentinel-" + name).encode())
    before = {p: p.read_bytes() for p in protected.iterdir()}
    yield archive, stage, wire, private, verifier, current, policy, now, staging, (protected,), before
    assert all(p.read_bytes() == value for p, value in before.items())
    with archive.open("rb") as handle: assert hashlib.file_digest(handle, "sha256").hexdigest() == sha


def prepare(artifact, *, fault="none"):
    archive, _, wire, private, verifier, current, policy, now, staging, protected, _ = artifact
    wire = dict(wire)
    if fault == "hash": wire["sha256"] = "0" * 64
    raw = json.dumps(wire).encode("utf-8")
    signature = sign_raw(raw, Ed25519PrivateKey.generate() if fault == "signature" else private)
    calls = []
    def serve(request):
        url = str(request.url); calls.append(url)
        if url == policy.manifest_url: return httpx.Response(200, stream=BytesStream(raw))
        if url == policy.signature_url: return httpx.Response(200, stream=BytesStream(signature))
        assert url == wire["url"]
        return httpx.Response(200, headers={"Content-Length": str(archive.stat().st_size)},
            stream=FileStream(archive, interrupted=fault == "interrupted"))
    transport = HttpsTransport(policy.allowed_hosts, transport=httpx.MockTransport(serve))
    result = prepare_update(policy, current, verifier, staging_parent=staging, protected_paths=protected,
        transport=transport, clock=lambda: now, cancelled=lambda: fault == "cancel")
    return result, calls


def test_actual_archive_passes_complete_readonly_contract_verification(artifact):
    result, calls = prepare(artifact)
    assert result.status == "staged"
    verify_bundle_contract(result.bundle_directory, result.candidate)
    assert len(calls) == 3
    assert result.candidate.source_commit == artifact[2]["source"]["commit"]
    assert (result.bundle_directory / "dashboard/dist/cli.js").is_file()
    assert (result.bundle_directory / "update-probe-result.json").is_file()


@pytest.mark.parametrize("fault,code", [("signature", "UPDATE_SIGNATURE_INVALID"), ("hash", "UPDATE_HASH_MISMATCH"),
    ("cancel", "UPDATE_CANCELLED"), ("interrupted", "UPDATE_TRANSFER_FAILED")])
def test_invalid_or_cancelled_real_package_never_changes_protected_data(artifact, fault, code):
    with pytest.raises(UpdateError, match=code) as exc: prepare(artifact, fault=fault)
    assert "private transport diagnostic" not in str(exc.value)
    assert not list(artifact[8].iterdir())


def test_mac_forkless_health_does_not_enable_unconfined_application_handoff(artifact):
    if artifact[2]["platform"] != "macos": pytest.skip("macOS incomplete-scope boundary")
    result, _ = prepare(artifact)
    verify_bundle_contract(result.bundle_directory, result.candidate)
    profile = artifact[8].parent / "health"; profile.mkdir(mode=0o700)
    assert frozen_health_check(result.bundle_directory, profile).passed is True
    from backend.core.update.handoff import platform_ready
    from types import SimpleNamespace
    # Forkless health is not proof that the ordinary application and all its
    # unrestricted tools have stopped. Preserve the handoff boundary.
    assert platform_ready(SimpleNamespace(registration={"platform": "macos"})) is False
