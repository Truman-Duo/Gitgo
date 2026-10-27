"""Explicit branch retention and exact, recoverable branch lifecycle actions."""
from copy import deepcopy

from . import formal
from .git import CandidateGit, OID, branch_ref
from .remote_git import RemoteGit
from .state import PublishError, digest, find_item, projection


def normalized_ref(value):
    return branch_ref(value.removeprefix("refs/heads/") if isinstance(value, str) else value)


def owned_branch(item, workspace, ref):
    """Ownership comes from a recorded absent-ref creation, never a name prefix."""
    owners = [operation for operation in item["operations"] if operation.get("state") == "observed"
              and operation.get("plan", {}).get("method") == "new_work_branch"
              and operation["plan"].get("repository") == workspace
              and operation["plan"].get("target_ref") == ref
              and operation["plan"].get("expected_target_oid") is None
              and OID.fullmatch(operation.get("result_oid", ""))]
    if not owners:
        raise PublishError("PUBLISH_BRANCH_NOT_OWNED")
    return owners[-1]


def protect(item, ref):
    folded = ref.casefold()
    if (folded in {"refs/heads/main", "refs/heads/master", "refs/heads/trunk"}
            or folded.startswith("refs/heads/gitgo/")):
        raise PublishError("PUBLISH_BRANCH_PROTECTED")
    for operation in item["operations"]:
        plan, source = operation.get("plan", {}), operation.get("candidate", {})
        if plan.get("destination") == "formal" and plan.get("target_ref") == ref:
            raise PublishError("PUBLISH_BRANCH_PROTECTED")
        if plan.get("method") == "new_work_branch" and (source.get("resolved_source_ref") or source.get("source_ref")) == ref:
            raise PublishError("PUBLISH_BRANCH_PROTECTED")
    if item["plan"].get("destination") == "formal" and item["plan"]["target_ref"] == ref:
        raise PublishError("PUBLISH_BRANCH_PROTECTED")


def outbound_history(git, oid, policy):
    result = git.run(["rev-list", "--reverse", "--max-count=129", oid])
    commits = result.stdout.split()
    if result.returncode or not 1 <= len(commits) <= 128 or any(not OID.fullmatch(value) for value in commits):
        raise PublishError("PUBLISH_HISTORY_LIMIT")
    total = 0
    for commit in commits:
        files = formal.tree_files(git, commit)
        total += sum(len(row[3]) for row in files)
        if total > 256 * 1024 * 1024:
            raise PublishError("PUBLISH_HISTORY_LIMIT")
        formal.filtered_tree(git, files, policy, "ff")
        message = git.run(["show", "-s", "--format=%B", commit])
        if message.returncode or formal.privacy(files, policy, message.stdout)["outcome"] != "passed":
            raise PublishError("PUBLISH_HISTORY_PRIVACY_BLOCKED")
    return {"outcome": "passed", "scope": "all_retained_branch_history", "commits": len(commits), "bytes": total}


