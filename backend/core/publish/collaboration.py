"""Local drafts and explicit platform effects in the existing Publish ledger.

This mixin has no permission system. Its entry point must be invoked through
the existing declared tool, actor/task context, grants, receipts and item lease.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re

from backend.core.storage.redaction import redact_for_persistence
from backend.remote.publish_accounts import resolve_secret
from backend.remote.publish_platform import PlatformClient, PlatformError, PlatformTarget, OID, MAX_PAGE
from .state import PublishError, digest, find_item


def now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, maximum, required=True):
    if (not isinstance(value, str) or len(value) > maximum or required and not value.strip()
            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        raise PublishError("PUBLISH_COLLABORATION_DRAFT_INVALID")
    return value.strip()


def _version(document):
    return digest({key: value for key, value in document.items() if key != "etag"})


def _reference(value, snapshot):
    if (not isinstance(value, dict) or set(value) != {"kind", "id"}
            or value["kind"] not in {"comment", "anchor"} or not isinstance(value["id"], str)):
        raise PublishError("PUBLISH_PLATFORM_REFERENCE_INVALID")
    rows = [row for row in snapshot.get("comments" if value["kind"] == "comment" else "anchors", [])
            if str(row["id"]) == value["id"]]
    if len(rows) != 1:
        raise PublishError("PUBLISH_PLATFORM_REFERENCE_UNVERIFIED")
    subject = deepcopy(rows[0])
    return {**value, **({"discussion_id": subject["discussion_id"]} if subject.get("discussion_id") else {})}, subject


def _anchor(value, snapshot, kind):
    if not isinstance(value, dict) or set(value) != {"path", "line", "side", "commit_id"}:
        raise PublishError("PUBLISH_PLATFORM_ANCHOR_INVALID")
    path, line, side, commit = (value[key] for key in ("path", "line", "side", "commit_id"))
    refs = snapshot["document"]["refs"]
    if (type(line) is not int or not 1 <= line <= 10000000 or side not in {"LEFT", "RIGHT"}
            or not isinstance(commit, str) or not OID.fullmatch(commit) or commit != refs.get("head_sha")):
        raise PublishError("PUBLISH_PLATFORM_ANCHOR_STALE")
    files = [row for row in snapshot.get("files", []) if row["path"] == path]
    if len(files) != 1 or files[0]["unavailable"]:
        raise PublishError("PUBLISH_PLATFORM_ANCHOR_UNVERIFIED")
    selected = files[0]
    old = new = None
    found = False
    for row in selected["patch"].splitlines():
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
        if match:
            old, new = int(match[1]), int(match[2])
            continue
        if old is None or row.startswith("\\"):
            continue
        if row.startswith("+"):
            found |= side == "RIGHT" and line == new
            new += 1
        elif row.startswith("-"):
            found |= side == "LEFT" and line == old
            old += 1
        elif row.startswith(" "):
            found |= line == (old if side == "LEFT" else new)
            old += 1
            new += 1
        else:
            old = new = None
    if not found:
        raise PublishError("PUBLISH_PLATFORM_ANCHOR_UNVERIFIED")
    if kind == "github":
        return deepcopy(value)
    if any(not OID.fullmatch(refs.get(key, "")) for key in ("base_sha", "head_sha", "start_sha")):
        raise PublishError("PUBLISH_PLATFORM_ANCHOR_UNVERIFIED")
    return {"position_type": "text", **{key: refs[key] for key in ("base_sha", "head_sha", "start_sha")},
            "old_path": selected["old_path"], "new_path": path,
            "old_line" if side == "LEFT" else "new_line": line}


def _draft(value, snapshot, target):
    allowed = {"kind", "body", "title", "head", "base", "head_oid", "base_oid", "anchor", "decision", "state", "reference"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise PublishError("PUBLISH_COLLABORATION_DRAFT_INVALID")
    kind = value.get("kind")
    if kind not in {"comment", "review", "issue", "pull", "edit", "state", "comment_edit", "reply"}:
        raise PublishError("PUBLISH_COLLABORATION_DRAFT_INVALID")
    body = _text(value.get("body", ""), 16000, required=kind != "state")
    # GitLab interprets /close, /merge, /assign etc. as side-effecting quick
    # actions. A comment tool must not accidentally become a workflow tool.
    if target.kind == "gitlab" and any(re.match(r"^\s*/[A-Za-z]", line) for line in body.splitlines()):
        raise PublishError("PUBLISH_GITLAB_QUICK_ACTION_FORBIDDEN")
    if "<!-- gitgo-publish:" in body:
        raise PublishError("PUBLISH_COLLABORATION_DRAFT_INVALID")
    result = {"kind": kind, "body": body}
    res = snapshot["resource"]
    if kind in {"issue", "pull"}:
        if res["kind"] != kind + "s":
            raise PublishError("PUBLISH_COLLABORATION_RESOURCE_MISMATCH")
        result["title"] = _text(value.get("title"), 256)
        if kind == "pull":
            for key in ("head", "base"):
                name = _text(value.get(key), 256)
                if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or ".." in name or name.startswith("-"):
                    raise PublishError("PUBLISH_PLATFORM_BRANCH_INVALID")
                result[key] = name
                oid = value.get(key + "_oid")
                if not isinstance(oid, str) or not OID.fullmatch(oid):
                    raise PublishError("PUBLISH_COLLABORATION_DRAFT_INVALID")
                result[key + "_oid"] = oid
            if result["head"] == result["base"]:
                raise PublishError("PUBLISH_COLLABORATION_DRAFT_INVALID")
    elif res["kind"] not in {"issue", "pull"}:
        raise PublishError("PUBLISH_COLLABORATION_RESOURCE_MISMATCH")
    if kind in {"edit", "state"}:
        result["subject"] = deepcopy(snapshot["document"])
        if kind == "edit":
            result["title"] = _text(value.get("title"), 256)
        else:
            if value.get("state") not in {"open", "closed"} or snapshot["document"].get("state") == "merged":
                raise PublishError("PUBLISH_PLATFORM_STATE_INVALID")
            result["state"] = value["state"]
    if kind in {"comment_edit", "reply"}:
        reference, subject = _reference(value.get("reference"), snapshot)
        if subject.get("system") or kind == "comment_edit" and subject["author"] != snapshot["identity"]:
            raise PublishError("PUBLISH_PLATFORM_COMMENT_IMMUTABLE")
        if kind == "reply" and reference["kind"] == "anchor" and target.kind == "github":
            parent = subject.get("anchor", {}).get("in_reply_to_id")
            if parent:
                reference, subject = _reference({"kind": "anchor", "id": str(parent)}, snapshot)
            if subject.get("anchor", {}).get("in_reply_to_id"):
                raise PublishError("PUBLISH_PLATFORM_REFERENCE_UNVERIFIED")
        result.update(reference=reference, subject=subject)
    if kind == "review":
        if res["kind"] != "pull":
            raise PublishError("PUBLISH_COLLABORATION_RESOURCE_MISMATCH")
        if value.get("anchor"):
            if value.get("decision", "COMMENT") != "COMMENT":
                raise PublishError("PUBLISH_COLLABORATION_DRAFT_INVALID")
            result["anchor"] = _anchor(value["anchor"], snapshot, target.kind)
        else:
            if target.kind != "github" or value.get("decision", "COMMENT") not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
                raise PublishError("PUBLISH_PLATFORM_REVIEW_UNSUPPORTED")
            result["decision"] = value.get("decision", "COMMENT")
            result["commit_id"] = snapshot["document"]["refs"].get("head_sha")
            if not isinstance(result["commit_id"], str) or not OID.fullmatch(result["commit_id"]):
                raise PublishError("PUBLISH_PLATFORM_ANCHOR_UNVERIFIED")
    if redact_for_persistence(result) != result:
        raise PublishError("PUBLISH_OUTBOUND_PRIVACY_FAILED")
    return result


def _wire(client, draft):
    content = draft["content"]
    kind, body = content["kind"], content["body"] + "\n\n" + draft["marker"]
    github = client.target.kind == "github"
    res = draft["resource"]
    path = client.path(res)
    if kind == "state":
        return path, ({"state": content["state"]} if github else {"state_event": "close" if content["state"] == "closed" else "reopen"}), "document"
    if kind == "edit":
        return path, {"title": content["title"], "body" if github else "description": body}, "document"
    if kind == "comment_edit":
        return client.comment_path(res, content["reference"]), {"body": body}, content["reference"]["kind"]
    if kind == "reply":
        reference = content["reference"]
        if reference["kind"] == "anchor":
            if github:
                return path + "/comments/" + str(reference["id"]) + "/replies", {"body": body}, "anchor"
            if reference.get("discussion_id"):
                return path + "/discussions/" + str(reference["discussion_id"]) + "/notes", {"body": body}, "anchor"
        # Ordinary Issue comments have no thread endpoint. Retain a fixed
        # reference without silently copying potentially private comment text.
        body = "Reply to " + (content["subject"].get("url") or "comment #" + str(reference["id"])) + ":\n\n" + body
        path = client.target.repo_path + "/issues/" + str(res["number"]) + "/comments" if github else path + "/notes"
        return path, {"body": body}, "comment"
    if kind == "issue":
        return path, {"title": content["title"], "body" if github else "description": body}, "document"
    if kind == "pull":
        return path, {"title": content["title"], "body" if github else "description": body,
            "head" if github else "source_branch": content["head"], "base" if github else "target_branch": content["base"]}, "document"
    if kind == "comment":
        path = client.target.repo_path + "/issues/" + str(res["number"]) + "/comments" if github else path + "/notes"
        return path, {"body": body}, "comment"
    if "anchor" in content:
        return path + ("/comments" if github else "/discussions"), ({"body": body, **content["anchor"]} if github
            else {"body": body, "position": content["anchor"]}), "anchor"
    return path + "/reviews", {"body": body, "commit_id": content["commit_id"], "event": content["decision"]}, "review"


def _matches(value, draft, category):
    content = draft["content"]
    subject = content.get("subject", {})
    editing = content["kind"] in {"edit", "state", "comment_edit"}
    expected_body = content["body"] + "\n\n" + draft["marker"]
    if content["kind"] == "reply" and content["reference"]["kind"] == "comment":
        expected_body = "Reply to " + (subject.get("url") or "comment #" + str(content["reference"]["id"])) + ":\n\n" + expected_body
    if content["kind"] == "state":
        return (value.get("id") == subject.get("id") and value.get("state") in
            ({"open", "opened"} if content["state"] == "open" else {"closed"})
            and all(value.get(key) == subject.get(key) for key in ("author", "body", "title", "refs")))
    if (value.get("body", "").replace("\r", "").rstrip() != expected_body.rstrip()
            or value.get("author") != (subject.get("author") if editing else draft["send_identity"])
            or editing and value.get("id") != subject.get("id")):
        return False
    if category == "document":
        if value.get("title") != content["title"]:
            return False
        if content["kind"] == "edit":
            return all(value.get(key) == subject.get(key) for key in ("refs", "state", "is_pull"))
        if value.get("is_pull") != (content["kind"] == "pull"):
            return False
        if content["kind"] == "pull":
            refs = value.get("refs", {})
            return refs.get("head_sha") == content["head_oid"] and refs.get("head") == content["head"] and refs.get("base") == content["base"]
    elif category == "anchor":
        if content["kind"] == "comment_edit":
            return value.get("anchor") == subject.get("anchor")
        if content["kind"] == "reply":
            return (value.get("anchor", {}).get("in_reply_to_id") == subject["id"] if draft["target"]["kind"] == "github"
                    else value.get("discussion_id") == content["reference"].get("discussion_id"))
        expected = content["anchor"]
        return all(value.get("anchor", {}).get(k) == v for k, v in expected.items() if k != "position_type")
    elif category == "review":
        states = {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
        return value.get("state") == states[content["decision"]] and value.get("commit_id") == content["commit_id"]
    return True


class CollaborationMixin:
    def _collaboration_client(self, target):
        return PlatformClient(target, resolve_secret(target))

    def _link_task(self, *, item_id, expected_revision, process_id, parent_process_id="",
                   decision_id="", snapshot_id="", anchor_id="", note=""):
        """Link existing project task facts; never create/complete Agent tasks."""
        process_id = _text(process_id, 160)
        parent_process_id = _text(parent_process_id, 160, required=False)
        note = _text(note, 4000, required=False)
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] in {"checking", "pausing", "executing"}:
            raise PublishError("PUBLISH_OPERATION_PENDING")
        if len(item.get("task_links", [])) >= 128:
            raise PublishError("PUBLISH_CAPACITY_REACHED")
        # The existing storage facade resolves only this workspace's project.
        # Keep transcripts/contracts private; retain the minimal observed facts.
        state = self.repo.storage.load_agent_process_state(process_id)
        if not state or not state.get("task", {}).get("task_id"):
            raise PublishError("PUBLISH_TASK_NOT_FOUND")
        process, task = state["process"], state["task"]
        if process.get("process_id") != process_id or process.get("task_id") != task["task_id"]:
            raise PublishError("PUBLISH_TASK_RELATION_CHANGED")
        actual_parent = process.get("parent_process_id") or ""
        if parent_process_id and parent_process_id != actual_parent:
            raise PublishError("PUBLISH_TASK_PARENT_MISMATCH")
        parent_task_id = ""
        if actual_parent:
            parent = self.repo.storage.load_agent_process_state(actual_parent)
            if not parent or not parent.get("task", {}).get("task_id"):
                raise PublishError("PUBLISH_TASK_PARENT_UNAVAILABLE")
            parent_task_id = parent["task"]["task_id"]
            if (parent["process"].get("process_id") != actual_parent
                    or parent["process"].get("task_id") != parent_task_id
                    or task.get("parent_task_id") != parent_task_id):
                raise PublishError("PUBLISH_TASK_RELATION_CHANGED")
        elif task.get("parent_task_id"):
            raise PublishError("PUBLISH_TASK_RELATION_CHANGED")
        reference = {}
        if decision_id:
            rows = [row for row in item.get("reviews", []) if row["id"] == decision_id and row["binding"] == item["binding"]]
            if len(rows) != 1:
                raise PublishError("PUBLISH_TASK_DECISION_STALE")
            reference["decision_id"] = decision_id
        if snapshot_id:
            snapshots = [row for row in item.get("collaboration", {}).get("snapshots", [])
                         if row["id"] == snapshot_id and row["binding"] == item["binding"]]
            if len(snapshots) != 1:
                raise PublishError("PUBLISH_PLATFORM_REFRESH_REQUIRED")
            snapshot = snapshots[0]
            reference.update(snapshot_id=snapshot_id, target=snapshot["target"], resource=snapshot["resource"])
            if anchor_id:
                anchors = [row for row in snapshot.get("anchors", []) if str(row["id"]) == str(anchor_id)]
                if len(anchors) != 1:
                    raise PublishError("PUBLISH_PLATFORM_ANCHOR_UNVERIFIED")
                reference.update(anchor_id=anchors[0]["id"], anchor=anchors[0]["anchor"])
        elif anchor_id:
            raise PublishError("PUBLISH_PLATFORM_REFRESH_REQUIRED")
        item.setdefault("task_links", []).append(redact_for_persistence({"id": self.operation_id,
            "binding": item["binding"], "process_id": process_id, "linked_task_id": task["task_id"],
            "parent_process_id": actual_parent, "parent_task_id": parent_task_id,
            "process_revision": process.get("state_revision"), "observed_status": task.get("status", ""),
            "recorded_at": now(), "actor": self.actor, "task_id": self.task_id, "note": note, **reference}))
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _collaborate(self, *, item_id, expected_revision, action, target=None, resource=None,
                     draft=None, draft_id=None, snapshot_id=None, page=1, query="", followup=None):
        if action not in {"refresh", "search", "followup", "draft", "send", "reconcile"}:
            raise PublishError("PUBLISH_INVALID_OPERATION")
        if type(page) is not int or not 1 <= page <= MAX_PAGE:
            raise PublishError("PUBLISH_PLATFORM_PAGE_INVALID")
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] in {"checking", "pausing", "executing"}:
            raise PublishError("PUBLISH_OPERATION_PENDING")
        collaboration = item.setdefault("collaboration", {"snapshots": [], "drafts": []})
        if action != "reconcile" and any(row["state"] in {"sending", "unknown"} for row in collaboration["drafts"]):
            raise PublishError("PUBLISH_COLLABORATION_RECONCILIATION_REQUIRED")
        try:
            if action in {"refresh", "search"}:
                if len(collaboration["snapshots"]) >= 32:
                    raise PublishError("PUBLISH_CAPACITY_REACHED")
                parsed = PlatformTarget.parse(target)
                if action == "search":
                    from .formal import privacy
                    if privacy([], {"outbound_policy": self.project.outbound_policy or {}}, _text(query, 200))["outcome"] != "passed":
                        raise PublishError("PUBLISH_OUTBOUND_PRIVACY_FAILED")
                with self._collaboration_client(parsed) as client:
                    snapshot = client.search(resource, query, page) if action == "search" else client.snapshot(resource, page)
                record = {"id": self.operation_id, "binding": item["binding"], "target": parsed.descriptor(),
                    "scope_id": parsed.scope_id, "observed_at": now(), "actor": self.actor, "task_id": self.task_id,
                    "document_version": _version(snapshot["document"]) if "document" in snapshot else None,
                    **redact_for_persistence(snapshot)}
                collaboration["snapshots"].append(record)
                item["revision"] += 1
                return self._save_item(ledger, item, ref)
            if action in {"draft", "followup"}:
                if len(collaboration["drafts"]) >= 128:
                    raise PublishError("PUBLISH_CAPACITY_REACHED")
                snapshots = [row for row in collaboration["snapshots"] if row["id"] == snapshot_id]
                if len(snapshots) != 1 or snapshots[0]["binding"] != item["binding"]:
                    raise PublishError("PUBLISH_PLATFORM_REFRESH_REQUIRED")
                snapshot = snapshots[0]
                parsed = PlatformTarget.parse(snapshot["target"])
                if action == "followup":
                    if (not isinstance(followup, dict) or set(followup) != {"reference", "status", "owner", "note"}
                            or followup["status"] not in {"pending_reply", "resolved"}):
                        raise PublishError("PUBLISH_PLATFORM_FOLLOWUP_INVALID")
                    reference, subject = _reference(followup["reference"], snapshot)
                    records = collaboration.setdefault("followups", [])
                    if len(records) >= 128:
                        raise PublishError("PUBLISH_CAPACITY_REACHED")
                    records.append(redact_for_persistence({"id": self.operation_id, "binding": item["binding"],
                        "snapshot_id": snapshot_id, "target": snapshot["target"], "resource": snapshot["resource"],
                        "reference": reference, "anchor": subject.get("anchor"), "status": followup["status"],
                        "owner": _text(followup["owner"], 160), "note": _text(followup["note"], 4000, required=False),
                        "actor": self.actor, "task_id": self.task_id, "recorded_at": now()}))
                    item["revision"] += 1
                    return self._save_item(ledger, item, ref)
                content = _draft(draft, snapshot, parsed)
                from .formal import privacy
                check = privacy([], {"outbound_policy": self.project.outbound_policy or {}}, content["body"] + "\n" + content.get("title", ""))
                if check["outcome"] != "passed":
                    raise PublishError("PUBLISH_OUTBOUND_PRIVACY_FAILED")
                record = {"id": self.operation_id, "snapshot_id": snapshot_id, "binding": item["binding"],
                    "target": parsed.descriptor(), "resource": snapshot["resource"], "content": content,
                    "state": "draft", "marker": "<!-- gitgo-publish:" + digest({"workspace": self.workspace, "item": item_id, "draft": self.operation_id}) + " -->",
                    "created_at": now(), "actor": self.actor, "task_id": self.task_id}
                collaboration["drafts"].append(record)
                item["revision"] += 1
                return self._save_item(ledger, item, ref)
            matches = [row for row in collaboration["drafts"] if row["id"] == draft_id]
            if len(matches) != 1:
                raise PublishError("PUBLISH_COLLABORATION_DRAFT_NOT_FOUND")
            saved = matches[0]
            if saved["binding"] != item["binding"]:
                raise PublishError("PUBLISH_COLLABORATION_DRAFT_STALE")
            if action == "send":
                if saved["state"] != "draft":
                    raise PublishError("PUBLISH_COLLABORATION_ALREADY_ATTEMPTED")
                return self._collaboration_send(ledger, item, ref, saved)
            if saved["state"] not in {"sending", "unknown"}:
                raise PublishError("PUBLISH_COLLABORATION_RECONCILIATION_UNAVAILABLE")
            return self._collaboration_reconcile(ledger, item, ref, saved, page)
        except PlatformError as exc:
            raise PublishError(exc.code) from exc

    def _collaboration_send(self, ledger, item, ref, saved):
        snapshot = next(row for row in item["collaboration"]["snapshots"] if row["id"] == saved["snapshot_id"])
        target = PlatformTarget.parse(saved["target"])
        # Persist the one-attempt intent before obtaining credentials or making
        # any network call; a crash here requires observation, never replay.
        saved.update(state="sending", send_operation=self.operation_id, send_actor=self.actor,
                     send_task_id=self.task_id, attempted_at=now(), send_identity=snapshot["identity"])
        item["revision"] += 1
        self._save_item(ledger, item, ref)
        ledger, ref = self.repo.load()
        item = find_item(ledger, item["id"])
        saved = next(row for row in item["collaboration"]["drafts"] if row["id"] == saved["id"])
        posted = False
        try:
            with self._collaboration_client(target) as client:
                if client.verify_identity() != snapshot["identity"] or client.repository() != snapshot["repository"]:
                    raise PlatformError("PUBLISH_PLATFORM_SCOPE_CHANGED")
                if "number" in saved["resource"]:
                    current = client.document(saved["resource"])
                    if saved["content"]["kind"] not in {"reply", "comment_edit"} and _version(current) != snapshot["document_version"]:
                        raise PlatformError("PUBLISH_PLATFORM_DOCUMENT_CHANGED")
                    if current["id"] != snapshot["document"]["id"]:
                        raise PlatformError("PUBLISH_PLATFORM_DOCUMENT_CHANGED")
                if saved["content"]["kind"] in {"reply", "comment_edit"}:
                    observed = client.comment(saved["resource"], saved["content"]["reference"])
                    if _version(observed) != _version(saved["content"]["subject"]):
                        raise PlatformError("PUBLISH_PLATFORM_COMMENT_CHANGED")
                if saved["content"]["kind"] == "pull":
                    for key in ("head", "base"):
                        if client.branch(saved["content"][key]) != saved["content"][key + "_oid"]:
                            raise PlatformError("PUBLISH_PLATFORM_BRANCH_CHANGED")
                from .formal import privacy
                content = saved["content"]
                if privacy([], {"outbound_policy": self.project.outbound_policy or {}}, content["body"] + "\n" + content.get("title", ""))["outcome"] != "passed":
                    raise PlatformError("PUBLISH_OUTBOUND_PRIVACY_FAILED")
                path, payload, category = _wire(client, saved)
                outbound = "\n".join(payload.get(key, "") for key in ("body", "description", "title"))
                if (redact_for_persistence(payload) != payload or
                        privacy([], {"outbound_policy": self.project.outbound_policy or {}}, outbound)["outcome"] != "passed"):
                    raise PlatformError("PUBLISH_OUTBOUND_PRIVACY_FAILED")
                posted = True
                # The parent document ETag does not describe the new comment
                # collection; these platform POST endpoints do not promise
                # conditional writes. Preserve the explicit preflight above.
                editing = content["kind"] in {"edit", "state", "comment_edit"}
                method = ("PATCH" if target.kind == "github" else "PUT") if editing else "POST"
                data, _ = client.request(method, path, json=payload, expected=(200, 201))
                if target.kind == "gitlab" and category == "anchor" and content["kind"] == "review":
                    notes = data.get("notes") if isinstance(data, dict) else None
                    data = notes[0] if isinstance(notes, list) and len(notes) == 1 else None
                result = client.normalize(data, category) if isinstance(data, dict) else None
                if result is not None and target.kind == "gitlab" and content.get("reference", {}).get("discussion_id"):
                    result["discussion_id"] = content["reference"]["discussion_id"]
                if result is None or not _matches(result, saved, category):
                    raise PlatformError("PUBLISH_PLATFORM_RESULT_UNCONFIRMED", ambiguous=True)
                saved.update(state="sent", result=redact_for_persistence(result), confirmed_at=now(), attribution="command_acknowledged")
        except PlatformError as exc:
            saved.update(state="unknown" if posted and exc.ambiguous else "blocked", reason=exc.code)
            # Malformed successful responses are uncertain even if a lower
            # normalization helper did not know a POST preceded its input.
            if posted and exc.code.startswith("PUBLISH_PLATFORM_RESPONSE"):
                saved["state"] = "unknown"
        except Exception:
            saved.update(state="unknown" if posted else "blocked", reason="PUBLISH_PLATFORM_UNAVAILABLE")
        item["revision"] += 1
        result = self._save_item(ledger, item, ref)
        return {**result, "effect_state": "ambiguous" if saved["state"] == "unknown" else
                "committed" if saved["state"] == "sent" else "not_committed", "local_state": "committed",
                **({"reason": saved["reason"]} if saved.get("reason") else {})}

    def _collaboration_reconcile(self, ledger, item, ref, saved, page):
        target = PlatformTarget.parse(saved["target"])
        with self._collaboration_client(target) as client:
            if client.verify_identity() != saved["send_identity"]:
                raise PlatformError("PUBLISH_PLATFORM_SCOPE_CHANGED")
            snapshot = next(row for row in item["collaboration"]["snapshots"] if row["id"] == saved["snapshot_id"])
            if client.repository() != snapshot["repository"]:
                raise PlatformError("PUBLISH_PLATFORM_SCOPE_CHANGED")
            path, _, category = _wire(client, saved)
            content = saved["content"]
            editing = content["kind"] in {"edit", "state", "comment_edit"}
            if editing:
                candidates = [client.comment(saved["resource"], content["reference"]) if content["kind"] == "comment_edit"
                              else client.document(saved["resource"])]
                more = False
            else:
                if content["kind"] == "reply" and content["reference"]["kind"] == "anchor":
                    path = client.path(saved["resource"]) + ("/comments" if target.kind == "github" else "/discussions")
                rows, more = client.page(path, page, **({"state": "all"} if category == "document" else {}))
                if target.kind == "gitlab" and category == "anchor":
                    rows = [{**note, "_discussion_id": row.get("id")} for row in rows for note in row.get("notes", [])]
                    if len(rows) > 500:
                        raise PlatformError("PUBLISH_PLATFORM_RESPONSE_LIMIT")
                candidates = [{**client.normalize(row, category), **({"discussion_id": row["_discussion_id"]} if "_discussion_id" in row else {})} for row in rows]
                candidates = [row for row in candidates if saved["marker"] in row["body"] and row["author"] == saved["send_identity"]]
            if category == "document" and not editing:
                if len(candidates) > 4:
                    raise PlatformError("PUBLISH_PLATFORM_RESULT_DUPLICATED")
                # GitLab's list response can omit diff_refs while a create is
                # being prepared. Observe the exact numbered resource instead.
                candidates = [client.document({"kind": saved["content"]["kind"], "number": row["number"]}) for row in candidates]
            matches = [row for row in candidates if _matches(row, saved, category)]
            if len(matches) == 1:
                saved.update(state="sent", result=redact_for_persistence(matches[0]), confirmed_at=now(),
                    attribution="matching_state_observed" if content["kind"] == "state" else "matching_result_observed")
            else:
                saved.update(state="unknown", reason="PUBLISH_PLATFORM_RESULT_DUPLICATED" if matches else "PUBLISH_PLATFORM_RESULT_NOT_OBSERVED")
            observations = saved.setdefault("observations", [])
            if len(observations) >= 40:
                raise PublishError("PUBLISH_CAPACITY_REACHED")
            observations.append({"at": now(), "page": page, "more": more, "matches": len(matches),
                "conflicts": [{"id": row["id"], "url": row["url"], "author": row["author"]} for row in candidates if row not in matches],
                "actor": self.actor, "task_id": self.task_id})
        item["revision"] += 1
        result = self._save_item(ledger, item, ref)
        return {**result, "effect_state": "committed" if saved["state"] == "sent" else "ambiguous",
                "local_state": "committed", **({"reason": saved["reason"]} if saved.get("reason") else {})}
