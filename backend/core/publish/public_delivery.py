"""Anonymous release-byte observation, separate from authenticated uploads."""
import hashlib
import time
from contextlib import closing
from urllib.parse import quote, urlsplit

import httpx

from .release_engine import ReleaseCancelled
from .state import PublishError

HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}


def validate_url(url):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in HOSTS and not parsed.username
                 and not parsed.password and parsed.port in (None, 443) and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid: raise PublishError("PUBLISH_PUBLIC_REDIRECT_DENIED")


def download_fact(url, expected, *, cancelled=None, progress=None, name="", client=None):
    validate_url(url)
    owned = client is None
    # No project client, credentials, cookie jar, or developer token is reused.
    client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                                   headers={"User-Agent": "gitgo-public-delivery"})
    started, checked, reported, count = time.monotonic(), float("-inf"), float("-inf"), 0
    hasher = hashlib.sha256()
    def check(force=False):
        nonlocal checked
        now = time.monotonic()
        if now - started > 900: raise PublishError("PUBLISH_PUBLIC_DOWNLOAD_TIMEOUT")
        if force or now - checked >= .2:
            checked = now
            if cancelled and cancelled(): raise ReleaseCancelled("anonymous_download")
    def report(state="running", force=False):
        nonlocal reported
        now = time.monotonic()
        if progress and (force or now - reported >= 1):
            reported = now
            progress({"direction":"download", "asset":name, "bytes":count, "total":expected["size"],
                      "seconds":round(now-started, 3), "state":state, "route":"anonymous"})
    try:
        for _ in range(5):
            check(True); validate_url(url)
            request = client.build_request("GET", url, headers={"Accept":"application/octet-stream"})
            request.headers.pop("Authorization", None); request.headers.pop("Cookie", None)
            with closing(client.send(request, stream=True)) as response:
                if response.status_code in {301,302,303,307,308}:
                    url = response.headers.get("location", "")
                    continue
                if response.status_code != 200: raise PublishError("PUBLISH_PUBLIC_DOWNLOAD_FAILED")
                report(force=True)
                for chunk in response.iter_bytes(chunk_size=65536):
                    check(); count += len(chunk)
                    if count > expected["size"]: raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
                    hasher.update(chunk); report()
                check(True)
                fact = {"size": count, "sha256":hasher.hexdigest()}
                if fact != expected: raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
                report("completed", True)
                return fact
        raise PublishError("PUBLISH_PUBLIC_REDIRECT_LIMIT")
    except httpx.HTTPError as error:
        raise PublishError("PUBLISH_PUBLIC_DOWNLOAD_FAILED") from error
    finally:
        if owned: client.close()


def verify_assets(target, version, assets, *, cancelled=None, progress=None):
    if target.kind != "github" or target.instance != "https://github.com":
        raise PublishError("PUBLISH_PUBLIC_VERIFICATION_UNSUPPORTED")
    base = target.instance + "/" + target.repository + "/releases/download/" + quote(version, safe="") + "/"
    return [{"name":asset["name"], **download_fact(base + quote(asset["name"], safe=""),
              {key:asset[key] for key in ("size","sha256")}, cancelled=cancelled, progress=progress, name=asset["name"])}
            for asset in assets]