def dependencies(ledger, owner_id, workspace, ref, oid, *, remote_ref=None):
    """Inspect saved dependencies only; closing a PR requires an explicit refresh."""
    active = {"prepared", "checking", "pausing", "paused", "conflicted", "adopted", "executing"}
    result, observed, ancestry = [], {}, {}
    heads = {ref.removeprefix("refs/heads/"), (remote_ref or ref).removeprefix("refs/heads/")}
    def related_head(value):
        # An older observed PR tip can still depend on a branch that advanced.
        # Missing objects are uncertainty, not evidence that it is unrelated.
        if not isinstance(value, str) or not OID.fullmatch(value) or value == oid or len(ancestry) >= 128:
            return True
        if value not in ancestry:
            git = CandidateGit(workspace)
            try:
                git.oid(value)
                ancestry[value] = git.run(["merge-base", "--is-ancestor", value, oid]).returncode != 1
            except PublishError:
                ancestry[value] = True
        return ancestry[value]
    for item in ledger["items"].values():
        plan = item["plan"]
        if (item["id"] != owner_id and plan.get("destination") == "formal"
                and plan.get("repository") == workspace and plan.get("target_ref") == ref):
            result.append({"item_id": item["id"], "kind": "formal_target"})
        if item["status"] in active:
            sources = [item["candidate"], *plan.get("contributions", [])]
            if any(source.get("repository") == workspace and (source.get("resolved_source_ref") or source.get("source_ref")) == ref for source in sources):
                result.append({"item_id": item["id"], "kind": "active_candidate"})
        collaboration = item.get("collaboration", {})
        for snapshot in collaboration.get("snapshots", []):
            target = snapshot.get("target", {})
            platform = (target.get("kind"), target.get("instance"), target.get("repository"))
            documents = snapshot.get("documents", [])
            if snapshot.get("document"):
                documents = [*documents, snapshot["document"]]
            for document in documents:
                if not document.get("is_pull") and snapshot.get("resource", {}).get("kind") not in {"pull", "pulls"}:
                    continue
                key = (*platform, document.get("number"))
                stamp = snapshot.get("observed_at", "")
                if (key not in observed or stamp > observed[key][0]
                        or stamp == observed[key][0] and document.get("state") not in {"closed", "merged"}):
                    observed[key] = (stamp, item["id"], snapshot.get("id"), document)
        for draft in collaboration.get("drafts", []):
            content = draft.get("content", {})
            if content.get("kind") != "pull" or content.get("head") not in heads or not related_head(content.get("head_oid")):
                continue
            if (draft.get("state") in {"sending", "unknown"}
                    or draft.get("state") == "draft" and draft.get("binding") == item["binding"]):
                result.append({"item_id": item["id"], "kind": "pull_draft", "draft_id": draft["id"]})
            elif draft.get("state") == "sent" and draft.get("result"):
                document = draft["result"]
                target = draft.get("target", {})
                key = (target.get("kind"), target.get("instance"), target.get("repository"), document.get("number"))
                stamp = draft.get("confirmed_at", draft.get("created_at", ""))
                if (key not in observed or stamp > observed[key][0]
                        or stamp == observed[key][0] and document.get("state") not in {"closed", "merged"}):
                    observed[key] = (stamp, item["id"], draft.get("id"), document)
    for _, item_id, snapshot_id, document in observed.values():
        refs = document.get("refs", {})
        if (document.get("state") not in {"closed", "merged"} and refs.get("head") in heads
                and related_head(refs.get("head_sha"))):
            result.append({"item_id": item_id, "kind": "open_pull", "snapshot_id": snapshot_id, "number": document.get("number")})
    return result


def require_no_dependencies(ledger, item_id, workspace, plan):
    if plan["kind"] not in {"delete", "remote_delete"}:
        return
    if dependencies(ledger, item_id, workspace, plan["branch"], plan["expected_oid"],
                    remote_ref=plan.get("observation", {}).get("ref")):
        raise PublishError("PUBLISH_BRANCH_HAS_DEPENDENCIES")


