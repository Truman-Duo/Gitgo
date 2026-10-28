"""Platform protocol tests: isolated transport, no real outbound messages."""
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.core.publish.collaboration import CollaborationMixin
from backend.core.publish.state import PublishError
from backend.remote.publish_accounts import provision_secret, resolve_secret
from backend.remote.publish_platform import PlatformClient, PlatformError, PlatformTarget
from tests.test_publish_commands import setup, prepare, call

HEAD, BASE, START = "a" * 40, "b" * 40, "c" * 40


def target(kind="github", account="alice"):
    return {"kind": kind, "instance": "https://github.com" if kind == "github" else "https://gitlab.test.invalid/team",
            "repository": "team/repo" if kind == "github" else "group/sub/repo", "account": account}


class Server:
    def __init__(self, kind="github"):
        self.kind = kind
        self.target = PlatformTarget.parse(target(kind))
        self.identity = {"id": 7, "login" if kind == "github" else "username": "alice"}
        self.rows = []
        self.requests = []
        self.changed = False
        self.lose_reply = False
        self.post_hook = lambda: None
        self.next_page = False

    def author(self):
        return {"user" if self.kind == "github" else "author": self.identity}

    def document(self, number=9):
        return {"id": 101, "number" if self.kind == "github" else "iid": number,
            "title": "Remote work", "body" if self.kind == "github" else "description": "changed" if self.changed else "Discuss this candidate",
            "state": "open" if self.kind == "github" else "opened", **self.author(),
            "updated_at": "2026-10-26T00:00:00Z", "html_url": "https://github.com/team/repo/pull/9",
            **({"head": {"sha": HEAD, "ref": "feature"}, "base": {"sha": BASE, "ref": "main"}} if self.kind == "github" else
               {"diff_refs": {"head_sha": HEAD, "base_sha": BASE, "start_sha": START}, "source_branch": "feature", "target_branch": "main"})}

    def handle(self, request):
        self.requests.append(request)
        suffix = request.url.path.split("/api/v4", 1)[-1] if self.kind == "gitlab" else request.url.path
        decoded_repo = self.target.repo_path.replace("%2F", "/")
        assert request.headers["authorization"] == "Bearer fixture-secret"
        if suffix == "/user":
            return httpx.Response(200, json=self.identity)
        if suffix == decoded_repo:
            return httpx.Response(200, json={"id": 42, "full_name" if self.kind == "github" else "path_with_namespace": self.target.repository,
                                             "default_branch": "main"})
        if request.method == "POST":
            self.post_hook()
            data = json.loads(request.content)
            row = {"id": 1000 + len(self.rows), **self.author(), **data}
            if "event" in data:
                row["state"] = {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[data["event"]]
            if suffix.endswith("/issues") or suffix.endswith("/pulls") or suffix.endswith("/merge_requests"):
                row.update(self.document(10))
                row.update(data)
                if suffix.endswith("/pulls"):
                    row["head"] = {"sha": HEAD, "ref": data["head"]}
                    row["base"] = {"sha": BASE, "ref": data["base"]}
                if suffix.endswith("/issues"):
                    row.pop("head", None)
                    row.pop("base", None)
                    row.pop("diff_refs", None)
            if self.kind == "gitlab" and suffix.endswith("/discussions"):
                self.rows.append({"id": "abc123", "notes": [row]})
                result = self.rows[-1]
            else:
                self.rows.append(row)
                result = row
            if self.lose_reply:
                raise httpx.ReadTimeout("synthetic lost acknowledgement")
            return httpx.Response(201, json=result)
        if suffix.endswith("/9"):
            return httpx.Response(200, json=self.document(), headers={"etag": '"v1"'})
        if suffix.endswith("/files") or suffix.endswith("/diffs"):
            return httpx.Response(200, json=[{"filename": "calc.py", "new_path": "calc.py", "old_path": "calc.py",
                "patch": "@@ -1,2 +1,2 @@\n keep\n-old\n+new", "diff": "@@ -1,2 +1,2 @@\n keep\n-old\n+new"}])
        if "/branches/" in suffix:
            oid = HEAD if suffix.endswith("feature") else BASE
            return httpx.Response(200, json={"commit": {"sha" if self.kind == "github" else "id": oid}})
        return httpx.Response(200, json=self.rows, headers={"Link": '<https://evil.invalid/page>; rel="next"'} if self.next_page else {})

    def client(self, parsed):
        return PlatformClient(parsed, "fixture-secret", transport=httpx.MockTransport(self.handle))


class Repository:
    def __init__(self):
        self.revision = 0
        self.ledger = {"items": {"one": {"id": "one", "revision": 1, "binding": "fixed-binding", "status": "prepared"}}}

    def load(self):
        return deepcopy(self.ledger), self.revision


class Harness(CollaborationMixin):
    def __init__(self, server):
        self.repo, self.server = Repository(), server
        self.actor, self.task_id, self.workspace = "test-process", "task-1", "fixture-workspace"
        self.project = SimpleNamespace(outbound_policy={})
        self.sequence = 0

    def _collaboration_client(self, parsed):
        return self.server.client(parsed)

    def _save_item(self, ledger, item, ref):
        assert ref == self.repo.revision
        self.repo.ledger = deepcopy(ledger)
        self.repo.revision += 1
        return {"item": deepcopy(item), "effect_state": "committed"}

    def call(self, action, **args):
        self.sequence += 1
        self.operation_id = "op-" + str(self.sequence)
        return self._collaborate(item_id="one", expected_revision=self.repo.ledger["items"]["one"]["revision"], action=action, **args)["item"]

    def refresh(self, kind="pull"):
        return self.call("refresh", target=self.server.target.descriptor(), resource={"kind": kind, **({"number": 9} if kind in {"pull", "issue"} else {})})["collaboration"]["snapshots"][-1]

    def draft(self, kind="comment", **content):
        snapshot = self.refresh(kind + "s" if kind in {"issue", "pull"} else "pull")
        return self.call("draft", snapshot_id=snapshot["id"], draft={"kind": kind, "body": "Please inspect the fixed candidate.", **content})["collaboration"]["drafts"][-1]


def test_scopes_include_instance_repository_and_account_without_environment_fallback(monkeypatch):
    parsed = PlatformTarget.parse(target("gitlab"))
    assert parsed.api_base == "https://gitlab.test.invalid/team/api/v4"
    assert parsed.repo_path == "/projects/group%2Fsub%2Frepo"
    assert parsed.scope_id != PlatformTarget.parse(target("gitlab", "bob")).scope_id
    provision_secret(parsed, "fixture-secret")
    assert resolve_secret(parsed) == "fixture-secret"
    monkeypatch.setenv("GITLAB_TOKEN", "must-not-be-used")
    with pytest.raises(PlatformError, match="UNCONFIGURED"):
        resolve_secret(PlatformTarget.parse(target("gitlab", "bob")))


@pytest.mark.parametrize("changes", [{"instance": "http://gitlab.invalid"}, {"instance": "https://u:pw@gitlab.invalid"},
    {"instance": "https://gitlab.invalid/../api"}, {"repository": "group/../repo"}, {"account": "alice\nAuthorization: bad"}])
def test_invalid_scopes_rejected(changes):
    with pytest.raises(PlatformError, match="TARGET_INVALID"):
        PlatformTarget.parse({**target("gitlab"), **changes})


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_refresh_bounded_pages_and_offline_draft_preserve_actor_binding(kind):
    server, h = Server(kind), None
    h = Harness(server)
    server.next_page = True
    snapshot = h.refresh()
    assert snapshot["binding"] == "fixed-binding" and snapshot["comments_more"]
    count = len(server.requests)
    draft = h.call("draft", snapshot_id=snapshot["id"], draft={"kind": "comment", "body": "A review note"})["collaboration"]["drafts"][-1]
    assert len(server.requests) == count
    assert draft["state"] == "draft" and draft["actor"] == "test-process" and draft["task_id"] == "task-1"
    assert all(r.url.host != "evil.invalid" for r in server.requests)
    assert all(r.url.params.get("per_page") == "50" for r in server.requests if "page" in r.url.params)


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_verified_line_anchor_sent_once_and_durable_intent_precedes_post(kind):
    server = Server(kind)
    h = Harness(server)
    draft = h.draft("review", anchor={"path": "calc.py", "line": 2, "side": "RIGHT", "commit_id": HEAD})
    def check_intent():
        saved = h.repo.ledger["items"]["one"]["collaboration"]["drafts"][-1]
        assert saved["state"] == "sending" and saved["send_task_id"] == "task-1"
    server.post_hook = check_intent
    item = h.call("send", draft_id=draft["id"])
    sent = item["collaboration"]["drafts"][-1]
    assert sent["state"] == "sent" and sent["result"]["author"]["id"] == 7
    with pytest.raises(PublishError, match="ALREADY_ATTEMPTED"):
        h.call("send", draft_id=draft["id"])
    assert sum(r.method == "POST" for r in server.requests) == 1


def test_lost_post_reply_reconciles_exact_author_body_and_anchor_without_replay():
    server = Server()
    h = Harness(server)
    draft = h.draft("review", anchor={"path": "calc.py", "line": 2, "side": "RIGHT", "commit_id": HEAD})
    server.lose_reply = True
    assert h.call("send", draft_id=draft["id"])["collaboration"]["drafts"][-1]["state"] == "unknown"
    server.rows[0]["user"] = {"id": 88, "login": "mallory"}
    assert h.call("reconcile", draft_id=draft["id"])["collaboration"]["drafts"][-1]["state"] == "unknown"
    server.rows[0]["user"] = server.identity
    server.rows[0]["line"] = 1
    assert h.call("reconcile", draft_id=draft["id"])["collaboration"]["drafts"][-1]["state"] == "unknown"
    server.rows[0]["line"] = 2
    assert h.call("reconcile", draft_id=draft["id"])["collaboration"]["drafts"][-1]["state"] == "sent"
    assert sum(r.method == "POST" for r in server.requests) == 1


@pytest.mark.parametrize("change", ["document", "account", "binding"])
def test_stale_document_account_or_candidate_blocks_send(change):
    server = Server()
    h = Harness(server)
    draft = h.draft()
    if change == "document":
        server.changed = True
    elif change == "account":
        server.identity = {"id": 8, "login": "bob"}
    else:
        h.repo.ledger["items"]["one"]["binding"] = "new-candidate"
    if change == "binding":
        with pytest.raises(PublishError, match="DRAFT_STALE"):
            h.call("send", draft_id=draft["id"])
    else:
        assert h.call("send", draft_id=draft["id"])["collaboration"]["drafts"][-1]["state"] == "blocked"
    assert not any(r.method == "POST" for r in server.requests)


@pytest.mark.parametrize("line,commit", [(9, HEAD), (2, BASE)])
def test_anchor_must_be_in_observed_fixed_diff(line, commit):
    h = Harness(Server())
    with pytest.raises(PublishError, match="ANCHOR_"):
        h.draft("review", anchor={"path": "calc.py", "line": line, "side": "RIGHT", "commit_id": commit})


def test_gitlab_quick_actions_and_outgoing_secrets_are_rejected_before_network():
    for kind, body, code in [("gitlab", "/close", "QUICK_ACTION"), ("github", "ghp_" + "x" * 30, "PRIVACY")]:
        server = Server(kind)
        h = Harness(server)
        snapshot = h.refresh()
        count = len(server.requests)
        with pytest.raises(PublishError, match=code):
            h.call("draft", snapshot_id=snapshot["id"], draft={"kind": "comment", "body": body})
        assert len(server.requests) == count


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_pull_creation_checks_pinned_remote_branch_heads(kind):
    server = Server(kind)
    h = Harness(server)
    draft = h.draft("pull", title="Fixed change", head="feature", base="main", head_oid=HEAD, base_oid=BASE)
    item = h.call("send", draft_id=draft["id"])
    assert item["collaboration"]["drafts"][-1]["state"] == "sent"
    branches = [r for r in server.requests if "/branches/" in r.url.path]
    assert len(branches) == 2


def test_http_redirect_and_oversized_read_are_rejected_without_following(monkeypatch):
    target_value = PlatformTarget.parse(target())
    calls = []
    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/steal"})
    with PlatformClient(target_value, "fixture-secret", transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(PlatformError, match="HTTP_302"):
            client.verify_identity()
    assert len(calls) == 1
    monkeypatch.setattr("backend.remote.publish_platform.MAX_RESPONSE", 20)
    with PlatformClient(target_value, "fixture-secret", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 21))) as client:
        with pytest.raises(PlatformError, match="RESPONSE_LIMIT"):
            client.verify_identity()


