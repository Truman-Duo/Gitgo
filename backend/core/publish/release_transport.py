"""Fixed-tag GitHub/GitLab releases with downloaded-byte verification.

Every write is preceded by a caller-persisted checkpoint. A reconciliation
invocation only observes; missing effects require a new explicit release call.
"""
import hashlib
import json
from urllib.parse import quote, urlsplit

import httpx

from backend.remote.publish_platform import PlatformError
from .package import MAX_PACKAGE, version_name
from .state import PublishError, digest


def _number(value):
    if type(value) is not int or value <= 0:
        raise PublishError("PUBLISH_RELEASE_RESPONSE_INVALID")
    return value


class ReleaseTransport:
    def __init__(self, client):
        self.client, self.target = client, client.target

    def _get(self, path):
        try:
            return self.client.request("GET", path)[0]
        except PlatformError as exc:
            if exc.code == "PUBLISH_PLATFORM_HTTP_404":
                return None
            raise

    def _binary(self, method, url, *, data=None, allow_absent=False, redirect=False):
        headers = {"Accept": "application/octet-stream" if method == "GET" else "application/json",
                   "Content-Type": "application/octet-stream"}
        request = self.client.client.build_request(method, url, content=data, headers=headers)
        if redirect:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or parsed.hostname not in {"release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}):
                raise PublishError("PUBLISH_RELEASE_REDIRECT_DENIED")
            request.headers.pop("Authorization", None)
            request.headers.pop("Cookie", None)
        try:
            response = self.client.client.send(request, stream=True)
            try:
                if method == "GET" and allow_absent and response.status_code == 404:
                    return None
                if method == "GET" and response.status_code == 302 and not redirect and self.target.instance == "https://github.com":
                    return self._binary("GET", response.headers.get("location", ""), redirect=True)
                if response.status_code not in ((200,) if method == "GET" else (200, 201)):
                    raise PlatformError("PUBLISH_PLATFORM_HTTP_" + str(response.status_code), ambiguous=method != "GET")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > (MAX_PACKAGE if method == "GET" else 2 * 1024 * 1024):
                        raise PlatformError("PUBLISH_RELEASE_ASSET_LIMIT", ambiguous=method != "GET")
                return bytes(raw)
            finally:
                response.close()
        except httpx.HTTPError as exc:
            raise PlatformError("PUBLISH_PLATFORM_UNAVAILABLE", ambiguous=method != "GET") from exc

    def commit(self, oid):
        suffix = "/git/commits/" if self.target.kind == "github" else "/repository/commits/"
        row = self._get(self.target.repo_path + suffix + oid)
        if not isinstance(row, dict) or row.get("sha" if self.target.kind == "github" else "id") != oid:
            raise PublishError("PUBLISH_RELEASE_SOURCE_NOT_REMOTE")

    def tag(self, version):
        suffix = "/git/ref/tags/" if self.target.kind == "github" else "/repository/tags/"
        row = self._get(self.target.repo_path + suffix + quote(version_name(version), safe=""))
        if row is None:
            return None
        if self.target.kind == "github":
            obj = row.get("object", {})
            if obj.get("type") != "commit":
                raise PublishError("PUBLISH_TAG_CONFLICT")
            return obj.get("sha")
        return row.get("commit", {}).get("id")

    def create_tag(self, version, oid):
        if self.target.kind == "github":
            self.client.request("POST", self.target.repo_path + "/git/refs", json={"ref": "refs/tags/" + version, "sha": oid}, expected=(201,))
        else:
            self.client.request("POST", self.target.repo_path + "/repository/tags", json={"tag_name": version, "ref": oid}, expected=(201,))

    def release(self, version):
        suffix = "/releases/tags/" if self.target.kind == "github" else "/releases/"
        row = self._get(self.target.repo_path + suffix + quote(version_name(version), safe=""))
        if row is None and self.target.kind == "github":
            # GitHub's tag lookup can omit drafts. Search the authenticated
            # release listing before deciding that creating a release is safe.
            found = []
            for page in range(1, 11):
                rows, _ = self.client.request("GET", self.target.repo_path + "/releases",
                                              params={"per_page": 100, "page": page})
                if not isinstance(rows, list) or any(not isinstance(value, dict) for value in rows):
                    raise PublishError("PUBLISH_RELEASE_RESPONSE_INVALID")
                found.extend(value for value in rows if value.get("tag_name") == version)
                if len(found) > 1:
                    raise PublishError("PUBLISH_RELEASE_CONFLICT")
                if len(rows) < 100:
                    row = found[0] if found else None
                    break
            else:
                raise PublishError("PUBLISH_RELEASE_LIST_LIMIT")
        if row is None:
            return None
        if row.get("tag_name") != version:
            raise PublishError("PUBLISH_RELEASE_CONFLICT")
        if self.target.kind == "github":
            return {"id": _number(row.get("id")), "body": row.get("body", ""), "draft": row.get("draft"),
                    "url": row.get("html_url", "")}
        return {"id": version, "body": row.get("description", ""), "draft": False,
                "url": row.get("_links", {}).get("self", ""), "links": row.get("assets", {}).get("links", [])}

    def create_release(self, version, oid, body, assets):
        payload = {"tag_name": version, "name": version}
        if self.target.kind == "github":
            payload.update(target_commitish=oid, body=body, draft=True, make_latest="false")
        else:
            payload.update(description=body, assets={"links": [{"name": a["name"], "url": self.asset_url(version, a["name"]),
                "link_type": "package"} for a in assets]})
        self.client.request("POST", self.target.repo_path + "/releases", json=payload, expected=(201,))

    def publish(self, release):
        if self.target.kind == "github":
            self.client.request("PATCH", self.target.repo_path + "/releases/" + str(_number(release["id"])), json={"draft": False, "make_latest": "false"})

    def assets(self, release):
        if self.target.kind != "github":
            return {}
        found = {}
        for page in range(1, 11):
            rows, _ = self.client.request("GET", self.target.repo_path + "/releases/" + str(_number(release["id"])) + "/assets",
                                           params={"per_page": 100, "page": page})
            if not isinstance(rows, list):
                raise PublishError("PUBLISH_RELEASE_RESPONSE_INVALID")
            for row in rows:
                name = row.get("name")
                if not isinstance(name, str) or name in found or row.get("state") != "uploaded":
                    raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
                found[name] = _number(row.get("id"))
            if len(rows) < 100:
                return found
        raise PublishError("PUBLISH_RELEASE_ASSET_LIMIT")

    def asset_url(self, version, name):
        return (self.target.api_base + self.target.repo_path + "/packages/generic/gitgo-publish/"
                + quote(version, safe="") + "/" + quote(name, safe=""))

    def download(self, release, version, name, known=None):
        if self.target.kind == "github":
            assets = known if known is not None else self.assets(release)
            if name not in assets:
                return None
            return self._binary("GET", self.target.api_base + self.target.repo_path + "/releases/assets/" + str(assets[name]))
        return self._binary("GET", self.asset_url(version, name), allow_absent=True)

    def upload(self, release, version, asset, raw):
        if self.target.kind == "github":
            base = "https://uploads.github.com" if self.target.instance == "https://github.com" else self.target.instance + "/api/uploads"
            url = base + self.target.repo_path + "/releases/" + str(_number(release["id"])) + "/assets?name=" + quote(asset["name"], safe="")
            self._binary("POST", url, data=raw)
        else:
            self._binary("PUT", self.asset_url(version, asset["name"]), data=raw)


def release_body(package):
    return ("Gitgo fixed release " + package["version"] + "\n\nSource: " + package["source_oid"]
            + "\nTree: " + package["tree_oid"] + "\nPackage SHA-256: " + package["sha256"]
            + "\n\n<!-- gitgo-package:" + digest({key: package[key] for key in ("version", "source_oid", "tree_oid", "sha256")}) + " -->")


def drive_release(transport, package, assets, record, checkpoint, *, observe_only=False):
    """Explicit driver; checkpoint mutates only the caller's authoritative CAS."""
    version, oid, body = package["version"], package["source_oid"], release_body(package)
    actor, repository = transport.client.verify_identity(), transport.client.repository()
    if record.get("repository_id", repository["id"]) != repository["id"] or record.get("account_id", actor["id"]) != actor["id"]:
        raise PublishError("PUBLISH_RELEASE_TARGET_CHANGED")
    record.update(repository_id=repository["id"], account_id=actor["id"])
    transport.commit(oid)
    def write(step, action):
        record.update(current=step, state="intent")
        checkpoint(record)
        action()
    tag = transport.tag(version)
    if tag is None and not observe_only:
        write("tag", lambda: transport.create_tag(version, oid))
        tag = transport.tag(version)
    if tag not in (None, oid):
        raise PublishError("PUBLISH_TAG_CONFLICT")
    if tag is None:
        record.update(state="partial", current=None, reason="PUBLISH_RELEASE_TAG_ABSENT")
        checkpoint(record)
        return record
    released = transport.release(version)
    if released is not None and released["body"] != body:
        raise PublishError("PUBLISH_RELEASE_CONFLICT")
    if released is None and not observe_only and transport.target.kind == "github":
        write("release", lambda: transport.create_release(version, oid, body, assets))
        released = transport.release(version)
        if released is None or released["body"] != body:
            raise PublishError("PUBLISH_RELEASE_CONFLICT")
    known = transport.assets(released) if released and transport.target.kind == "github" else {}
    if set(known) - {row["name"] for row in assets}:
        raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
    observed = []
    for asset in assets:
        raw = transport.download(released, version, asset["name"], known) if released or transport.target.kind == "gitlab" else None
        if raw is None and not observe_only:
            write("upload:" + asset["name"], lambda asset=asset: transport.upload(released, version, asset, asset["raw"]))
            raw = transport.download(released, version, asset["name"])
        actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if raw is not None and (actual != asset["sha256"] or len(raw) != asset["size"]):
            raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
        observed.append({"name": asset["name"], "state": "verified" if raw is not None else "absent", "sha256": actual})
        record.update(assets=list(observed), current=None)
        checkpoint(record)
    complete = all(row["state"] == "verified" for row in observed)
    if complete and not released and not observe_only:
        write("release", lambda: transport.create_release(version, oid, body, assets))
        released = transport.release(version)
    if released and released["body"] != body:
        raise PublishError("PUBLISH_RELEASE_CONFLICT")
    if transport.target.kind == "gitlab" and released:
        links = released["links"]
        if {row.get("name"): row.get("url") for row in links} != {a["name"]: transport.asset_url(version, a["name"]) for a in assets} or len(links) != len(assets):
            raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
    if complete and released and released["draft"] and not observe_only:
        write("publish", lambda: transport.publish(released))
        released = transport.release(version)
    # Re-observe fixed tag after all uploads. A moved tag never yields success.
    if transport.tag(version) != oid:
        raise PublishError("PUBLISH_TAG_CONFLICT")
    complete = complete and released is not None and released["body"] == body and released["draft"] is False
    record.update(state="published" if complete else "partial", current=None,
                  reason="PUBLISH_RELEASE_VERIFIED" if complete else "PUBLISH_RELEASE_INCOMPLETE")
    if released:
        record.update(release_id=released["id"], url=released["url"])
    checkpoint(record)
    return record
