"""Protocol fault injection for real release adapters, never platform proof."""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from backend.core.publish.release_transport import ReleaseTransport, drive_release
from backend.core.publish.state import PublishError
from backend.remote.publish_platform import PlatformClient, PlatformTarget, PlatformError


OID = "a" * 40


class Platform:
    def __init__(self, kind):
        self.target = PlatformTarget.parse({"kind": kind, "instance": "https://github.com" if kind == "github" else "https://gitlab.test",
                                           "repository": "team/repo", "account": "alice"})
        self.tag = None
        self.release = None
        self.assets = {}
        self.writes = []
        self.lost_upload = False
        self.fail_upload = False
        self.tamper = False

    def request(self, request):
        path, method = request.url.path, request.method
        assert request.headers["authorization"] == "Bearer test-secret"
        if method != "GET":
            self.writes.append((method, path))
        base = self.target.repo_path
        if self.target.kind == "gitlab":
            base = base.replace("%2F", "/")
            path = path.removeprefix("/api/v4")
        def response(status=200, **data):
            return httpx.Response(status, json=data)
        if path == "/user":
            return response(id=10, login="alice", username="alice")
        if path == base:
            return response(id=20, full_name="team/repo", path_with_namespace="team/repo")
        if path in {base + "/git/commits/" + OID, base + "/repository/commits/" + OID}:
            return response(sha=OID, id=OID)
        if path.startswith(base + "/git/ref/tags/") or method == "GET" and path.startswith(base + "/repository/tags/"):
            return response(404) if self.tag is None else response(object={"type": "commit", "sha": self.tag}, commit={"id": self.tag})
        if method == "POST" and path in {base + "/git/refs", base + "/repository/tags"}:
            payload = json.loads(request.content)
            self.tag = payload.get("sha", payload.get("ref"))
            return response(201)
        if method == "GET" and (path.startswith(base + "/releases/tags/") or path == base + "/releases/1.0.0"):
            if self.target.kind == "github" and self.release and self.release["draft"]:
                return response(404)
            return httpx.Response(200, json=self.release) if self.release else response(404)
        if method == "GET" and path == base + "/releases":
            return httpx.Response(200, json=[self.release] if self.release else [])
        if method == "POST" and path == base + "/releases":
            payload = json.loads(request.content)
            self.release = {"id": 50, "tag_name": payload["tag_name"], "body": payload.get("body"), "draft": payload.get("draft", False),
                "description": payload.get("description"), "assets": payload.get("assets", {}), "html_url": "https://github.com/team/repo/releases/tag/1.0.0"}
            return httpx.Response(201, json=self.release)
        if method == "PATCH" and path == base + "/releases/50":
            self.release["draft"] = False
            return httpx.Response(200, json=self.release)
        if method == "GET" and path == base + "/releases/50/assets":
            return httpx.Response(200, json=[{"id": index + 100, "name": name, "state": "uploaded"} for index, name in enumerate(self.assets)])
        if method == "POST" and path == base + "/releases/50/assets":
            name = request.url.params["name"]
            return self.upload(request, name)
        if method == "GET" and path.startswith(base + "/releases/assets/"):
            index = int(path.rsplit("/", 1)[-1]) - 100
            raw = list(self.assets.values())[index]
            return httpx.Response(200, content=raw + (b"tampered" if self.tamper else b""))
        if path.startswith(base + "/packages/generic/gitgo-publish/1.0.0/"):
            name = path.rsplit("/", 1)[-1]
            if method == "PUT":
                return self.upload(request, name)
            return httpx.Response(200, content=self.assets[name] + (b"tampered" if self.tamper else b"")) if name in self.assets else response(404)
        pytest.fail("Unexpected platform route: " + method + " " + path)

    def upload(self, request, name):
        assert request.headers["accept"] == "application/json"
        assert request.headers["content-type"] == "application/octet-stream"
        if self.fail_upload:
            return httpx.Response(503, json={"error": "injected"})
        self.assets[name] = request.content
        if self.lost_upload:
            self.lost_upload = False
            raise httpx.ReadTimeout("lost upload receipt", request=request)
        return httpx.Response(201, json={"id": 100})


