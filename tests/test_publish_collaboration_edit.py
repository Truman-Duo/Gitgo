"""Scoped edits, stable replies and explicit searches use isolated HTTP only."""
from copy import deepcopy
import json

import httpx
import pytest

from backend.core.publish.state import PublishError
from backend.remote.publish_platform import PlatformClient, PlatformError
from tests.test_publish_collaboration import Server, Harness, HEAD
from tests.test_publish_commands import setup, prepare, call


class EditingServer(Server):
    def __init__(self, kind="github"):
        super().__init__(kind)
        self.doc = super().document()
        self.comments = [{"id": 501, "body": "Original note", **self.author(),
            "html_url": "https://github.com/team/repo/pull/9#issuecomment-501"}]
        self.anchors = [{"id": 502, "body": "Old line note", **self.author(), **(
            {"path": "calc.py", "line": 2, "side": "RIGHT", "commit_id": HEAD} if kind == "github" else
            {"position": {"new_path": "calc.py", "new_line": 2, "head_sha": HEAD}})}]
        self.foreign_search = False

    def document(self, number=9):
        return deepcopy(self.doc)

    def handle(self, request):
        suffix = request.url.path
        github = self.kind == "github"
        custom = (request.method in {"PATCH", "PUT"} or suffix.endswith(("/501", "/502", "/thread1", "/comments/502/replies", "/thread1/notes"))
            or suffix.endswith("/9/comments") or suffix.endswith("/9/notes") or suffix.endswith("/discussions")
            or suffix == "/search/issues" or request.url.params.get("search"))
        if not custom:
            return super().handle(request)
        self.requests.append(request)
        if suffix == "/search/issues" or request.url.params.get("search"):
            row = deepcopy(self.doc)
            row["repository_url"] = self.target.api_base + ("/repos/foreign/private" if self.foreign_search else self.target.repo_path)
            return httpx.Response(200, json={"items": [row], "incomplete_results": True} if github else [row])
        if request.method in {"PATCH", "PUT", "POST"}:
            payload = json.loads(request.content)
            if request.method == "POST":
                if suffix.endswith(("/comments/502/replies", "/thread1/notes")):
                    row = {"id": 600, "body": payload["body"], **self.author(), "in_reply_to_id": 502}
                    self.anchors.append(row)
                else:
                    row = {"id": 601, **payload, **self.author()}
                    self.comments.append(row)
            else:
                row = self.comments[0] if suffix.endswith("/501") else self.anchors[0] if suffix.endswith("/502") else self.doc
                row.update(payload)
                if "state_event" in payload:
                    row["state"] = "closed" if payload["state_event"] == "close" else "opened"
            if self.lose_reply:
                raise httpx.ReadTimeout("Lost mutation response")
            return httpx.Response(200, json=row)
        if suffix.endswith("/501"):
            return httpx.Response(200, json=self.comments[0])
        if suffix.endswith("/502"):
            return httpx.Response(200, json=self.anchors[0])
        if suffix.endswith("/thread1"):
            return httpx.Response(200, json={"id": "thread1", "notes": self.anchors})
        if suffix.endswith("/discussions"):
            return httpx.Response(200, json=[{"id": "thread1", "notes": self.anchors}])
        return httpx.Response(200, json=self.anchors if "/pulls/" in suffix else self.comments)


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("kind,content", [("edit", {"title": "Revised", "body": "Updated text"}),
    ("state", {"state": "closed", "body": ""}), ("state", {"state": "open", "body": ""})])
