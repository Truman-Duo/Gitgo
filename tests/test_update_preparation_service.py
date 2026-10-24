"""Manual application boundary using signed fixtures, not runnable products."""
import base64
from datetime import timedelta
import json
import os
from pathlib import Path
import hashlib
import selectors
import shlex
import shutil
import subprocess
import threading
import time

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import httpx
import pytest

from backend.core.application import ApplicationServices, OperationError
from backend.core.update import UpdateError
from backend.core.update.bootstrap_contract import CURRENT_API
from backend.core.update import installation
from backend.core.update.installation import initialize_managed, open_managed
from backend.core.update.preparation_service import UpdatePreparationService
from backend.core.update.release_policy import parse_release_policy, POLICY_FILE
from backend.core.update.persistence import read_control, atomic_control
from backend.core.update.tree import tree_digest
from tests.test_update_transaction import build_fixture, refresh_build, archive_fixture
from tests.test_update_manifest import signing, sign_raw, manifest_wire
from tests.test_update_discovery import NOW

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Managed download adapter currently requires POSIX ownership")


def policy_wire(public):
    return {"schema_version": 1, "product_id": "gitgo-terminal", "platform": "macos", "architecture": "arm64",
        "allowed_hosts": ["releases.example.test"],
        "public_keys": [{"key_id": "ephemeral-test", "ed25519_base64": base64.b64encode(public).decode()}],
        "channels": {channel: {"manifest_url": f"https://releases.example.test/{channel}.json",
            "signature_url": f"https://releases.example.test/{channel}.sig"} for channel in ("stable", "preview")}}


class Stream(httpx.SyncByteStream):
    def __init__(self, body, interrupted=False): self.body, self.interrupted = body, interrupted
    def __iter__(self):
        yield self.body
        if self.interrupted: raise OSError("proxy-password-private-diagnostic")


@pytest.fixture
def prep(tmp_path_factory, monkeypatch, signing):
    base = tmp_path_factory.resolve()
    protected = base / "protected"; protected.mkdir(mode=0o700)
    for name in ("project", "config", "state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"):
        (protected / name).write_bytes(("keep-" + name).encode())
    before = {path: path.read_bytes() for path in protected.iterdir()}
    seed = base / "seed"; build_fixture(seed, version="1.0.0-alpha.2")
    public = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    (seed / POLICY_FILE).write_text(json.dumps(policy_wire(public)))
    refresh_build(seed, version="1.0.0-alpha.2")
    def probe(host, profile):
        product = json.loads((host.parents[2] / "product.json").read_bytes())
        contract = json.loads((host.parents[2] / "update-data-contract.json").read_bytes())
        return {"runtime": {**{k: product[k] for k in ("product_id", "version", "platform", "architecture")},
            "data_compatibility": contract["data_compatibility"]}, "cleanup": {"complete": False}}
    monkeypatch.setattr(installation, "probe_managed_host", probe)
    root = base / "managed"; root.mkdir(mode=0o700)
    store = initialize_managed(root, seed, protected_paths=(protected,))
    candidate = base / "candidate"; build_fixture(candidate)
    download = base / "input"; download.mkdir(mode=0o700)
    raw, signature, archive = archive_fixture(candidate, download, signing)
    network = {"raw": raw, "signature": signature, "body": archive.read_bytes(), "calls": [], "fault": "", "asset": None}
    def serve(request):
        url = str(request.url); network["calls"].append(url)
        if url.endswith(".json"): return httpx.Response(200, stream=Stream(network["raw"]))
        if url.endswith(".sig"): return httpx.Response(200, stream=Stream(network["signature"]))
        assert url.endswith("gitgo.tar.gz")
        if network["asset"]: network["asset"]()
        body = b"corrupt package" if network["fault"] == "hash" else network["body"]
        return httpx.Response(200, stream=Stream(body, network["fault"] == "interrupted"))
    def factory(hosts, **options): return httpx_transport(hosts, serve, **options)
    service = UpdatePreparationService(store_loader=lambda: open_managed(root), transport_factory=factory, clock=lambda: NOW)
    active_before = tree_digest(root / "active")
    yield service, store, network, signing, candidate, download
    assert tree_digest(root / "active") == active_before
    assert store._ledger()["current"]["version"] == "1.0.0-alpha.2"
    assert store._ledger()["transaction"] is None
    assert all(path.read_bytes() == value for path, value in before.items())


