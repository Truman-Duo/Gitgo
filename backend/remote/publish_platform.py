"""Bounded, explicit platform I/O for Publish; no environment-token fallback.

Reads are invoked only by refresh/send/reconcile commands. The saved projection
is read by the UI without constructing a client or opening the credential store.
"""
from dataclasses import asdict, dataclass
import hashlib
import json as json_module
import re
from urllib.parse import quote, urlsplit

import httpx

PAGE_SIZE = 50
MAX_PAGE = 20
MAX_RESPONSE = 2 * 1024 * 1024
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class PlatformError(ValueError):
    def __init__(self, code, *, ambiguous=False):
        self.code, self.ambiguous = code, ambiguous
        super().__init__(code)


def canonical(value):
    return json_module.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _reject_constant(_):
    raise ValueError("Non-finite JSON")


@dataclass(frozen=True)
class PlatformTarget:
    kind: str
    instance: str
    repository: str
    account: str

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {"kind", "instance", "repository", "account"}:
            raise PlatformError("PUBLISH_PLATFORM_TARGET_INVALID")
        kind, instance, repository, account = (value[k] for k in ("kind", "instance", "repository", "account"))
        if not all(isinstance(v, str) for v in (kind, instance, repository, account)) or kind not in {"github", "gitlab"}:
            raise PlatformError("PUBLISH_PLATFORM_TARGET_INVALID")
        try:
            parsed = urlsplit(instance)
            port = parsed.port
        except ValueError as exc:
            raise PlatformError("PUBLISH_PLATFORM_TARGET_INVALID") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(ord(c) < 33 for c in instance)
                or "\\" in instance or "%" in instance or len(instance) > 512
                or any(p in {".", ".."} for p in parsed.path.split("/"))
                or not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+", repository)
                or any(p in {".", ".."} for p in repository.split("/")) or len(repository) > 400
                or kind == "github" and len(repository.split("/")) != 2
                or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", account)):
            raise PlatformError("PUBLISH_PLATFORM_TARGET_INVALID")
        repository = repository.removesuffix(".git")
        if not repository.rsplit("/", 1)[-1]:
            raise PlatformError("PUBLISH_PLATFORM_TARGET_INVALID")
        host = parsed.hostname.lower()
        host = "[" + host + "]" if ":" in host else host
        origin = "https://" + host + (":" + str(port) if port and port != 443 else "")
        return cls(kind, origin + parsed.path.rstrip("/"), repository, account)

    def descriptor(self):
        return asdict(self)

    @property
    def scope_id(self):
        return "publish-account-" + digest(self.descriptor())

    @property
    def api_base(self):
        if self.kind == "github":
            return "https://api.github.com" if self.instance == "https://github.com" else self.instance + "/api/v3"
        return self.instance + "/api/v4"

    @property
    def repo_path(self):
        return "/repos/" + self.repository if self.kind == "github" else "/projects/" + quote(self.repository, safe="")


def resource(value):
    if not isinstance(value, dict) or set(value) - {"kind", "number"}:
        raise PlatformError("PUBLISH_PLATFORM_RESOURCE_INVALID")
    kind, number = value.get("kind"), value.get("number")
    if kind in {"issues", "pulls"} and number is None:
        return {"kind": kind}
    if kind in {"issue", "pull"} and type(number) is int and 0 < number < 2**31:
        return {"kind": kind, "number": number}
    raise PlatformError("PUBLISH_PLATFORM_RESOURCE_INVALID")


