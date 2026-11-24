"""Product admission, real project CAS and controlled platform fault injection.

The mocked remote is protocol evidence, not GitHub/GitLab or terminal QA.
"""
from copy import deepcopy
import threading
import time

import httpx
import pytest

from backend.core.publish.state import read_ledger
from backend.core.publish.release_journal import ACTIVE_RELEASE
from tests.test_publish_commands import setup, call
from tests.test_publish_formal import formal_setup, prepare, command, protocol
import tests.test_publish_release_transport as wire
import backend.remote.publish_platform as platform_module
from backend.core.tools.publish_service import build_publish_tools


@pytest.fixture
def packaged(formal_setup, monkeypatch):
    protocol(monkeypatch)
    item = prepare(formal_setup)
    item = command(formal_setup, "verify", item)["item"]
    item = command(formal_setup, "execute", item)["item"]
    result = call(formal_setup, "package", "controller-package", item_id=item["id"],
                  expected_revision=item["revision"], version="1.0.0")
    assert "package_id" in result, result
    try:
        yield formal_setup, result["item"], result["package_id"]
    finally:
        formal_setup[0].publish_runtime.close()


def remote(monkeypatch, package, kind="github", route=None):
    value = wire.Platform(kind)
    monkeypatch.setattr(wire, "OID", package["source_oid"])
    constructor = platform_module.PlatformClient
    calls = []
    def client(target, token):
        calls.append(target)
        return constructor(target, token, transport=httpx.MockTransport(route or value.request))
    monkeypatch.setattr(platform_module, "PlatformClient", client)
    monkeypatch.setattr("backend.remote.publish_accounts.resolve_secret", lambda target:"test-secret")
    return value, calls


def plan(packaged, platform, request="controller-plan"):
    context, item, package_id = packaged
    result = call(context, "release.plan", request, item_id=item["id"], expected_revision=item["revision"],
                  package_id=package_id, target=platform.target.descriptor())
    assert "item" in result, result
    return result["item"]


def latest(context, item_id):
    return read_ledger(context[2])[0]["items"][item_id]


def settle(context, item_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        item = latest(context, item_id)
        if item["release_controller"]["job"]["state"] not in ACTIVE_RELEASE:
            return item
        time.sleep(.01)
    pytest.fail("Product release worker did not settle")


def run(context, item, through="prepared", request="controller-run"):
    return call(context, "release.run", request, item_id=item["id"], expected_revision=item["revision"], through=through)


def test_ci_selection_download_is_reused_and_new_attempt_invalidates_saved_success(packaged, monkeypatch):
    import hashlib
    import io
    import zipfile
    context, original, package_id = packaged
    package=original["packages"][0]
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,"w") as archive: archive.writestr("native/application.bin",b"CI-built bytes")
    body=raw.getvalue()
    platform=wire.Platform("github")
    run_row={"id":10,"run_attempt":2,"head_sha":package["source_oid"],"repository":{"id":20},"head_repository":{"id":20},
             "workflow_id":30,"path":".github/workflows/build.yml","status":"completed","conclusion":"success",
             "run_started_at":"2026-11-08T00:00:00Z"}
    artifact={"id":40,"name":"native-build","size_in_bytes":len(body),"digest":"sha256:"+hashlib.sha256(body).hexdigest(),
        "expired":False,"created_at":"2026-11-08T00:01:00Z","workflow_run":{"id":10,"repository_id":20,"head_repository_id":20,"head_sha":package["source_oid"]}}
    downloads=[]
    def route(request):
        path=request.url.path
        base=platform.target.repo_path
        if path in {base+"/actions/runs/10",base+"/actions/runs/10/attempts/2"}: return httpx.Response(200,json=run_row)
        if path == base+"/actions/runs": return httpx.Response(200,json={"workflow_runs":[run_row]})
        if path == base+"/actions/runs/10/artifacts": return httpx.Response(200,json={"artifacts":[artifact],"total_count":1})
        if path == base+"/actions/artifacts/40": return httpx.Response(200,json=artifact)
        if path == base+"/actions/runs/10/attempts/2/jobs":
            return httpx.Response(200,json={"jobs":[{"id":50,"run_id":10,"run_attempt":2,"name":"build","status":"completed","conclusion":"success"}]})
        if path == base+"/actions/artifacts/40/zip":
            downloads.append(path); return httpx.Response(200,content=body)
        return platform.request(request)
    remote(monkeypatch,package,route=route)
    item=plan(packaged,platform)
    sources=context[0].invoke("publish.release.sources",{"project":context[1].name,"item_id":item["id"],
        "plan_binding":item["release_controller"]["plan"]["binding"]})
    assert sources["rows"][0]["artifact_id"] == 40 and platform.writes == []
    selected=call(context,"release.plan","plan-ci",item_id=item["id"],expected_revision=item["revision"],package_id=package_id,
        target=platform.target.descriptor(),ci_artifacts=[{"run_id":10,"attempt":2,"artifact_id":40}])
    assert "item" in selected, selected
    item=selected["item"]
    run(context,item,request="prepare-ci"); item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"] == "completed", item["release_controller"]
    run(context,item,"draft",request="draft-ci"); item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"] == "completed", item["release_controller"]
    assert len(downloads) == 1 and platform.assets["application.bin"] == b"CI-built bytes"
    run_row["run_attempt"]=3
    before=list(platform.writes)
    run(context,item,"published",request="stale-ci"); item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"] == "failed"
    assert item["release_controller"]["job"]["reason"] == "PUBLISH_CI_ATTEMPT_MISMATCH"
    assert platform.writes == before and len(downloads) == 1
    stale=item["release_controller"]["job"]
    observed=call(context,"release.reconcile","observe-after-ci-rerun",item_id=item["id"],
        expected_revision=item["revision"],job_id=stale["id"])
    assert "item" in observed,observed
    item=settle(context,item["id"])
    job=item["release_controller"]["job"]
    assert job["state"] == "blocked" and job["reason"] == "PUBLISH_RELEASE_INCOMPLETE",job
    assert job["phases"]["draft"]["state"] == "observed"
    assert platform.writes == before and len(downloads) == 1