def inputs():
    raw = b"sealed version package"
    sha = hashlib.sha256(raw).hexdigest()
    return ({"version": "1.0.0", "source_oid": OID, "tree_oid": "b" * 40, "sha256": sha},
            [{"name": "release.zip", "raw": raw, "sha256": sha, "size": len(raw)}])


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_release_downloads_every_asset_and_reconcile_does_not_write(kind):
    platform = Platform(kind)
    checkpoints = []
    with PlatformClient(platform.target, "test-secret", transport=httpx.MockTransport(platform.request)) as client:
        transport = ReleaseTransport(client)
        package, assets = inputs()
        record = drive_release(transport, package, assets, {}, lambda r: checkpoints.append(deepcopy(r)))
        assert record["state"] == "published"
        assert record["assets"][0]["state"] == "verified"
        writes = list(platform.writes)
        assert drive_release(transport, package, assets, record, lambda _: None, observe_only=True)["state"] == "published"
        assert platform.writes == writes
    assert any(r["current"] == "upload:release.zip" and r["state"] == "intent" for r in checkpoints)
    assert platform.tag == OID


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_lost_upload_ack_observes_bytes_without_reupload_then_explicit_resume(kind):
    platform = Platform(kind)
    platform.lost_upload = True
    package, assets = inputs()
    record = {}
    with PlatformClient(platform.target, "test-secret", transport=httpx.MockTransport(platform.request)) as client:
        transport = ReleaseTransport(client)
        with pytest.raises(PlatformError, match="UNAVAILABLE"):
            drive_release(transport, package, assets, record, lambda _: None)
        writes = list(platform.writes)
        recovered = drive_release(transport, package, assets, record, lambda _: None, observe_only=True)
        assert recovered["state"] == "partial" and recovered["assets"][0]["state"] == "verified"
        assert platform.writes == writes
        assert drive_release(transport, package, assets, record, lambda _: None)["state"] == "published"
        assert sum("assets" in path or "packages" in path for _, path in platform.writes) == 1


@pytest.mark.parametrize("failure", ["digest", "tag", "extra"])
def test_reconciliation_blocks_tampered_assets_tags_or_unexpected_assets(failure):
    platform = Platform("github")
    package, assets = inputs()
    with PlatformClient(platform.target, "test-secret", transport=httpx.MockTransport(platform.request)) as client:
        transport = ReleaseTransport(client)
        record = drive_release(transport, package, assets, {}, lambda _: None)
        if failure == "digest":
            platform.tamper = True
        elif failure == "tag":
            platform.tag = "c" * 40
        else:
            platform.assets["unexpected"] = b"unreviewed"
        writes = list(platform.writes)
        with pytest.raises(PublishError):
            drive_release(transport, package, assets, record, lambda _: None, observe_only=True)
        assert platform.writes == writes


def test_failed_upload_keeps_github_release_draft_and_no_false_success():
    platform = Platform("github")
    platform.fail_upload = True
    package, assets = inputs()
    with PlatformClient(platform.target, "test-secret", transport=httpx.MockTransport(platform.request)) as client:
        with pytest.raises(PlatformError):
            drive_release(ReleaseTransport(client), package, assets, {}, lambda _: None)
        assert platform.release["draft"] is True and not platform.assets


def test_download_redirect_never_forwards_credentials_or_accepts_unknown_host():
    seen = []
    def route(request):
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/file?signature=secret"})
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=b"verified")
    target = Platform("github").target
    with PlatformClient(target, "test-secret", transport=httpx.MockTransport(route)) as client:
        transport = ReleaseTransport(client)
        assert transport._binary("GET", "https://api.github.com/repos/team/repo/releases/assets/1") == b"verified"
        with pytest.raises(PublishError, match="REDIRECT_DENIED"):
            transport._binary("GET", "https://attacker.invalid/secret", redirect=True)
    assert len(seen) == 2


@pytest.mark.parametrize("failure", ["duplicates", "limit"])
def test_draft_lookup_never_creates_a_release_from_ambiguous_or_truncated_listing(failure):
    platform = Platform("github")
    reads = []
    def route(request):
        assert request.method == "GET"
        if "/releases/tags/" in request.url.path:
            return httpx.Response(404)
        reads.append(int(request.url.params["page"]))
        rows = [{"id": index + 1, "tag_name": "1.0.0" if failure == "duplicates" else "another"} for index in range(100)]
        return httpx.Response(200, json=rows)
    with PlatformClient(platform.target, "test-secret", transport=httpx.MockTransport(route)) as client:
        with pytest.raises(PublishError, match="CONFLICT" if failure == "duplicates" else "LIST_LIMIT"):
            ReleaseTransport(client).release("1.0.0")
    assert len(reads) == (1 if failure == "duplicates" else 10)