def httpx_transport(hosts, serve, **options):
    from backend.core.update.transport import HttpsTransport
    return HttpsTransport(hosts, transport=httpx.MockTransport(serve), **options)


def check(prep): return prep[0].check("preview", "1" * 32)
def download(prep, candidate): return prep[0].download("preview", candidate, "2" * 32)


def test_status_never_contacts_release_server_or_initializes_user_configuration(prep):
    service, store, network, *_ = prep
    assert service.status()["status"] == "idle"
    assert network["calls"] == []
    assert store._ledger()["watermarks"] == []
    assert not list((store.root / "control/preparation").iterdir())


def test_check_persists_verified_high_water_but_does_not_download(prep):
    result = check(prep)
    assert result["status"] == "available" and result["latest_version"] == "1.0.0-alpha.3"
    assert result["can_download"] is True and result["can_apply"] is False
    assert prep[2]["calls"] == ["https://releases.example.test/preview.json", "https://releases.example.test/preview.sig"]
    assert len(prep[1]._ledger()["watermarks"]) == 1
    restarted = UpdatePreparationService(store_loader=lambda: open_managed(prep[1].root), clock=lambda: NOW,
        transport_factory=lambda *_a, **_k: pytest.fail("cached status made a network request"))
    assert restarted.status("preview") == result


def test_reviewed_download_is_static_verified_and_never_runs_candidate(prep, monkeypatch):
    selected = check(prep)["candidate"]["candidate_id"]
    def forbidden(*_a, **_k): pytest.fail("preparation executed code or attempted an install")
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(installation.ManagedStore, "apply", forbidden)
    result = download(prep, selected)
    assert result["status"] == "downloaded" and result["can_apply"] is False
    assert result["can_download"] is False
    assert prep[2]["calls"][-1] == "https://releases.example.test/gitgo.tar.gz"
    calls = list(prep[2]["calls"])
    assert download(prep, selected)["status"] == "downloaded"
    assert prep[2]["calls"] == calls  # Already verified receipt is idempotent.


def test_download_requires_exact_reviewed_selection_and_no_arbitrary_root_url_or_key(prep):
    with pytest.raises(UpdateError, match="UPDATE_SELECTION_STALE"): download(prep, "a" * 64)
    assert prep[2]["calls"] == []
    check(prep)
    with pytest.raises(UpdateError, match="UPDATE_SELECTION_STALE"): download(prep, "a" * 64)
    services = ApplicationServices(); services.updates = prep[0]
    for operation, arguments in (("update.check", {"channel": "preview", "operation_id": "f" * 32, "url": "https://other.example.test/file"}),
        ("update.download", {"channel": "preview", "candidate_id": "a" * 64, "operation_id": "f" * 32, "root": str(prep[1].root)})):
        with pytest.raises(OperationError) as error: services.invoke(operation, arguments)
        assert error.value.code == "INVALID_ARGUMENTS"
    assert "update.apply" not in services.operations and "update.enable" not in services.operations


@pytest.mark.parametrize("fault", ["hash", "interrupted"])
def test_broken_download_is_cleaned_but_never_lowers_release_high_water(prep, fault):
    selected = check(prep)["candidate"]["candidate_id"]
    high_water = prep[1]._ledger()["watermarks"]
    prep[2]["fault"] = fault
    with pytest.raises(UpdateError) as exc: download(prep, selected)
    assert "proxy-password" not in str(exc.value)
    assert prep[1]._ledger()["watermarks"] == high_water
    assert [path.name for path in (prep[1].root / "control/preparation").iterdir()] == ["check-preview.json"]


def test_bad_signature_never_saves_receipt_or_changes_watermark(prep):
    prep[2]["raw"] += b" "
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"): check(prep)
    assert prep[1]._ledger()["watermarks"] == []
    assert not list((prep[1].root / "control/preparation").iterdir())