def test_invalid_page_and_unknown_effect_do_not_allow_additional_remote_work():
    server = Server()
    h = Harness(server)
    with pytest.raises(PublishError, match="PAGE_INVALID"):
        h.call("refresh", target=target(), resource={"kind": "issues"}, page=21)
    assert not server.requests
    draft = h.draft()
    server.lose_reply = True
    h.call("send", draft_id=draft["id"])
    count = len(server.requests)
    for action, args in [("refresh", {"target": target(), "resource": {"kind": "issues"}}),
                         ("draft", {"snapshot_id": draft["snapshot_id"], "draft": {"kind": "comment", "body": "another"}})]:
        with pytest.raises(PublishError, match="RECONCILIATION_REQUIRED"):
            h.call(action, **args)
    assert len(server.requests) == count


def test_malformed_api_ref_values_are_rejected_as_protocol_errors():
    server = Server()
    document = server.document()
    document["head"] = "not-a-head-object"
    with server.client(server.target) as client:
        with pytest.raises(PlatformError, match="RESPONSE_INVALID"):
            client.normalize(document, "document")


def test_real_service_uses_separate_send_grant_receipt_and_readonly_projection(setup, monkeypatch):
    from backend.core.publish.state import read_ledger
    from backend.core.tools.publish_service import invoke_local_publish
    server = Server()
    monkeypatch.setattr(CollaborationMixin, "_collaboration_client", lambda self, parsed: server.client(parsed))
    item = prepare(setup)["item"]
    item = call(setup, "collaborate", "remote-refresh", item_id=item["id"], expected_revision=item["revision"],
        action="refresh", target=target(), resource={"kind": "pull", "number": 9})["item"]
    snapshot = item["collaboration"]["snapshots"][-1]
    item = call(setup, "collaborate", "remote-draft", item_id=item["id"], expected_revision=item["revision"],
        action="draft", snapshot_id=snapshot["id"], draft={"kind": "comment", "body": "Explicit reviewed note"})["item"]
    args = {"request_id": "remote-send", "item_id": item["id"], "expected_revision": item["revision"],
        "draft_id": item["collaboration"]["drafts"][-1]["id"]}
    with pytest.raises(PublishError, match="AUTHORIZATION_REQUIRED"):
        invoke_local_publish(setup[1], "collaborate_send", args)
    sent = setup[0].invoke("publish.collaborate.send", {"project": setup[1].name, **args})
    assert sent["effect_state"] == "committed"
    assert sent["item"]["collaboration"]["drafts"][-1]["state"] == "sent"
    count = len(server.requests)
    assert setup[0].invoke("publish.collaborate.send", {"project": setup[1].name, **args}) == sent
    before = read_ledger(setup[2])
    projected = setup[0].invoke("publish.item", {"project": setup[1].name, "item_id": item["id"]})
    assert projected["item"]["collaboration"]["drafts"][-1]["state"] == "sent"
    assert len(server.requests) == count and read_ledger(setup[2]) == before


