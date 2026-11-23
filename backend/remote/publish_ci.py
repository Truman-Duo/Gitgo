"""Read-only, account-scoped CI observations and artifact byte acquisition."""
from datetime import datetime
import re
import time
from urllib.parse import urlsplit

import httpx

from .publish_platform import PlatformError, OID
from backend.core.publish.release_engine import ReleaseCancelled

MAX_ARTIFACT_BYTES = 1024**3


def number(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
    return value


def instant(value):
    if not isinstance(value, str):
        raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None: raise ValueError()
        return result
    except ValueError as error:
        raise PlatformError("PUBLISH_CI_RESPONSE_INVALID") from error


class ProjectCI:
    def __init__(self, client, *, cancelled=lambda: False, progress=None):
        self.client, self.target, self.cancelled, self.progress = client, client.target, cancelled, progress
        self.last_cancel_check = 0.
        self.read_deadline = None
        if self.target.kind != "github" or self.target.instance != "https://github.com":
            raise PlatformError("PUBLISH_CI_UNSUPPORTED")
        self._check(True)
        self.repository_id = client.repository()["id"]
        self._check(True)
        self.account_id = client.verify_identity()["id"]
        self._check(True)

    def _check(self, force=False):
        now = time.monotonic()
        if self.read_deadline is not None and now >= self.read_deadline:
            raise PlatformError("PUBLISH_CI_READ_TIMEOUT")
        if force or now - self.last_cancel_check >= .2:
            self.last_cancel_check = now
            if self.cancelled(): raise ReleaseCancelled("ci_read")

    def _get(self, suffix, **params):
        self._check(True)
        result, headers = self.client.request("GET", self.target.repo_path + suffix, params=params or None)
        self._check(True)
        return result, headers

    def run(self, run_id, attempt, oid):
        number(run_id); number(attempt)
        if not isinstance(oid, str) or not OID.fullmatch(oid):
            raise PlatformError("PUBLISH_CI_SOURCE_INVALID")
        latest, _ = self._get("/actions/runs/" + str(run_id))
        if not isinstance(latest, dict) or latest.get("run_attempt") != attempt:
            raise PlatformError("PUBLISH_CI_ATTEMPT_MISMATCH")
        row, _ = self._get("/actions/runs/" + str(run_id) + "/attempts/" + str(attempt))
        if (not isinstance(row, dict) or row.get("id") != run_id or row.get("run_attempt") != attempt
                or row.get("head_sha") != oid or row.get("repository", {}).get("id") != self.repository_id
                or row.get("head_repository", {}).get("id") != self.repository_id):
            raise PlatformError("PUBLISH_CI_SOURCE_MISMATCH")
        return {"run_id": run_id, "attempt": attempt, "source_oid": oid,
                "workflow_id": number(row.get("workflow_id")), "workflow": str(row.get("path", "")),
                "status": str(row.get("status", "")), "conclusion": str(row.get("conclusion") or ""),
                "started_at": row.get("run_started_at"), "repository_id": self.repository_id,
                "account_id": self.account_id, "created_at":row.get("created_at")}

    def artifact(self, run, artifact_id, *, row=None):
        number(artifact_id)
        if row is None: row, _ = self._get("/actions/artifacts/" + str(artifact_id))
        if not isinstance(row, dict):
            raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
        origin = row.get("workflow_run") or {}
        if (row.get("id") != artifact_id or origin.get("id") != run["run_id"]
                or origin.get("head_sha") != run["source_oid"]
                or origin.get("repository_id") != self.repository_id or origin.get("head_repository_id") != self.repository_id):
            raise PlatformError("PUBLISH_CI_SOURCE_MISMATCH")
        if row.get("expired") is not False:
            raise PlatformError("PUBLISH_CI_ARTIFACT_EXPIRED")
        if instant(row.get("created_at")) < instant(run["started_at"]):
            raise PlatformError("PUBLISH_CI_ATTEMPT_MISMATCH")
        digest = row.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise PlatformError("PUBLISH_CI_DIGEST_REQUIRED")
        size = number(row.get("size_in_bytes"))
        name = row.get("name")
        if size > MAX_ARTIFACT_BYTES or not isinstance(name, str) or not name or len(name) > 256:
            raise PlatformError("PUBLISH_CI_ARTIFACT_LIMIT")
        return {**run, "artifact_id": artifact_id, "name": name, "size": size,
                "sha256": digest[7:], "created_at": row["created_at"]}

    def jobs(self, run):
        jobs = []
        for page in range(1, 11):
            data, _ = self._get("/actions/runs/" + str(run["run_id"]) + "/attempts/" + str(run["attempt"]) + "/jobs",
                                per_page=50, page=page)
            rows = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) > 50:
                raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
            for row in rows:
                if not isinstance(row, dict) or row.get("run_id") != run["run_id"] or row.get("run_attempt") != run["attempt"]:
                    raise PlatformError("PUBLISH_CI_ATTEMPT_MISMATCH")
                start, end = row.get("started_at"), row.get("completed_at")
                seconds = (instant(end)-instant(start)).total_seconds() if start is not None and end is not None else None
                if seconds is not None and seconds < 0: raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
                jobs.append({"id": number(row.get("id")), "name": str(row.get("name", "")),
                             "status": str(row.get("status", "")), "conclusion": str(row.get("conclusion") or ""),
                             "started_at":start,"completed_at":end,"seconds":seconds})
            if len(rows) < 50: return jobs
        raise PlatformError("PUBLISH_CI_RESPONSE_LIMIT")

    def validate(self, candidate, oid):
        run = self.run(candidate["run_id"], candidate["attempt"], oid)
        actual = self.artifact(run, candidate["artifact_id"])
        if actual != candidate:
            raise PlatformError("PUBLISH_CI_ARTIFACT_CHANGED")
        jobs = self.jobs(run)
        if (run["status"] != "completed" or run["conclusion"] != "success" or not jobs
                or any(row["status"] != "completed" or row["conclusion"] not in {"success", "skipped", "neutral"} for row in jobs)):
            raise PlatformError("PUBLISH_CI_NOT_READY")
        complete = all(row["seconds"] is not None for row in jobs)
        queued = (instant(run["started_at"])-instant(run["created_at"])).total_seconds() if run.get("created_at") else None
        if queued is not None and queued < 0: raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
        return {"run": run, "jobs": jobs, "timing":{
            "queue_seconds":queued,
            "runner_occupied_seconds":sum(row["seconds"] for row in jobs) if complete else None,
            "observed_job_span_seconds":(max(instant(row["completed_at"]) for row in jobs)
                -min(instant(row["started_at"]) for row in jobs)).total_seconds() if complete else None}}

    def candidates(self, oid, page=1):
        if not isinstance(oid, str) or not OID.fullmatch(oid) or type(page) is not int or not 1 <= page <= 20:
            raise PlatformError("PUBLISH_CI_SOURCE_INVALID")
        self.read_deadline = time.monotonic()+90
        data, headers = self._get("/actions/runs", head_sha=oid, per_page=10, page=page)
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list) or len(runs) > 10:
            raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
        rows = []
        for summary in runs:
            run = self.run(number(summary.get("id")), number(summary.get("run_attempt")), oid)
            if run["status"] != "completed" or run["conclusion"] != "success":
                continue
            artifacts, _ = self._get("/actions/runs/" + str(run["run_id"]) + "/artifacts", per_page=50, page=1)
            items = artifacts.get("artifacts") if isinstance(artifacts, dict) else None
            if not isinstance(items, list) or len(items) > 50:
                raise PlatformError("PUBLISH_CI_RESPONSE_INVALID")
            if artifacts.get("total_count", len(items)) > 50:
                raise PlatformError("PUBLISH_CI_RESPONSE_LIMIT")
            for item in items:
                # A partial rerun must not silently adopt another attempt's
                # old artifacts. They remain unavailable for this selection.
                if item.get("expired") is not False or instant(item.get("created_at")) < instant(run["started_at"]):
                    continue
                # The listing contains the same artifact identity/digest
                # fields. Selection/prepare re-fetch the exact chosen IDs;
                # discovery need not make a second request per listed file.
                rows.append(self.artifact(run, number(item.get("id")), row=item))
        return {"rows": rows, "page": page, "has_more": 'rel="next"' in headers.get("link", "") or len(runs) == 10}

    def download(self, candidate, artifacts):
        """No token/cookie follows the API redirect to artifact storage."""
        self._check(True)
        url = self.target.api_base + self.target.repo_path + "/actions/artifacts/" + str(candidate["artifact_id"]) + "/zip"
        request = self.client.client.build_request("GET", url, headers={"Accept": "application/octet-stream"})
        response = None
        try:
            response = self.client.client.send(request, stream=True)
            if response.status_code == 302:
                location = response.headers.get("location", "")
                parsed = urlsplit(location)
                host = parsed.hostname or ""
                allowed = (host.endswith(".actions.githubusercontent.com")
                    or bool(re.fullmatch(r"productionresults[a-z0-9-]*\.blob\.core\.windows\.net", host))
                    or host in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"})
                if (not allowed or parsed.scheme != "https" or parsed.username or parsed.password
                        or parsed.port not in (None, 443)):
                    raise PlatformError("PUBLISH_CI_REDIRECT_DENIED")
                response.close()
                request = self.client.client.build_request("GET", location, headers={"Accept": "application/octet-stream"})
                request.headers.pop("Authorization", None); request.headers.pop("Cookie", None)
                response = self.client.client.send(request, stream=True)
            if response.status_code != 200:
                raise PlatformError("PUBLISH_CI_DOWNLOAD_FAILED")
            started, latest, processed = time.monotonic(), 0., 0
            def chunks():
                nonlocal latest, processed
                for chunk in response.iter_bytes(chunk_size=65536):
                    self._check(); processed += len(chunk)
                    now = time.monotonic()
                    if self.progress and now - latest >= 1:
                        latest = now
                        self.progress({"direction":"download","asset":candidate["name"],"bytes":processed,
                            "total":candidate["size"],"seconds":round(now-started,3),"state":"running"})
                    yield chunk
            value = artifacts.ingest(chunks(), size=candidate["size"], sha256=candidate["sha256"])
            if self.progress:
                self.progress({"direction":"download","asset":candidate["name"],"bytes":processed,
                    "total":candidate["size"],"seconds":round(time.monotonic()-started,3),"state":"completed"})
            return value
        except httpx.HTTPError as error:
            raise PlatformError("PUBLISH_CI_DOWNLOAD_FAILED") from error
        finally:
            if response is not None: response.close()