def test_metadata_changes_after_check_do_not_silently_change_reviewed_package(prep):
    selected = check(prep)["candidate"]["candidate_id"]
    prep[2]["raw"], prep[2]["signature"] = b"new unreviewed metadata", b"new signature"
    assert download(prep, selected)["status"] == "downloaded"
    assert len(prep[2]["calls"]) == 3  # Download uses exact cached signed metadata.


def test_expired_selection_requires_new_check_before_any_asset_request(prep):
    selected = check(prep)["candidate"]["candidate_id"]
    calls = list(prep[2]["calls"])
    prep[0]._clock = lambda: NOW + timedelta(days=3)
    assert prep[0].status("preview")["status"] == "stale"
    with pytest.raises(UpdateError, match="UPDATE_FRESHNESS_INVALID"): download(prep, selected)
    assert prep[2]["calls"] == calls


def test_preparation_does_not_hold_exclusive_runtime_or_operation_lock_while_network_waits(prep):
    selected = check(prep)["candidate"]["candidate_id"]
    def callback():
        with prep[1].runtime_session(): pass
        assert prep[1].recover().status == "busy"
        second = UpdatePreparationService(store_loader=lambda: open_managed(prep[1].root), clock=lambda: NOW)
        with pytest.raises(UpdateError, match="UPDATE_BUSY"): second.check("preview", "9" * 32)
    prep[2]["asset"] = callback
    assert download(prep, selected)["status"] == "downloaded"


def test_cancel_before_admission_and_mid_download_leave_program_and_data_unchanged(prep):
    prep[0].cancel("1" * 32)
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"): check(prep)
    assert prep[2]["calls"] == []
    selected = prep[0].check("preview", "3" * 32)["candidate"]["candidate_id"]
    prep[2]["asset"] = lambda: prep[0].cancel("2" * 32)
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"): download(prep, selected)
    assert not list((prep[1].root / "control/preparation").glob("gitgo-update-*"))


def test_close_cancels_pending_host_work_and_rejects_new_jobs(prep):
    selected = check(prep)["candidate"]["candidate_id"]
    prep[2]["asset"] = prep[0].close
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"): download(prep, selected)
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"): prep[0].check("preview", "f" * 32)


def test_changed_prepared_file_or_pinned_policy_is_not_accepted(prep):
    selected = check(prep)["candidate"]["candidate_id"]
    download(prep, selected)
    record = read_control(prep[1].root / "control/preparation/prepared-preview.json")
    bundle = prep[1].root / "control/preparation" / record["directory"] / record["bundle"]
    (bundle / "gitgo").write_bytes(b"changed local payload")
    assert prep[0].status("preview")["status"] == "stale"
    with pytest.raises(UpdateError): download(prep, selected)
    before = list(prep[2]["calls"])
    policy = prep[1].root / "bootstrap" / POLICY_FILE
    policy.chmod(0o600); policy.write_bytes(b"{}")
    with pytest.raises(UpdateError): prep[0].check("preview", "f" * 32)
    assert prep[2]["calls"] == before


def test_replayed_metadata_is_rejected_after_restart(prep):
    check(prep)
    raw, sig, _ = archive_fixture(prep[4], prep[5], prep[3], sequence=1)
    prep[2]["raw"], prep[2]["signature"] = raw, sig
    with pytest.raises(UpdateError, match="UPDATE_REPLAY_REJECTED"): prep[0].check("preview", "f" * 32)


def test_explicit_channels_never_implicitly_promote_preview_to_stable(prep):
    check(prep)
    assert prep[0].status("stable")["status"] == "idle"
    assert prep[0].status("preview")["status"] == "available"
    with pytest.raises(UpdateError, match="UPDATE_SCOPE_MISMATCH"): prep[0].check("stable", "f" * 32)


@pytest.mark.parametrize("change", ["schema", "target", "private", "host", "url", "keys", "channel"])
def test_invalid_publisher_policy_cannot_create_client_trust(signing, change):
    public = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    value = policy_wire(public)
    if change == "schema": value["schema_version"] = True
    if change == "target": value["platform"] = "linux"
    if change == "private": value["private_key"] = "must not be accepted"
    if change == "host": value["allowed_hosts"] = [["bad"]]
    if change == "url": value["channels"]["stable"]["manifest_url"] = "http://releases.example.test/file"
    if change == "keys": value["public_keys"][0]["ed25519_base64"] = "A" * 44
    if change == "channel": value["channels"] = {"automatic": value["channels"]["stable"]}
    with pytest.raises(UpdateError): parse_release_policy(json.dumps(value).encode(), "macos", "arm64")