def test_real_service_unknown_effect_blocks_other_mutation_until_observation(setup, monkeypatch):
    server = Server()
    monkeypatch.setattr(CollaborationMixin, "_collaboration_client", lambda self, parsed: server.client(parsed))
    item = prepare(setup)["item"]
    item = call(setup, "collaborate", "refresh", item_id=item["id"], expected_revision=item["revision"],
        action="refresh", target=target(), resource={"kind": "pull", "number": 9})["item"]
    item = call(setup, "collaborate", "draft", item_id=item["id"], expected_revision=item["revision"], action="draft",
        snapshot_id=item["collaboration"]["snapshots"][-1]["id"], draft={"kind": "comment", "body": "One exact message"})["item"]
    draft_id = item["collaboration"]["drafts"][-1]["id"]
    server.lose_reply = True
    result = call(setup, "collaborate.send", "send", item_id=item["id"], expected_revision=item["revision"], draft_id=draft_id)
    assert result["effect_state"] == "ambiguous"
    item = result["item"]
    assert call(setup, "collaborate", "blocked-refresh", item_id=item["id"], expected_revision=item["revision"], action="refresh",
        target=target(), resource={"kind": "pull", "number": 9})["error"] == "PUBLISH_COLLABORATION_RECONCILIATION_REQUIRED"
    assert call(setup, "plan.update", "blocked-plan", item_id=item["id"], expected_revision=item["revision"],
        target_branch="publish/revised", owner="owner", rework="fix")["error"] == "PUBLISH_OPERATION_PENDING"
    result = call(setup, "collaborate", "observe", item_id=item["id"], expected_revision=item["revision"],
        action="reconcile", draft_id=draft_id)
    assert result["effect_state"] == "committed" and result["item"]["collaboration"]["drafts"][-1]["state"] == "sent"
    assert sum(r.method == "POST" for r in server.requests) == 1