def _string(value, maximum=32000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
    return value


def _author(value):
    value = value or {}
    if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0:
        raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
    return {"id": value["id"], "login": _string(value.get("login", value.get("username")), 100)}


def _url(value):
    value = _string(value, 2048)
    parsed = urlsplit(value)
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else ""


def _identifier(value):
    if type(value) is int and value > 0 or isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        return value
    raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")


class PlatformClient:
    def __init__(self, target, token, *, transport=None):
        self.target = target if isinstance(target, PlatformTarget) else PlatformTarget.parse(target)
        self.token = token
        if not isinstance(token, str) or not token or any(ord(c) < 33 for c in token):
            raise PlatformError("PUBLISH_ACCOUNT_SECRET_INVALID")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "gitgo-publish"}
        if self.target.kind == "github":
            headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"})
        self.client = httpx.Client(headers=headers, timeout=httpx.Timeout(20, connect=10),
                                   verify=True, follow_redirects=False, trust_env=True, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def request(self, method, path, *, params=None, json=None, expected=(200,), data=None, headers=None):
        if (not isinstance(path, str) or not (path == "/user" or path == self.target.repo_path
                or self.target.kind == "github" and method == "GET" and path == "/search/issues"
                or path.startswith(self.target.repo_path + "/")) or "?" in path or "#" in path
                or "\\" in path or any(p in {".", ".."} for p in path.split("/"))):
            raise PlatformError("PUBLISH_PLATFORM_PATH_INVALID")
        writing = method != "GET"
        try:
            with self.client.stream(method, self.target.api_base + path, params=params,
                                    json=json, data=data, headers=headers) as response:
                if response.status_code not in expected:
                    raise PlatformError("PUBLISH_PLATFORM_HTTP_" + str(response.status_code),
                                        ambiguous=writing and (response.status_code >= 500 or response.status_code in {408, 429}))
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise PlatformError("PUBLISH_PLATFORM_RESPONSE_LIMIT", ambiguous=writing)
                try:
                    payload = json_module.loads(raw, parse_constant=_reject_constant) if raw else None
                except (ValueError, UnicodeError) as exc:
                    raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID", ambiguous=writing) from exc
                return payload, dict(response.headers)
        except httpx.HTTPError as exc:
            raise PlatformError("PUBLISH_PLATFORM_UNAVAILABLE", ambiguous=writing) from exc

    def verify_identity(self):
        data, _ = self.request("GET", "/user")
        identity = _author(data)
        if identity["login"].casefold() != self.target.account.casefold():
            raise PlatformError("PUBLISH_PLATFORM_ACCOUNT_CHANGED")
        return identity

    def repository(self):
        data, _ = self.request("GET", self.target.repo_path)
        if not isinstance(data, dict):
            raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        name = data.get("full_name" if self.target.kind == "github" else "path_with_namespace")
        if not isinstance(name, str) or name.casefold() != self.target.repository.casefold():
            raise PlatformError("PUBLISH_PLATFORM_REPOSITORY_CHANGED")
        return {"id": _identifier(data.get("id")), "name": name,
                "url": _url(data.get("html_url", data.get("web_url"))),
                "default_branch": _string(data.get("default_branch"), 256)}

    def path(self, res):
        res = resource(res)
        plural = "issues" if res["kind"].startswith("issue") else ("pulls" if self.target.kind == "github" else "merge_requests")
        return self.target.repo_path + "/" + plural + ("/" + str(res["number"]) if "number" in res else "")

    def page(self, path, page=1, **params):
        if type(page) is not int or not 1 <= page <= MAX_PAGE:
            raise PlatformError("PUBLISH_PLATFORM_PAGE_INVALID")
        rows, headers = self.request("GET", path, params={**params, "page": page, "per_page": PAGE_SIZE})
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or any(not isinstance(r, dict) for r in rows):
            raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        # We never follow a supplied Link URL (it may name another host).
        more = bool(headers.get("x-next-page")) or 'rel="next"' in headers.get("link", "") or len(rows) == PAGE_SIZE
        return rows, more

    def document(self, res):
        data, headers = self.request("GET", self.path(res))
        if not isinstance(data, dict):
            raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        result = self.normalize(data, "document")
        result["etag"] = _string(headers.get("etag"), 512)
        if result["number"] != res["number"]:
            raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        return result

    def comment_path(self, res, reference):
        ident = str(_identifier(reference["id"]))
        if self.target.kind == "github":
            return self.target.repo_path + ("/pulls/comments/" if reference["kind"] == "anchor" else "/issues/comments/") + ident
        path = self.path(res)
        if reference.get("discussion_id"):
            path += "/discussions/" + str(_identifier(reference["discussion_id"]))
        return path + "/notes/" + ident

    def comment(self, res, reference):
        if self.target.kind == "gitlab" and reference.get("discussion_id"):
            data, headers = self.request("GET", self.path(res) + "/discussions/" + str(_identifier(reference["discussion_id"])))
            notes = data.get("notes") if isinstance(data, dict) else None
            if not isinstance(notes, list) or len(notes) > 100:
                raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
            matching = [note for note in notes if isinstance(note, dict) and str(note.get("id")) == str(reference["id"])]
            if len(matching) != 1:
                raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
            data = matching[0]
        else:
            data, headers = self.request("GET", self.comment_path(res, reference))
        result = self.normalize(data, reference["kind"])
        if str(result["id"]) != str(reference["id"]):
            raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        if reference.get("discussion_id"):
            result["discussion_id"] = reference["discussion_id"]
        result["etag"] = _string(headers.get("etag"), 512)
        return result

    def search(self, res, query, page=1):
        res = resource(res)
        if (res["kind"] not in {"issues", "pulls"} or not isinstance(query, str)
                or not 1 <= len(query.strip()) <= 200 or not re.fullmatch(r"[\w .-]+", query)
                or type(page) is not int or not 1 <= page <= MAX_PAGE):
            raise PlatformError("PUBLISH_PLATFORM_SEARCH_INVALID")
        identity, repository = self.verify_identity(), self.repository()
        incomplete = False
        if self.target.kind == "github":
            data, headers = self.request("GET", "/search/issues", params={"q": 'repo:' + self.target.repository
                + ' is:' + ("issue" if res["kind"] == "issues" else "pr") + ' "' + query.strip() + '"',
                "page": page, "per_page": PAGE_SIZE})
            rows = data.get("items") if isinstance(data, dict) else None
            if (not isinstance(rows, list) or len(rows) > PAGE_SIZE or any(not isinstance(row, dict)
                    or row.get("repository_url", "").casefold() != (self.target.api_base + self.target.repo_path).casefold() for row in rows)):
                raise PlatformError("PUBLISH_PLATFORM_SEARCH_SCOPE_CHANGED")
            more = 'rel="next"' in headers.get("link", "") or len(rows) == PAGE_SIZE
            incomplete = bool(data.get("incomplete_results"))
        else:
            rows, more = self.page(self.path(res), page, search=query.strip(), state="all")
        return {"identity": identity, "repository": repository, "resource": res, "query": query.strip(),
            "page": page, "documents": [self.normalize(row, "document") for row in rows], "more": more, "incomplete": incomplete}

    def normalize(self, row, category):
        if not isinstance(row, dict):
            raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        github = self.target.kind == "github"
        result = {"id": _identifier(row.get("id")), "body": _string(row.get("body", row.get("description"))),
            "author": _author(row.get("user" if github else "author")),
            "url": _url(row.get("html_url", row.get("web_url"))),
            "created_at": _string(row.get("created_at"), 100), "updated_at": _string(row.get("updated_at"), 100),
            "system": bool(row.get("system"))}
        if category == "document":
            result.update(number=row.get("number" if github else "iid"), title=_string(row.get("title"), 1024),
                state=_string(row.get("state"), 64), is_pull=bool(row.get("pull_request") or row.get("head") or row.get("source_branch")))
            if type(result["number"]) is not int:
                raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
            if github:
                head, base = row.get("head") or {}, row.get("base") or {}
                if not isinstance(head, dict) or not isinstance(base, dict):
                    raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
                result["refs"] = {"head_sha": _string(head.get("sha"), 64),
                    "base_sha": _string(base.get("sha"), 64),
                    "head": _string(head.get("ref"), 256), "base": _string(base.get("ref"), 256)}
            else:
                refs = row.get("diff_refs") or {}
                if not isinstance(refs, dict):
                    raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
                result["refs"] = {**{key: _string(refs.get(key), 64) for key in ("base_sha", "head_sha", "start_sha")},
                    "head": _string(row.get("source_branch"), 256), "base": _string(row.get("target_branch"), 256)}
        elif category == "review":
            result.update(state=_string(row.get("state"), 64), commit_id=_string(row.get("commit_id"), 64))
        elif category == "anchor":
            if github:
                result["anchor"] = {key: row.get(key) for key in ("commit_id", "original_commit_id", "path", "line", "side", "start_line", "start_side", "original_line", "in_reply_to_id")}
            else:
                position = row.get("position") or {}
                if not isinstance(position, dict):
                    raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
                result["anchor"] = {key: position.get(key) for key in ("base_sha", "head_sha", "start_sha", "old_path", "new_path", "old_line", "new_line")}
                result["resolved"] = bool(row.get("resolved"))
            for key, value in result["anchor"].items():
                numeric = key.endswith("line") or key == "in_reply_to_id"
                if value is not None and ((numeric and (type(value) is not int or value < 1))
                        or (not numeric and (not isinstance(value, str) or len(value) > 1024))):
                    raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        return result

    def snapshot(self, res, page=1):
        res = resource(res)
        if type(page) is not int or not 1 <= page <= MAX_PAGE:
            raise PlatformError("PUBLISH_PLATFORM_PAGE_INVALID")
        identity, repository = self.verify_identity(), self.repository()
        if "number" not in res:
            rows, more = self.page(self.path(res), page, state="all")
            return {"identity": identity, "repository": repository, "resource": res,
                    "page": page, "documents": [self.normalize(row, "document") for row in rows], "more": more}
        document = self.document(res)
        path = self.path(res)
        comments_path = (self.target.repo_path + "/issues/" + str(res["number"]) + "/comments") if self.target.kind == "github" else path + "/notes"
        rows, more = self.page(comments_path, page)
        result = {"identity": identity, "repository": repository, "resource": res, "document": document,
            "page": page, "comments": [self.normalize(row, "comment") for row in rows], "comments_more": more}
        if res["kind"] == "pull":
            if self.target.kind == "github":
                reviews, reviews_more = self.page(path + "/reviews", page)
                anchors, anchors_more = self.page(path + "/comments", page)
                result.update(reviews=[self.normalize(row, "review") for row in reviews], reviews_more=reviews_more,
                              anchors=[self.normalize(row, "anchor") for row in anchors], anchors_more=anchors_more)
            else:
                discussions, more = self.page(path + "/discussions", page)
                anchors = []
                for discussion in discussions:
                    notes = discussion.get("notes", [])
                    if not isinstance(notes, list) or len(notes) > 100 or len(anchors) + len(notes) > 500:
                        raise PlatformError("PUBLISH_PLATFORM_RESPONSE_LIMIT")
                    anchors.extend({**self.normalize(note, "anchor"), "discussion_id": _identifier(discussion.get("id"))} for note in notes)
                result.update(anchors=anchors, anchors_more=more, reviews=[], reviews_supported=False)
            files, more = self.page(path + ("/files" if self.target.kind == "github" else "/diffs"), page)
            result["files"] = [{"path": _string(row.get("filename", row.get("new_path")), 1024),
                "old_path": _string(row.get("previous_filename", row.get("old_path", row.get("filename"))), 1024),
                "patch": _string(row.get("patch", row.get("diff")), 100000),
                "unavailable": bool(row.get("too_large") or row.get("collapsed") or not row.get("patch", row.get("diff")))} for row in files]
            result["files_more"] = more
        return result

    def branch(self, name):
        if not isinstance(name, str) or len(name) > 256 or not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or ".." in name or name.startswith("-"):
            raise PlatformError("PUBLISH_PLATFORM_BRANCH_INVALID")
        suffix = "/branches/" if self.target.kind == "github" else "/repository/branches/"
        data, _ = self.request("GET", self.target.repo_path + suffix + quote(name, safe=""))
        oid = (data.get("commit") or {}).get("sha" if self.target.kind == "github" else "id") if isinstance(data, dict) else None
        if not isinstance(oid, str) or not OID.fullmatch(oid):
            raise PlatformError("PUBLISH_PLATFORM_RESPONSE_INVALID")
        return oid
