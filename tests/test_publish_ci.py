import hashlib
import httpx
import pytest
from backend.remote.publish_ci import ProjectCI
from backend.remote.publish_platform import PlatformClient, PlatformTarget, PlatformError
from backend.core.storage import StorageRuntime
from backend.core.publish.release_artifacts import ReleaseArtifacts

OID = "a" * 40
TARGET = PlatformTarget("github", "https://github.com", "fixture/repository", "owner")


def responses(raw):
    run = {"id":10,"run_attempt":2,"head_sha":OID,"repository":{"id":5},"head_repository":{"id":5},
           "workflow_id":7,"path":".github/workflows/build.yml","status":"completed","conclusion":"success",
           "run_started_at":"2026-11-08T00:00:00Z"}
    artifact = {"id":20,"name":"native-build","size_in_bytes":len(raw),"expired":False,
        "digest":"sha256:"+hashlib.sha256(raw).hexdigest(),"created_at":"2026-11-08T00:01:00Z",
        "workflow_run":{"id":10,"repository_id":5,"head_repository_id":5,"head_sha":OID}}
    jobs = {"jobs":[{"id":30,"run_id":10,"run_attempt":2,"name":"build","status":"completed","conclusion":"success"}]}
    return run, artifact, jobs


def client_for(run, artifact, jobs, observed, raw):
    def handler(request):
        observed.append(request)
        path = request.url.path
        if request.url.host == "productionresultssa1.blob.core.windows.net":
            return httpx.Response(200, content=raw)
        if path == "/user": return httpx.Response(200,json={"id":3,"login":"owner"})
        if path == "/repos/fixture/repository": return httpx.Response(200,json={"id":5,"full_name":"fixture/repository"})
        if path.endswith("/attempts/2"): return httpx.Response(200,json=run)
        if path.endswith("/actions/runs/10"): return httpx.Response(200,json=run)
        if path.endswith("/attempts/2/jobs"): return httpx.Response(200,json=jobs)
        if path.endswith("/artifacts/20"): return httpx.Response(200,json=artifact)
        if path.endswith("/artifacts/20/zip"):
            return httpx.Response(302, headers={"location":"https://productionresultssa1.blob.core.windows.net/data?signed=private"})
        raise AssertionError(path)
    return PlatformClient(TARGET,"fixture-secret",transport=httpx.MockTransport(handler))


def test_ci_source_attempt_digest_and_redirect_credentials_are_bound_before_artifact_acquisition(tmp_path_factory):
    raw = b"synthetic-archive-body"
    run, artifact, jobs = responses(raw)
    observed = []
    workspace=tmp_path_factory/"workspace"; workspace.mkdir()
    with client_for(run,artifact,jobs,observed,raw) as client, StorageRuntime(workspace,state_home=tmp_path_factory/"state") as storage:
        ci = ProjectCI(client)
        candidate = ci.artifact(ci.run(10,2,OID),20)
        assert ci.validate(candidate,OID)["jobs"][0]["id"] == 30
        store=ReleaseArtifacts(storage,"release")
        try:
            descriptor=ci.download(candidate,store)
            assert b"".join(store.chunks(descriptor)) == raw
        finally: store.close()
    assert observed[-2].headers["authorization"] == "Bearer fixture-secret"
    assert "authorization" not in observed[-1].headers and "cookie" not in observed[-1].headers


@pytest.mark.parametrize("change,code", [
    ({"expired":True},"ARTIFACT_EXPIRED"),
    ({"created_at":"2026-11-07T00:00:00Z"},"ATTEMPT_MISMATCH"),
    ({"digest":None},"DIGEST_REQUIRED"),
    ({"workflow_run":{"id":99}},"SOURCE_MISMATCH"),
])
def test_old_attempt_expired_or_unbound_artifacts_never_become_current_source(change, code):
    run,artifact,jobs=responses(b"bytes")
    artifact.update(change)
    with client_for(run,artifact,jobs,[],b"bytes") as client:
        ci=ProjectCI(client)
        with pytest.raises(PlatformError,match=code): ci.artifact(ci.run(10,2,OID),20)


def test_another_source_or_failed_ci_is_not_a_successful_preparation():
    run,artifact,jobs=responses(b"bytes")
    with client_for(run,artifact,jobs,[],b"bytes") as client:
        ci=ProjectCI(client)
        candidate=ci.artifact(ci.run(10,2,OID),20)
        jobs["jobs"][0]["conclusion"]="failure"
        with pytest.raises(PlatformError,match="NOT_READY"): ci.validate(candidate,OID)
        run["head_sha"]="b"*40
        with pytest.raises(PlatformError,match="SOURCE_MISMATCH"): ci.run(10,2,OID)


def test_ci_parallel_runner_occupancy_is_separate_from_observed_wall_time_and_unknown_is_preserved():
    run,artifact,jobs=responses(b"bytes")
    run["created_at"]="2026-11-07T23:59:30Z"
    jobs["jobs"][0].update(started_at="2026-11-08T00:00:00Z",completed_at="2026-11-08T00:01:00Z")
    jobs["jobs"].append({**jobs["jobs"][0],"id":31,"name":"parallel-build"})
    with client_for(run,artifact,jobs,[],b"bytes") as client:
        ci=ProjectCI(client);candidate=ci.artifact(ci.run(10,2,OID),20)
        assert ci.validate(candidate,OID)["timing"]=={"queue_seconds":30.,"runner_occupied_seconds":120.,"observed_job_span_seconds":60.}
        jobs["jobs"][1].pop("completed_at")
        timing=ci.validate(candidate,OID)["timing"]
        assert timing["runner_occupied_seconds"] is None and timing["observed_job_span_seconds"] is None
