"""Fixed-tag GitHub/GitLab releases with downloaded-byte verification.

Every write is preceded by a caller-persisted checkpoint. A reconciliation
invocation only observes; missing effects require a new explicit release call.
"""
import hashlib
import json
import time
from copy import deepcopy
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
    def __init__(self, client, *, cancelled=None, progress=None):
        self.client, self.target = client, client.target
        self.cancelled, self.progress = cancelled, progress

    def _get(self, path):
        try:
            return self.client.request("GET", path)[0]
        except PlatformError as exc:
            if exc.code == "PUBLISH_PLATFORM_HTTP_404":
                return None
            raise

    def _binary(self, method, url, *, data=None, allow_absent=False, redirect=False, asset_name="", expected=None, maximum=None):
        from .release_engine import ReleaseCancelled
        started, last_check, last_report = time.monotonic(), float("-inf"), float("-inf")
        processed = 0
        total = len(data) if data is not None else None
        def check(force=False):
            nonlocal last_check
            now = time.monotonic()
            # The cancellation source can be the project ledger. Bound these
            # reads independently of network chunk size and transfer speed.
            if self.cancelled and (force or now - last_check >= .2):
                last_check = now
                if self.cancelled():
                    raise ReleaseCancelled("asset_transfer")
        def report(state="running", force=False):
            nonlocal last_report
            now = time.monotonic()
            if self.progress and (force or now - last_report >= 1):
                last_report = now
                self.progress({"direction": "download" if method == "GET" else "upload", "asset": asset_name,
                    "bytes": processed, "total": total, "seconds": round(now - started, 3), "state": state})
        def upload():
            nonlocal processed
            chunks = (data[offset:offset + 65536] for offset in range(0, len(data), 65536)) if isinstance(data, bytes) else iter(data)
            for chunk in chunks:
                check()
                if not isinstance(chunk, bytes) or len(chunk) > 65536 or processed + len(chunk) > total:
                    raise PublishError("PUBLISH_RELEASE_ARTIFACT_INVALID")
                yield chunk
                processed += len(chunk)
                report()
            check(True)
            if processed != total:
                raise PublishError("PUBLISH_RELEASE_ARTIFACT_INVALID")
        check(True)
        report(force=True)
        headers = {"Accept": "application/octet-stream" if method == "GET" else "application/json",
                   "Content-Type": "application/octet-stream"}
        # Keep the platform's required fixed Content-Length while allowing
        # cancellation between socket writes. Bytes offered to transport are
        # progress, not an acknowledgement that the server stored the asset.
        if data is not None:
            headers["Content-Length"] = str(len(data))
        request = self.client.client.build_request(method, url, content=upload() if data is not None else None, headers=headers)
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
                    report("absent", True)
                    return None
                if method == "GET" and response.status_code == 302 and not redirect and self.target.instance == "https://github.com":
                    return self._binary("GET", response.headers.get("location", ""), redirect=True, asset_name=asset_name, expected=expected, maximum=maximum)
                if response.status_code not in ((200,) if method == "GET" else (200, 201)):
                    raise PlatformError("PUBLISH_PLATFORM_HTTP_" + str(response.status_code), ambiguous=method != "GET")
                if method == "GET":
                    length = response.headers.get("content-length", "")
                    total = int(length) if (length.isascii() and length.isdecimal() and len(length) <= 12
                                            and response.headers.get("content-encoding", "identity") == "identity") else None
                raw, hasher = bytearray(), hashlib.sha256()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    check()
                    if time.monotonic() - started > 900:
                        raise PlatformError("PUBLISH_RELEASE_TRANSFER_TIMEOUT", ambiguous=method != "GET")
                    if expected is None or method != "GET": raw.extend(chunk)
                    if ((method == "GET" and processed + len(chunk) > (expected["size"] if expected else min(MAX_PACKAGE,maximum or MAX_PACKAGE)))
                            or method != "GET" and len(raw) > 2 * 1024 * 1024):
                        if method == "GET" and expected is not None:
                            raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
                        raise PlatformError("PUBLISH_RELEASE_ASSET_LIMIT", ambiguous=method != "GET")
                    if method == "GET":
                        hasher.update(chunk)
                        processed += len(chunk)
                        report()
                check(True)
                if method == "GET" and expected is not None:
                    fact = {"size": processed, "sha256": hasher.hexdigest()}
                    if fact != expected:
                        raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
                    report("completed", True)
                    return fact
                report("completed", True)
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
        name = version if version in {"updates-stable","updates-preview"} else version_name(version)
        row = self._get(self.target.repo_path + suffix + quote(name, safe=""))
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

    def assets(self, release, *, metadata=False):
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
                identifier = _number(row.get("id"))
                found[name] = {**row, "id":identifier} if metadata else identifier
            if len(rows) < 100:
                return found
        raise PublishError("PUBLISH_RELEASE_ASSET_LIMIT")

    def asset_url(self, version, name):
        return (self.target.api_base + self.target.repo_path + "/packages/generic/gitgo-publish/"
                + quote(version, safe="") + "/" + quote(name, safe=""))

    def download(self, release, version, name, known=None, *, maximum=None):
        if self.target.kind == "github":
            assets = known if known is not None else self.assets(release)
            if name not in assets:
                return None
            return self._binary("GET", self.target.api_base + self.target.repo_path + "/releases/assets/" + str(assets[name]), asset_name=name, maximum=maximum)
        return self._binary("GET", self.asset_url(version, name), allow_absent=True, asset_name=name, maximum=maximum)

    def asset_inventory(self, release):
        return self.assets(release, metadata=True)

    def delete_asset(self, identifier):
        self.client.request("DELETE", self.target.repo_path + "/releases/assets/" + str(_number(identifier)), expected=(204,))

    def download_fact(self, release, version, asset, known=None):
        expected = {key: asset[key] for key in ("size", "sha256")}
        if self.target.kind == "github":
            assets = known if known is not None else self.assets(release)
            if asset["name"] not in assets: return None
            url = self.target.api_base + self.target.repo_path + "/releases/assets/" + str(assets[asset["name"]])
        else:
            url = self.asset_url(version, asset["name"])
        return self._binary("GET", url, expected=expected, allow_absent=self.target.kind == "gitlab", asset_name=asset["name"])

    def upload(self, release, version, asset, raw):
        if self.target.kind == "github":
            base = "https://uploads.github.com" if self.target.instance == "https://github.com" else self.target.instance + "/api/uploads"
            url = base + self.target.repo_path + "/releases/" + str(_number(release["id"])) + "/assets?name=" + quote(asset["name"], safe="")
            self._binary("POST", url, data=raw, asset_name=asset["name"])
        else:
            self._binary("PUT", self.asset_url(version, asset["name"]), data=raw, asset_name=asset["name"])