def test_document_edits_and_state_use_single_intent_and_observe_lost_response(platform, kind, content):
    server, h = EditingServer(platform), None
    h = Harness(server)
    draft = h.draft(kind, **content)
    server.lose_reply = True
    item = h.call("send", draft_id=draft["id"])
    assert item["collaboration"]["drafts"][-1]["state"] == "unknown"
    writes = [r for r in server.requests if r.method != "GET"]
    assert len(writes) == 1 and writes[0].method == ("PATCH" if platform == "github" else "PUT")
    item = h.call("reconcile", draft_id=draft["id"])
    saved = item["collaboration"]["drafts"][-1]
    assert saved["state"] == "sent"
    assert saved["attribution"] == ("matching_state_observed" if kind == "state" else "matching_result_observed")
    assert len([r for r in server.requests if r.method != "GET"]) == 1


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_comment_edit_own_only_and_changed_comment_blocks_before_write(platform):
    server, h = EditingServer(platform), None
    h = Harness(server)
    snapshot = h.refresh()
    draft = h.call("draft", snapshot_id=snapshot["id"], draft={"kind": "comment_edit", "body": "New note",
        "reference": {"kind": "comment", "id": "501"}})["collaboration"]["drafts"][-1]
    server.comments[0]["body"] = "Concurrent author edit"
    saved = h.call("send", draft_id=draft["id"])["collaboration"]["drafts"][-1]
    assert saved["state"] == "blocked" and saved["reason"] == "PUBLISH_PLATFORM_COMMENT_CHANGED"
    assert all(r.method == "GET" for r in server.requests)
    server.comments[0]["user" if platform == "github" else "author"] = {"id": 8, "login" if platform == "github" else "username": "bob"}
    snapshot = h.refresh()
    with pytest.raises(PublishError, match="IMMUTABLE"):
        h.call("draft", snapshot_id=snapshot["id"], draft={"kind": "comment_edit", "body": "Unauthorized",
            "reference": {"kind": "comment", "id": "501"}})


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("reference_kind,ident", [("comment", "501"), ("anchor", "502")])
def test_edit_and_reply_preserve_exact_reference(platform, reference_kind, ident):
    server = EditingServer(platform)
    h = Harness(server)
    draft = h.draft("comment_edit", reference={"kind": reference_kind, "id": ident})
    saved = h.call("send", draft_id=draft["id"])["collaboration"]["drafts"][-1]
    assert saved["state"] == "sent" and str(saved["result"]["id"]) == ident
    draft = h.draft("reply", reference={"kind": reference_kind, "id": ident})
    # Replying to an old discussion remains bound to its original anchor even
    # when the current branch advances between drafting and sending.
    server.doc["head" if platform == "github" else "diff_refs"]["sha" if platform == "github" else "head_sha"] = "d" * 40
    server.lose_reply = True
    saved = h.call("send", draft_id=draft["id"])["collaboration"]["drafts"][-1]
    assert saved["state"] == "unknown"
    saved = h.call("reconcile", draft_id=draft["id"])["collaboration"]["drafts"][-1]
    assert saved["state"] == "sent"
    if reference_kind == "comment":
        assert saved["result"]["body"].startswith("Reply to https://github.com/")
    else:
        assert saved["content"]["subject"]["anchor"]["commit_id" if platform == "github" else "head_sha"] == HEAD


def test_pending_reply_is_local_and_preserves_anchor_and_actor():
    server = EditingServer()
    h = Harness(server)
    snapshot = h.refresh()
    count = len(server.requests)
    item = h.call("followup", snapshot_id=snapshot["id"], followup={"reference": {"kind": "anchor", "id": "502"},
        "status": "pending_reply", "owner": "alice", "note": "Investigate the fixed line"})
    followup = item["collaboration"]["followups"][-1]
    assert followup["anchor"]["commit_id"] == HEAD and followup["actor"] == "test-process"
    assert followup["binding"] == item["binding"] and len(server.requests) == count


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_search_is_explicit_bounded_and_scope_checked(platform):
    server = EditingServer(platform)
    h = Harness(server)
    item = h.call("search", target=server.target.descriptor(), resource={"kind": "pulls"}, query="fixed candidate", page=2)
    snapshot = item["collaboration"]["snapshots"][-1]
    assert snapshot["query"] == "fixed candidate" and snapshot["page"] == 2
    assert server.requests[-1].url.params["per_page"] == "50"
    count = len(server.requests)
    with pytest.raises(PublishError, match="SEARCH_INVALID"):
        h.call("search", target=server.target.descriptor(), resource={"kind": "pulls"}, query='x" OR repo:foreign/private')
    assert len(server.requests) == count
    if platform == "github":
        server.foreign_search = True
        with pytest.raises(PublishError, match="SCOPE_CHANGED"):
            h.call("search", target=server.target.descriptor(), resource={"kind": "pulls"}, query="candidate")


def test_operator_network_trust_keeps_tls_verification(monkeypatch):
    captured = {}
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: captured.update(kwargs))
    PlatformClient(Server().target, "fixture-secret")
    assert captured["trust_env"] is True and captured["verify"] is True and captured["follow_redirects"] is False


def test_create_comment_does_not_send_parent_document_etag():
    server = Server()
    h = Harness(server)
    draft = h.draft()
    assert h.call("send", draft_id=draft["id"])["collaboration"]["drafts"][-1]["state"] == "sent"
    request = next(r for r in server.requests if r.method == "POST")
    assert "if-match" not in request.headers


def test_new_draft_schema_and_explicit_send_use_formal_application_boundary(setup, monkeypatch):
    from backend.core.publish.collaboration import CollaborationMixin
    server = EditingServer()
    monkeypatch.setattr(CollaborationMixin, "_collaboration_client", lambda self, parsed: server.client(parsed))
    item = prepare(setup)["item"]
    item = call(setup, "collaborate", "edit-refresh", item_id=item["id"], expected_revision=item["revision"],
        action="refresh", target=server.target.descriptor(), resource={"kind": "pull", "number": 9})["item"]
    snapshot_id = item["collaboration"]["snapshots"][-1]["id"]
    item = call(setup, "collaborate", "edit-draft", item_id=item["id"], expected_revision=item["revision"],
        action="draft", snapshot_id=snapshot_id, draft={"kind": "edit", "title": "Revised", "body": "Verified edit"})["item"]
    result = call(setup, "collaborate.send", "edit-send", item_id=item["id"], expected_revision=item["revision"],
        draft_id=item["collaboration"]["drafts"][-1]["id"])
    assert result["effect_state"] == "committed" and result["item"]["collaboration"]["drafts"][-1]["state"] == "sent"