class BranchesMixin:
    def _branch(self, *, item_id, expected_revision, action, branch="", owner="", reason="",
                remote="", remote_branch="", merge_item_id=""):
        from .service import now, text
        if action not in {"retain_source", "retain_work", "prepare_archive", "prepare_delete", "prepare_publish", "prepare_remote_delete", "reconcile"}:
            raise PublishError("PUBLISH_INVALID_OPERATION")
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        if item["status"] in {"checking", "pausing", "paused", "executing"}:
            raise PublishError("PUBLISH_OPERATION_PENDING")
        state = item.setdefault("branches", {"retention": [], "publications": []})
        if action == "reconcile":
            return self._observe_branch(item_id)
        if state.get("plan", {}).get("state") == "pending":
            raise PublishError("PUBLISH_OPERATION_PENDING")
        owner, reason = text(owner, 160), text(reason, 4000)
        from backend.core.storage.redaction import redact_for_persistence
        safe = redact_for_persistence({"owner": owner, "reason": reason})
        owner, reason = safe["owner"], safe["reason"]
        if action == "retain_source":
            retention = {"kind": "source", "repository": item["candidate"]["repository"],
                "ref": item["candidate"].get("resolved_source_ref") or item["candidate"]["source_ref"],
                "oid": item["candidate"]["oid"], "owner": owner, "reason": reason,
                "id": self.operation_id, "recorded_at": now(), "actor": self.actor, "task_id": self.task_id}
        else:
            branch = normalized_ref(branch or item["plan"]["target_ref"])
            protect(item, branch)
            ownership = owned_branch(item, self.workspace, branch)
            git = CandidateGit(self.workspace)
            current = git.target(branch)
            if current and current["symbolic"]:
                raise PublishError("PUBLISH_BRANCH_PROTECTED")
            if current and git.run(["merge-base", "--is-ancestor", ownership["result_oid"], current["oid"]]).returncode:
                raise PublishError("PUBLISH_BRANCH_OWNERSHIP_CHANGED")
            if not current and action != "prepare_remote_delete":
                raise PublishError("PUBLISH_TARGET_UNAVAILABLE")
            retention = {"kind": "work", "repository": self.workspace, "ref": branch,
                "oid": current["oid"] if current else ownership["result_oid"], "owner": owner, "reason": reason,
                "creation_operation_id": ownership["id"], "id": self.operation_id,
                "recorded_at": now(), "actor": self.actor, "task_id": self.task_id}
        if len(state["retention"]) >= 128:
            raise PublishError("PUBLISH_CAPACITY_REACHED")
        state["retention"].append(retention)
        if action.startswith("prepare_"):
            kind = action.removeprefix("prepare_")
            oid = retention["oid"]
            plan = {"state": "prepared", "kind": kind, "branch": branch, "expected_oid": oid,
                "binding": item["binding"], "retention": deepcopy(retention), "created_at": now(),
                "creation_operation_id": ownership["id"]}
            if kind in {"archive", "delete"}:
                git.require_target(branch, oid)
            if kind in {"publish", "remote_delete"}:
                target = normalized_ref(remote_branch or branch)
                protect(item, target)
                transport = RemoteGit(self.workspace, remote)
                observed = transport.observe(target)
                plan["observation"] = observed
                if kind == "publish":
                    if len(state["publications"]) >= 32:
                        raise PublishError("PUBLISH_CAPACITY_REACHED")
                    policy = formal.policy(self.project)
                    plan["privacy"] = outbound_history(git, oid, policy)
                    plan["policy_binding"] = digest(policy)
                    if observed["oid"]:
                        self.effect_started = True
                        transport.fetch(observed)
                        if git.run(["merge-base", "--is-ancestor", observed["oid"], oid]).returncode:
                            raise PublishError("PUBLISH_REMOTE_NON_FAST_FORWARD")
                else:
                    publications = [row for row in state["publications"] if row["state"] == "published"
                        and row["observation"]["remote"] == observed["remote"] and row["observation"]["ref"] == observed["ref"]
                        and row["observation"]["identity"] == observed["identity"] and row["oid"] == observed["oid"]
                        and row["branch"] == branch]
                    if not observed["oid"] or not publications:
                        raise PublishError("PUBLISH_BRANCH_NOT_PUBLISHED")
                    oid = observed["oid"]
                    if current and current["oid"] != oid:
                        raise PublishError("PUBLISH_BRANCH_CHANGED")
                    self.effect_started = True
                    transport.fetch(observed)
                    git.oid(oid)
                    plan.update(expected_oid=oid, publication_id=publications[-1]["id"])
                    plan["retention"]["oid"] = oid
            if merge_item_id:
                merged = find_item(ledger, merge_item_id)
                sources = merged["plan"].get("contributions") or [merged["candidate"]]
                successes = [row for row in merged["operations"] if row.get("kind") == "formal"
                    and row.get("state") == "observed" and row.get("integration_succeeded", True)
                    and row.get("binding") == merged["binding"]]
                if merged["status"] != "integrated" or not successes or not any(row["oid"] == oid for row in sources):
                    raise PublishError("PUBLISH_BRANCH_MERGE_UNVERIFIED")
                result = successes[-1]
                target_git = self._formal_git()
                if target_git.workspace != result["plan"]["repository"]:
                    raise PublishError("PUBLISH_PLAN_STALE")
                target = target_git.target(result["plan"]["target_ref"])
                if not target or target["symbolic"] or target_git.run(["merge-base", "--is-ancestor", result["result_oid"], target["oid"]]).returncode:
                    raise PublishError("PUBLISH_BRANCH_MERGE_UNVERIFIED")
                plan["merged_evidence"] = {"item_id": merge_item_id, "binding": merged["binding"],
                    "result_oid": result["result_oid"], "target_oid": target["oid"], "observed_at": now()}
            if kind in {"archive", "delete", "remote_delete"}:
                plan["archive_ref"] = "refs/gitgo/publish/archive/" + item_id + "/" + digest(branch)[:12] + "/" + oid
                archived = git.target(plan["archive_ref"])
                if archived and (archived["symbolic"] or archived["oid"] != oid):
                    raise PublishError("PUBLISH_BRANCH_ARCHIVE_CONFLICT")
            require_no_dependencies(ledger, item_id, self.workspace, plan)
            if state.get("plan"):
                history = state.setdefault("history", [])
                if len(history) >= 32:
                    raise PublishError("PUBLISH_CAPACITY_REACHED")
                history.append(state["plan"])
            state["plan"] = plan
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _branch_apply(self, *, item_id, expected_revision):
        from .service import now
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        plan = item.get("branches", {}).get("plan", {})
        if item["status"] in {"checking", "pausing", "paused", "executing"} or plan.get("state") != "prepared":
            raise PublishError("PUBLISH_OPERATION_PENDING")
        if plan["binding"] != item["binding"]:
            raise PublishError("PUBLISH_PLAN_STALE")
        protect(item, plan["branch"])
        owned_branch(item, self.workspace, plan["branch"])
        require_no_dependencies(ledger, item_id, self.workspace, plan)
        git, kind = CandidateGit(self.workspace), plan["kind"]
        if kind in {"archive", "delete"}:
            git.require_target(plan["branch"], plan["expected_oid"])
        elif kind == "publish":
            current = git.target(plan["branch"])
            if not current or current["symbolic"] or current["oid"] != plan["expected_oid"]:
                raise PublishError("PUBLISH_BRANCH_CHANGED")
        if "observation" in plan:
            observation = plan["observation"]
            protect(item, observation["ref"])
            transport = RemoteGit(self.workspace, observation["remote"])
            current = transport.observe(observation["ref"], identity=observation["identity"])
            if current["oid"] != observation["oid"]:
                raise PublishError("PUBLISH_BRANCH_CHANGED")
            if kind == "publish" and formal.policy_binding(self.project) != plan["policy_binding"]:
                raise PublishError("PUBLISH_PLAN_STALE")
        operation = {"id": self.operation_id, "kind": "branch", "state": "intent", "binding": item["binding"],
            "plan": deepcopy(plan), "actor": self.actor, "task_id": self.task_id, "created_at": now()}
        item["operations"].append(operation)
        plan.update(state="pending", operation_id=self.operation_id)
        item["revision"] += 1
        self._save_item(ledger, item, ref)
        try:
            if "archive_ref" in plan:
                archived = git.target(plan["archive_ref"])
                if not archived:
                    git.require_absent(plan["archive_ref"])
                    git.run(["update-ref", "--no-deref", "-m", "gitgo publish archive", plan["archive_ref"],
                             plan["expected_oid"], "0" * len(plan["expected_oid"])])
                archived = git.target(plan["archive_ref"])
                if not archived or archived["symbolic"] or archived["oid"] != plan["expected_oid"]:
                    raise PublishError("PUBLISH_BRANCH_ARCHIVE_UNVERIFIED")
            if kind == "delete":
                git.require_target(plan["branch"], plan["expected_oid"])
                git.run(["update-ref", "--no-deref", "-m", "gitgo publish delete", "-d", plan["branch"], plan["expected_oid"]])
            elif kind in {"publish", "remote_delete"}:
                transport.update(plan["observation"], plan["expected_oid"] if kind == "publish" else "", delete=kind == "remote_delete")
        except (OSError, RuntimeError, PublishError):
            return {"item": projection(item, None), "effect_state": "ambiguous", "reason": "PUBLISH_BRANCH_RESULT_UNVERIFIED"}
        return self._observe_branch(item_id)

    def _observe_branch(self, item_id):
        from .service import now
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        state, git = item.get("branches", {}), CandidateGit(self.workspace)
        plan = state.get("plan", {})
        if plan.get("state") != "pending":
            raise PublishError("PUBLISH_NO_PENDING_OPERATION")
        try:
            archived = git.target(plan["archive_ref"]) if "archive_ref" in plan else None
            archive_ok = bool(archived and not archived["symbolic"] and archived["oid"] == plan["expected_oid"])
            if "observation" in plan:
                observation = plan["observation"]
                current = RemoteGit(self.workspace, observation["remote"]).observe(observation["ref"], identity=observation["identity"])
            else:
                current = git.target(plan["branch"])
                if current and current["symbolic"]:
                    raise PublishError("PUBLISH_BRANCH_PROTECTED")
            oid = current["oid"] if current else None
            kind = plan["kind"]
            matched = archive_ok if kind == "archive" else oid == plan["expected_oid"] if kind == "publish" else archive_ok and oid is None
        except (OSError, RuntimeError, PublishError):
            return {"item": projection(item, ref), "effect_state": "ambiguous", "reason": "PUBLISH_BRANCH_RESULT_UNVERIFIED"}
        outcome = {"archive": "archived", "delete": "deleted", "publish": "published", "remote_delete": "remote_deleted"}[kind]
        plan.update(state=outcome if matched else "conflict", observed_oid=oid, archive_retained=archive_ok, observed_at=now())
        operation = next(row for row in item["operations"] if row["id"] == plan["operation_id"])
        operation.update(state="observed", outcome=plan["state"], observed_oid=oid, archive_retained=archive_ok, observed_at=now(),
                         attribution="matching_ref_observed")
        if matched and kind == "publish":
            if len(state["publications"]) >= 32:
                raise PublishError("PUBLISH_CAPACITY_REACHED")
            state["publications"].append({"id": operation["id"], "state": "published", "oid": oid,
                "branch": plan["branch"], "observation": deepcopy(plan["observation"]), "observed_at": now()})
        elif matched and kind == "remote_delete":
            publication = next(row for row in state["publications"] if row["id"] == plan["publication_id"])
            publication.update(state="deleted", deleted_at=now(), archive_ref=plan["archive_ref"])
        item["evidence"].append({"id": operation["id"] + "-branch", "kind": "branch_lifecycle", "scope": kind,
            "binding": item["binding"], "outcome": "passed" if matched else "failed", "summary": "PUBLISH_BRANCH_" + plan["state"].upper(),
            "ref": plan.get("observation", {}).get("ref", plan["branch"]), "observed_oid": oid,
            "archive_ref": plan.get("archive_ref", ""), "recorded_at": now(), "producer": "protected_git"})
        item["revision"] += 1
        saved = self._save_item(ledger, item, ref)
        return saved if matched else {**saved, "error": "PUBLISH_BRANCH_RESULT_CONFLICT"}