def test_unmanaged_missing_release_trust_and_invalid_arguments_never_access_network(monkeypatch, tmp_path_factory):
    service = UpdatePreparationService(store_loader=lambda: None,
        transport_factory=lambda *_a, **_k: pytest.fail("unsupported installation contacted release server"))
    with pytest.raises(UpdateError, match="UPDATE_INSTALL_UNMANAGED"): service.check("stable", "f" * 32)
    for channel in ("automatic", {}, None):
        with pytest.raises(UpdateError): service.check(channel, "e" * 32)


def test_missing_publisher_policy_stays_unconfigured_without_network(prep, monkeypatch):
    import backend.core.update.preparation_service as module
    monkeypatch.setattr(module, "installed_release_policy", lambda _store: None)
    assert prep[0].status()["status"] == "unconfigured"
    with pytest.raises(UpdateError, match="UPDATE_TRUST_MISSING"): check(prep)
    assert prep[2]["calls"] == []


def test_clock_expiry_during_metadata_fetch_is_rejected_before_persistence(prep):
    times = iter((NOW, NOW + timedelta(days=3)))
    prep[0]._clock = lambda: next(times)
    with pytest.raises(UpdateError, match="UPDATE_FRESHNESS_INVALID"): check(prep)
    assert prep[1]._ledger()["watermarks"] == []
    assert not list((prep[1].root / "control/preparation").iterdir())


def test_current_version_reports_up_to_date_without_package_download(prep):
    raw, signature, _archive = archive_fixture(prep[4], prep[5], prep[3], version="1.0.0-alpha.2")
    prep[2]["raw"], prep[2]["signature"] = raw, signature
    result = check(prep)
    assert result["status"] == "up_to_date" and result["candidate"] is None
    assert result["latest_version"] == "1.0.0-alpha.2" and result["can_download"] is False
    assert len(prep[2]["calls"]) == 2


def test_valid_signed_archive_with_bad_component_contract_is_never_marked_prepared(prep):
    (prep[4] / "gitgo").unlink()
    raw, signature, archive = archive_fixture(prep[4], prep[5], prep[3])
    prep[2].update(raw=raw, signature=signature, body=archive.read_bytes())
    selected = check(prep)["candidate"]["candidate_id"]
    with pytest.raises(UpdateError, match="UPDATE_PACKAGE_CONTRACT"): download(prep, selected)
    assert not list((prep[1].root / "control/preparation").glob("gitgo-update-*"))
    assert not (prep[1].root / "control/preparation/prepared-preview.json").exists()


def test_changed_receipt_cannot_reference_or_cleanup_external_data(prep):
    selected = check(prep)["candidate"]["candidate_id"]
    download(prep, selected)
    path = prep[1].root / "control/preparation/prepared-preview.json"
    value = read_control(path); value["directory"] = "../../protected"
    atomic_control(path, value)
    assert prep[0].status("preview")["status"] == "stale"
    with pytest.raises(UpdateError): download(prep, selected)


def test_replaced_preparation_lock_is_rejected_before_network(prep):
    path = prep[1].root / "control/preparation.lock"
    path.rename(path.with_suffix(".old")); path.write_bytes(b""); path.chmod(0o600)
    with pytest.raises(UpdateError): check(prep)
    assert prep[2]["calls"] == []


def test_native_host_request_cancel_reaches_the_preparation_job(prep):
    import io
    from backend.core.native_host import NativeHost
    host = NativeHost(stdout=io.StringIO()); host.services.updates = prep[0]
    started, release = threading.Event(), threading.Event()
    outcome = []
    selected = check(prep)["candidate"]["candidate_id"]
    def asset():
        started.set(); assert release.wait(10)
    prep[2]["asset"] = asset
    def request():
        try:
            host._dispatch({"request_id": "real-wire-id", "operation": "update.download",
                "arguments": {"channel": "preview", "candidate_id": selected, "operation_id": "f" * 32}})
        except OperationError as error: outcome.append(error.code)
    worker = threading.Thread(target=request)
    try:
        worker.start(); assert started.wait(10)
        assert host._cancel("real-wire-id") == {"cancelled": True}
        release.set(); worker.join(timeout=10)
        assert not worker.is_alive() and outcome == ["UPDATE_CANCELLED"]
    finally:
        release.set(); worker.join(timeout=10); host.close()