def test_plan_and_saved_queries_never_open_accounts_or_start_platform_requests(packaged, monkeypatch):
    context, original, _ = packaged
    platform, clients = remote(monkeypatch, original["packages"][0])
    item = plan(packaged, platform)
    assert clients == [] and platform.writes == []
    result = context[0].invoke("publish.release.status", {"project":context[1].name, "item_id":item["id"]})
    assert result["recorded_only"] is True
    assert result["events"] == [] and result["plan"]["capabilities"]["anonymous_delivery_verification"] is True
    assert clients == []
    assert "publish_release_run" not in build_publish_tools(context[1])


def test_preparation_cannot_publish_and_live_command_replay_cannot_start_a_second_worker(packaged, monkeypatch):
    context, original, _ = packaged
    platform, _ = remote(monkeypatch, original["packages"][0])
    item = plan(packaged, platform)
    admitted = run(context, item)
    assert admitted["item"]["release_controller"]["job"]["state"] == "queued"
    finished = settle(context, item["id"])
    assert finished["release_controller"]["job"]["state"] == "completed", finished["release_controller"]
    assert platform.writes == []
    assert run(context, item) == admitted
    assert latest(context, item["id"])["release_controller"]["job"]["id"] == finished["release_controller"]["job"]["id"]


def test_unexpected_adapter_failure_is_terminal_and_keeps_the_exact_job(packaged, monkeypatch):
    context, original, _ = packaged
    platform, _ = remote(monkeypatch, original["packages"][0])
    item = plan(packaged, platform)
    def broken(*args, **kwargs):
        raise TypeError("adapter construction failed")
    monkeypatch.setattr("backend.core.publish.release_transport.ReleaseTransport", broken)
    admitted = run(context, item)
    saved = settle(context, item["id"])
    job = saved["release_controller"]["job"]
    assert job["id"] == admitted["item"]["release_controller"]["job"]["id"]
    assert job["state"] == "failed" and job["reason"] == "PUBLISH_RELEASE_OPERATION_UNCERTAIN"
    assert platform.writes == []