def test_task_link_uses_durable_parent_and_current_decision_without_creating_task(setup):
    from backend.core.storage import get_storage
    item = prepare(setup)["item"]
    item = call(setup, "decide", "rework", item_id=item["id"], expected_revision=item["revision"],
        decision="rework", note="Fix the selected failure", owner="worker")["item"]
    storage = get_storage(str(setup[2]))
    for process_id in ("root", "child"):
        storage.save_agent_checkpoint({"process_id": process_id, "parent_id": "root" if process_id == "child" else "",
            "session_id": "session-" + process_id, "task_id": "task-" + process_id, "status": "completed",
            "role": "worker" if process_id == "child" else "supervisor", "messages": [], "runtime_state": {}})
    before = storage.list_agent_process_links()
    assert call(setup, "link_task", "bad-parent", item_id=item["id"], expected_revision=item["revision"],
        process_id="child", parent_process_id="not-parent")["error"] == "PUBLISH_TASK_PARENT_MISMATCH"
    assert call(setup, "link_task", "other-project", item_id=item["id"], expected_revision=item["revision"],
        process_id="other-workspace-process")["error"] == "PUBLISH_TASK_NOT_FOUND"
    linked = call(setup, "link_task", "link-task", item_id=item["id"], expected_revision=item["revision"], process_id="child",
        parent_process_id="root", decision_id=item["reviews"][-1]["id"])["item"]
    link = linked["task_links"][-1]
    assert link["linked_task_id"] == "task-child" and link["parent_task_id"] == "task-root"
    assert link["observed_status"] == "completed" and link["binding"] == item["binding"]
    assert storage.list_agent_process_links() == before