@pytest.mark.parametrize("api", [True, *range(1, CURRENT_API + 2)])
def test_managed_probe_accepts_only_current_integer_bootstrap_api(tmp_path_factory, api):
    from backend.core.update.health import probe_managed_host
    from backend.core.update.runtime_contract import data_probe
    base = tmp_path_factory.resolve()
    profile = base / "profile"; profile.mkdir(mode=0o700)
    report = {"schema_version": 1, "managed_api": api, "runtime": data_probe("a" * 32)}
    # A synthetic report-only executable, not frozen-runtime acceptance.
    fmt = json.dumps(report).replace('"' + "a" * 32 + '"', '"%s"') + "\n"
    host = base / "report-only"
    host.write_text("#!/bin/sh\nprintf " + shlex.quote(fmt) + ' "$4"\n'); host.chmod(0o700)
    if type(api) is not int or api != CURRENT_API:
        with pytest.raises(UpdateError, match="UPDATE_HEALTH_FAILED"): probe_managed_host(host, profile)
    else:
        result = probe_managed_host(host, profile)
        assert result["runtime"]["memory_sqlite_check"] is True
        assert type(result["cleanup"]["complete"]) is bool


@pytest.mark.skipif(not os.environ.get("GITGO_MANAGED_TEST_STAGE") or not os.environ.get("GITGO_UPDATE_TEST_ARCHIVE"),
    reason="Requires actual fresh managed-capable stage and its immutable full archive")