def release_body(package):
    return ("Gitgo fixed release " + package["version"] + "\n\nSource: " + package["source_oid"]
            + "\nTree: " + package["tree_oid"] + "\nPackage SHA-256: " + package["sha256"]
            + "\n\n<!-- gitgo-package:" + digest({key: package[key] for key in ("version", "source_oid", "tree_oid", "sha256")}) + " -->")


def drive_release(transport, package, assets, record, checkpoint, *, observe_only=False):
    """Legacy entry point into the common engine, using the original CAS writer."""
    from contextlib import nullcontext
    from .release_engine import ReleaseContext, ReleaseEngine, ReleaseBlocked
    from .package_release_recipe import PackageReleaseRecipe

    recipe = PackageReleaseRecipe(transport, package, assets, record, checkpoint, observe_only=observe_only)

    class Journal:
        def locked(self):
            # PublishService already holds the item lease on this path.
            return nullcontext()

        def record(self, name, state, **fields):
            record["engine"] = {"name": name, "state": state, **fields}
            checkpoint(deepcopy(record))

    try:
        ReleaseEngine(recipe).run(ReleaseContext({}, Journal(), transport), "published")
    except ReleaseBlocked:
        # Historical reconciliation returns the observed partial result and
        # never treats absence as permission to repeat an external write.
        pass
    return record
