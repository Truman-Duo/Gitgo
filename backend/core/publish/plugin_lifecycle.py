"""Parent-validated plugin selections and non-replayed completion observations."""
from copy import deepcopy
import hashlib
import json

from . import formal, plugins, preview
from .git import CandidateGit
from .state import PublishError, digest, encoded, find_item


def bundle_for(storage, plan):
    raw = storage.read_blob(plan["plugin_snapshot_ref"])
    if "sha256:" + hashlib.sha256(raw).hexdigest() != plan["plugin_snapshot_ref"]:
        raise PublishError("PUBLISH_PLUGIN_SNAPSHOT_INVALID")
    return json.loads(raw)


class PluginLifecycleMixin:
    def _observe_formal_failure(self, item_id, reason):
        """Record a typed pre-write refusal, never infer it from a ref snapshot."""
        from .service import now
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        operation = item["operations"][-1]
        if operation["kind"] != "formal" or operation["state"] != "intent":
            raise PublishError("PUBLISH_REVISION_CONFLICT")
        operation.update(state="observed", outcome="failed", integration_succeeded=False,
                         observed_at=now(), reason=reason, attribution="prewrite_refused")
        item["revision"] += 1
        return self._save_item(ledger, item, ref)

    def _apply_plugin_selection(self, item, result, operation_id):
        """Rebuild immutable objects; no original ref, worktree or index changes."""
        from .environment import dependencies
        from .service import now
        plan = deepcopy(item["plan"])
        bundle = bundle_for(self.repo.storage, plan)
        selection = plugins.validate_selection(result["selection"], bundle)
        if result.get("validated_plan") != digest(plan):
            raise PublishError("PUBLISH_PLUGIN_RESULT_STALE")
        git, target = CandidateGit(self.workspace), plan["expected_target_oid"]
        self._formal_git().require_target(plan["target_ref"], target)
        if formal.policy_binding(self.project) != plan["policy_binding"]:
            raise PublishError("PUBLISH_PLAN_STALE")
        selected = selection["commits"]
        old_selected = [row["hash"] for row in bundle["selected"]]
        tree = plan["tree_oid"]
        steps = plan["merge_steps"]
        if selected != old_selected:
            if len(selected) > 32:
                raise PublishError("PUBLISH_PLUGIN_SELECTION_LIMIT")
            current, steps = target, []
            for oid in selected:
                parents = git.run(["rev-list", "--parents", "-n", "1", oid])
                row = parents.stdout.split()
                if parents.returncode or len(row) != 2:
                    raise PublishError("PUBLISH_NONLINEAR_SELECTION")
                step = preview.merge(git, current, oid, base=row[1])
                if step["conflicted"]:
                    raise PublishError("PUBLISH_PLUGIN_SELECTION_CONFLICT")
                steps.append(step)
                tree = step["tree_oid"]
                current = preview.commit(git, tree, [current], "Gitgo private plugin selection preview")
        before = formal.tree_files(git, target)
        after = formal.tree_files(git, tree)
        subset = {row[0]: row for row in before}
        after_map = {row[0]: row for row in after}
        for name in selection["paths"]:
            if name in after_map:
                subset[name] = after_map[name]
            else:
                subset.pop(name, None)
        original_blobs = {name: row[2] for name, row in subset.items()}
        tree, files, excluded = formal.filtered_tree(git, list(subset.values()), bundle["configuration"], "squash")
        if tree == git.oid(target, "tree"):
            raise PublishError("PUBLISH_NO_FORMAL_CHANGES")
        if any(check["target"].split("::", 1)[0] not in {row[0] for row in files} for check in plan["checks"]):
            raise PublishError("PUBLISH_INVALID_TEST_TARGET")
        message, number = formal.commit_message(self.project, git, target, item["candidate"]["oid"],
            bundle["configuration"], self.workspace, selected=selected,
            revisions=[target, *(row["oid"] for row in plan["contributions"])])
        message = plugins.validate_message(message, {**plan, "number": number}, bundle["configuration"], files)
        privacy = formal.privacy(files, bundle["configuration"], message)
        checked = git.run(["diff", "--check", target, tree, "--"])
        if checked.returncode not in (0, 1, 2):
            raise PublishError("PUBLISH_CHECK_UNAVAILABLE")
        entries = plugins.entries(before, files)
        known = {row["hash"]: row for row in bundle["selected"]}
        bundle.update(entries=entries, selected=[known[oid] for oid in selected])
        bundle["project_config"]["publish_number"] = number
        prior_binding = item["binding"]
        plan.update(tree_oid=tree, selected_commits=selected, merge_steps=steps, paths=selection["paths"],
                    dependencies=dependencies(files), excluded_path_fingerprints=excluded,
                    transformed_files=[{"path": name, "before": original_blobs[name], "after": blob}
                                       for name, _, blob, _ in files if original_blobs[name] != blob],
                    commit_message=message, number=number,
                    plugin_snapshot_ref=self.repo.storage.put_blob(encoded(bundle), media_type="application/json"),
                    plugin_selection={"paths": selection["paths"], "commits": selected, "from_binding": prior_binding,
                                      "operation_id": operation_id, "review_policy": "explicit_verify"})
        item["plan"] = plan
        item["binding"] = digest({"candidate": item["candidate"], "plan": plan, "revision": item["revision"] + 1})
        for evidence in [{"kind": "git_diff_check", "scope": "final_tree_whitespace",
                          "outcome": "passed" if checked.returncode == 0 else "failed",
                          "summary": (checked.stdout or checked.stderr)[:8000]}, privacy]:
            item["evidence"].append({**evidence, "binding": item["binding"], "tree_oid": tree,
                "id": operation_id + "-selection-" + evidence["kind"], "producer": "protected_git", "recorded_at": now()})

    def _start_plugin_completion(self, item_id):
        from .service import now
        from backend.core.loop.service_runtime import service_runtime
        runtime = service_runtime()
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        formal_operation = item["operations"][-1]
        if (formal_operation["kind"] != "formal" or formal_operation["state"] != "observed"
                or not formal_operation.get("completion_required")):
            raise PublishError("PUBLISH_NO_PENDING_OPERATION")
        intent = {"id": formal_operation["id"] + "-complete", "kind": "plugin_completion", "state": "intent",
                  "binding": item["binding"], "formal_operation_id": formal_operation["id"],
                  "created_at": now(), "actor": self.actor, "task_id": self.task_id,
                  "runtime_owner": list(runtime.owner)}
        intent["runtime_key"] = runtime.bind_operation(intent, item_id, self.repo)
        item["operations"].append(intent)
        succeeded = formal_operation.get("integration_succeeded", True)
        item.update(status="executing", revision=item["revision"] + 1,
                    plugin_completion={"state": "running", "operation_id": intent["id"],
                        "integration_succeeded": succeeded, "git_result_oid": formal_operation["result_oid"] if succeeded else ""})
        self._save_item(ledger, item, ref)
        files = formal.tree_files(CandidateGit(self.workspace), item["plan"]["tree_oid"])
        bundle = bundle_for(self.repo.storage, item["plan"])
        completion = {"success": succeeded, "commit_hash": formal_operation["result_oid"] if succeeded else "", "files_count": len(bundle["entries"])}
        result = runtime.complete_tree(formal.image(files), item["plan"], completion)
        return self._record_plugin_completion(item_id, result, runtime.producers)

    def _record_plugin_completion(self, item_id, result, producers, *, recovered=False):
        from .service import now
        from backend.core.storage.redaction import redact_for_persistence
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        operation = item["operations"][-1]
        if operation["kind"] != "plugin_completion" or item["status"] != "executing":
            raise PublishError("PUBLISH_NO_PENDING_OPERATION")
        if result.get("cleanup_pending"):
            operation["state"] = "cleanup_pending"
            item["plugin_completion"]["state"] = "cleanup_pending"
            item["revision"] += 1
            saved = self._save_item(ledger, item, ref)
            return {**saved, "effect_state": "ambiguous", "status": "needs_reconciliation", "reason": "SANDBOX_CLEANUP_PENDING"}
        passed = result.get("passed") is True and result.get("validated_plan") == digest(item["plan"])
        outcome = "passed" if passed else "failed"
        reason = "PUBLISH_PLUGIN_COMPLETION_PASSED" if passed else result.get("error", "PUBLISH_PLUGIN_COMPLETION_INTERRUPTED")
        operation.update(state="observed", outcome=outcome, observed_at=now(), recovered=recovered)
        item["plugin_completion"].update(state=outcome, reason=reason, recovered=recovered)
        item["evidence"].append(redact_for_persistence({"id": operation["id"], "kind": "plugin_completion",
            "scope": "isolated_post_integration_callback", "binding": item["binding"], "tree_oid": item["plan"]["tree_oid"],
            "outcome": outcome, "summary": reason, "detail": result.get("summary", ""),
            "observations": result.get("observations", []), "recorded_at": now(),
            "producer": "registered_native_plugin", "native_producers": producers, "recovered": recovered}))
        succeeded = item["plugin_completion"].get("integration_succeeded", True)
        item.update(status="integrated" if succeeded else "failed", revision=item["revision"] + 1)
        if not succeeded:
            item["binding"] = digest({"previous": item["binding"], "refused_operation": operation["id"]})
        saved = self._save_item(ledger, item, ref)
        if not succeeded:
            return {**saved, "integration_failed": True, "error": "PUBLISH_FORMAL_NOT_APPLIED", "completion_failed": not passed}
        return saved if passed else {**saved, "reason": reason, "completion_failed": True}

    def _reconcile_plugin_completion(self, item_id):
        from backend.core.loop.publish_recovery import recover_check
        ledger, _ = self.repo.load()
        item = find_item(ledger, item_id)
        self.effect_started = True
        record = recover_check(self.repo.storage, item["operations"][-1])
        result = record.get("result") if record.get("state") == "finished" else None
        if not result or result.get("cleanup_pending"):
            result = {"error": "PUBLISH_PLUGIN_COMPLETION_INTERRUPTED"}
        return self._record_plugin_completion(item_id, result, record.get("native_producers", []), recovered=True)

    def _skip_unstarted_plugin_completion(self, item_id):
        """A lost execute acknowledgement cannot grant a fresh callback launch."""
        from .service import now
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        operation = item["operations"][-1]
        if operation["kind"] != "formal" or operation["state"] != "observed":
            raise PublishError("PUBLISH_NO_PENDING_OPERATION")
        succeeded = operation.get("integration_succeeded", True)
        item["plugin_completion"] = {"state": "interrupted", "git_result_oid": operation["result_oid"] if succeeded else "",
                                     "integration_succeeded": succeeded,
                                     "reason": "PUBLISH_PLUGIN_COMPLETION_NOT_STARTED"}
        item["evidence"].append({"id": operation["id"] + "-completion-not-started", "kind": "plugin_completion",
            "scope": "isolated_post_integration_callback", "binding": item["binding"], "tree_oid": item["plan"]["tree_oid"],
            "outcome": "failed", "summary": "PUBLISH_PLUGIN_COMPLETION_NOT_STARTED", "recorded_at": now(), "producer": "reconciliation"})
        item.update(status="integrated" if succeeded else "failed", revision=item["revision"] + 1)
        if not succeeded:
            item["binding"] = digest({"previous": item["binding"], "refused_operation": operation["id"]})
        return {**self._save_item(ledger, item, ref), "completion_failed": True, "reason": "PUBLISH_PLUGIN_COMPLETION_NOT_STARTED"}