def test_actual_frozen_host_uses_pinned_public_trust_and_rejects_wrong_version_real_archive(tmp_path_factory, signing):
    """Actual frozen protocol, injected HTTP stream, no candidate execution.

    The signed manifest deliberately claims a higher version than the archive.
    Correct refusal proves static contract validation, NOT a two-version update.
    """
    from backend.core.update.tree import inventory
    base = tmp_path_factory.resolve()
    stage = Path(os.environ["GITGO_MANAGED_TEST_STAGE"]).resolve(strict=True)
    archive = Path(os.environ["GITGO_UPDATE_TEST_ARCHIVE"]).resolve(strict=True)
    seed = base / "seed"; shutil.copytree(stage, seed, symlinks=True); seed.chmod(0o700)
    product = json.loads((seed / "product.json").read_bytes())
    from backend.core.update.version import Version
    claimed_version = str(Version.parse(product["version"]).core[0] + 1) + ".0.0"
    public = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if (seed / POLICY_FILE).exists(): (seed / POLICY_FILE).chmod(0o600)
    policy = {**policy_wire(public), "platform": product["platform"], "architecture": product["architecture"]}
    (seed / POLICY_FILE).write_text(json.dumps(policy)); (seed / POLICY_FILE).chmod(0o400)
    build = json.loads((seed / "build-manifest.json").read_bytes())
    build["files"] = [{"path": item["path"], **({"link": item["link"]} if item["kind"] == "symlink" else
        {"bytes": item["bytes"], "sha256": item["sha256"]})} for item in inventory(seed)
        if item["kind"] != "directory" and item["path"] != "build-manifest.json"]
    (seed / "build-manifest.json").write_text(json.dumps(build))
    profile = base / "profile"; profile.mkdir(mode=0o700)
    protected = base / "project"; protected.mkdir(mode=0o700)
    (protected / "sentinel").write_bytes(b"existing project")
    root = base / "managed"; root.mkdir(mode=0o700)
    store = initialize_managed(root, seed, protected_paths=(protected, profile))
    before = tree_digest(root / "active")
    with archive.open("rb") as handle: archive_sha = hashlib.file_digest(handle, "sha256").hexdigest()
    contract = json.loads((seed / "update-data-contract.json").read_bytes())
    wire = manifest_wire(bytes=archive.stat().st_size, sha256=archive_sha, source={key: build["source"][key] for key in ("commit", "tree")},
        data_compatibility=contract["data_compatibility"], version=claimed_version,
        platform=product["platform"], architecture=product["architecture"])
    raw = json.dumps(wire).encode(); signature = sign_raw(raw, signing[0])
    # Debug role is test-only privileged instrumentation, not a request-facing
    # transport/key override. NativeHost subsequently serves its real protocol.
    script = """import base64,json,sys,httpx
from datetime import datetime,timezone
from backend.core.native_host import NativeHost
from backend.core.update.transport import HttpsTransport
data=json.loads(sys.stdin.readline())
class Bytes(httpx.SyncByteStream):
 def __init__(self,value): self.value=value
 def __iter__(self): yield self.value
class File(httpx.SyncByteStream):
 def __iter__(self):
  with open(data['archive'],'rb') as handle:
   while chunk:=handle.read(65536): yield chunk
def serve(request):
 if str(request.url).endswith('.json'): return httpx.Response(200,stream=Bytes(base64.b64decode(data['raw'])))
 if str(request.url).endswith('.sig'): return httpx.Response(200,stream=Bytes(base64.b64decode(data['signature'])))
 return httpx.Response(200,stream=File())
host=NativeHost()
host.services.updates._transport_factory=lambda hosts,**options: HttpsTransport(hosts,transport=httpx.MockTransport(serve),**options)
host.services.updates._clock=lambda: datetime.fromisoformat(data['clock'])
raise SystemExit(host.serve_forever())
"""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(profile), "TMPDIR": str(base),
        "GITGO_CONFIG_PATH": str(profile / "config.json"), "GITGO_STATE_HOME": str(profile / "state"),
        "GITGO_LLM_CONFIG_PATH": str(profile / "providers.json"), "GITGO_LLM_SECRET_PATH": str(profile / "secrets.json")}
    proc = subprocess.Popen([str(root / "active/internal/gitgo-host/gitgo-host"), "--gitgo-internal-role", "python", "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    buffered = bytearray()
    def event():
        deadline = time.monotonic() + 90
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while b"\n" not in buffered:
                assert len(buffered) <= 65536 and selector.select(max(0, deadline - time.monotonic())), "frozen update protocol timed out"
                chunk = os.read(proc.stdout.fileno(), 4096)
                assert chunk, "frozen update protocol ended"
                buffered.extend(chunk)
        line, _, remainder = buffered.partition(b"\n"); buffered[:] = remainder
        return json.loads(line)
    def request(operation, arguments):
        proc.stdin.write((json.dumps({"protocol_version": 1, "type": "request", "request_id": operation,
            "operation": operation, "arguments": arguments}) + "\n").encode()); proc.stdin.flush()
        return event()
    try:
        proc.stdin.write((json.dumps({"raw": base64.b64encode(raw).decode(), "signature": base64.b64encode(signature).decode(),
            "archive": str(archive), "clock": NOW.isoformat()}) + "\n").encode()); proc.stdin.flush()
        assert event()["type"] == "host_started"
        initial = request("update.status", {"channel": "preview"})["result"]
        assert initial["can_check"] is True and initial["status"] == "idle"
        checked = request("update.check", {"channel": "preview", "operation_id": "a" * 32})["result"]
        assert checked["status"] == "available" and checked["latest_version"] == claimed_version
        response = request("update.download", {"channel": "preview", "operation_id": "b" * 32,
            "candidate_id": checked["candidate"]["candidate_id"]})
        assert response["error"]["code"] == "UPDATE_PACKAGE_CONTRACT"
        proc.stdin.close(); proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None: proc.kill(); proc.wait(timeout=10)
        proc.stdout.close(); proc.stderr.close()
    assert tree_digest(root / "active") == before and store.recover().status == "idle"
    assert store._ledger()["transaction"] is None
    assert not list((root / "control/preparation").glob("gitgo-update-*"))
    assert (protected / "sentinel").read_bytes() == b"existing project"
    assert not (profile / "providers.json").exists() and not (profile / "secrets.json").exists()
    with archive.open("rb") as handle: assert hashlib.file_digest(handle, "sha256").hexdigest() == archive_sha