def test_github_draft_stops_before_publication_then_explicit_publish_reuses_uploaded_assets(packaged, monkeypatch):
    context, original, _ = packaged
    platform, _ = remote(monkeypatch, original["packages"][0])
    item = plan(packaged, platform)
    run(context, item, "draft")
    draft = settle(context, item["id"])
    assert draft["release_controller"]["job"]["state"] == "completed", draft["release_controller"]
    assert platform.release["draft"] is True and len(platform.assets) == 3
    uploads = [row for row in platform.writes if row[1].endswith("/assets")]
    run(context, draft, "published", "publish-existing-draft")
    published = settle(context, item["id"])
    assert published["release_controller"]["job"]["state"] == "completed", published["release_controller"]
    assert platform.release["draft"] is False
    assert [row for row in platform.writes if row[1].endswith("/assets")] == uploads
    saved = context[0].invoke("publish.release.status", {"project":context[1].name, "item_id":item["id"], "limit":2})
    assert len(saved["events"]) == 2 and saved["has_more"]
    following = context[0].invoke("publish.release.status", {"project":context[1].name, "item_id":item["id"],
        "job_id":published["release_controller"]["job"]["id"], "after_sequence":saved["next_sequence"], "limit":100})
    assert following["events"][0]["sequence"] == 3
    projected = context[0].invoke("publish.item", {"project":context[1].name, "item_id":item["id"]})["item"]
    assert "records" not in projected["release_controller"]["job"]
    assert "references" not in projected["release_controller"]["plan"]


def test_gitlab_has_no_synthetic_draft_stage_and_keeps_existing_publishing_capability(packaged, monkeypatch):
    context, original, _ = packaged
    platform, _ = remote(monkeypatch, original["packages"][0], "gitlab")
    item = plan(packaged, platform)
    refused = run(context, item, "draft", "unsupported-draft")
    assert refused["error"] == "PUBLISH_RELEASE_PHASE_UNAVAILABLE" and platform.writes == []
    run(context, item, "published", "gitlab-publish")
    saved = settle(context, item["id"])
    assert saved["release_controller"]["job"]["state"] == "completed", saved["release_controller"]
    assert "draft" not in saved["release_controller"]["job"]["phases"]
    assert platform.release["draft"] is False and len(platform.assets) == 3


def test_lost_response_requires_observation_only_recovery_before_an_explicit_resume(packaged, monkeypatch):
    context, original, _ = packaged
    platform, _ = remote(monkeypatch, original["packages"][0])
    platform.lost_upload = True
    item = plan(packaged, platform)
    run(context, item, "published")
    failed = settle(context, item["id"])
    job = failed["release_controller"]["job"]
    assert job["state"] == "needs_reconciliation", job
    assert job["pending_effect"].startswith("upload:")
    writes = deepcopy(platform.writes)
    refused = run(context, failed, "published", "unsafe-resume")
    assert refused["error"] == "PUBLISH_RELEASE_RECONCILIATION_REQUIRED"
    recovery = call(context, "release.reconcile", "observe-release", item_id=item["id"],
                    expected_revision=failed["revision"], job_id=job["id"])
    assert "item" in recovery, recovery
    observed = settle(context, item["id"])
    assert platform.writes == writes
    assert observed["release_controller"]["job"]["state"] == "blocked"
    run(context, observed, "published", "explicit-resume")
    finished = settle(context, item["id"])
    assert finished["release_controller"]["job"]["state"] == "completed", finished["release_controller"]
    assert len([row for row in platform.writes if row[1].endswith("/assets")]) == 3


def test_cancellation_targets_the_same_job_across_progress_and_does_not_delete_a_draft(packaged, monkeypatch):
    context, original, _ = packaged
    platform = wire.Platform("github")
    entered, release = threading.Event(), threading.Event()
    def route(request):
        response = platform.request(request)
        if request.method == "POST" and request.url.path.endswith("/assets"):
            entered.set()
            assert release.wait(5)
        return response
    # Keep the platform instance used by the blocking route and the account
    # target passed to the real HTTP adapter the same.
    actual, _ = remote(monkeypatch, original["packages"][0], route=route)
    actual.request = platform.request
    item = plan(packaged, platform)
    accepted = run(context, item, "published")
    try:
        assert entered.wait(5)
        job_id = accepted["item"]["release_controller"]["job"]["id"]
        cancelled = call(context, "release.cancel", "cancel-exact-job", item_id=item["id"],
                         expected_revision=item["revision"], job_id=job_id)
        assert "item" in cancelled, cancelled
    finally:
        release.set()
    finished = settle(context, item["id"])
    # Cancellation can arrive after the server stored the upload but before
    # its response was verified. Preserve intent and require observation.
    assert finished["release_controller"]["job"]["state"] == "needs_reconciliation", finished["release_controller"]
    assert finished["release_controller"]["job"]["transfer"]["state"] == "stopped"
    assert platform.release["draft"] is True and len(platform.assets) == 1
