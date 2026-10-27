"""Formal preparation, explicit verification and conditional local integration."""
from copy import deepcopy

from backend.core.storage.redaction import redact_for_persistence
from . import formal, checks as check_policy, plugins, preview
from .git import CandidateGit, RefWriteRefused, branch_ref, OID
from .state import PublishError, digest, encoded, find_item, projection, active_evidence, formal_ready
from .plugin_lifecycle import PluginLifecycleMixin


class FormalMixin(PluginLifecycleMixin):
    def _formal_git(self):
        from backend.core.application.services import _external_workspace
        if not self.project.backup_path:
            raise PublishError("PUBLISH_FORMAL_UNCONFIGURED")
        _external_workspace(self.project.backup_path)
        return CandidateGit(self.project.backup_path)

    def _prepare_formal(self, *, source_role, source_ref, target_branch, owner, title, base_oid,
                        rework, item_id, expected_revision, method, test_target, checks,
                        contributions=None, selected_commits=None, paths=None):
        from .service import text, now
        if method not in preview.METHODS:
            raise PublishError("PUBLISH_INVALID_METHOD")
        target_ref = branch_ref(target_branch)
        target_git, git = self._formal_git(), CandidateGit(self.workspace)
        configuration = formal.policy(self.project)
        if configuration["plugins"] and method != "squash":
            raise PublishError("PUBLISH_PLUGIN_REQUIRES_SQUASH")
        target = target_git.target(target_ref)
        if not target or target["symbolic"]:
            raise PublishError("PUBLISH_TARGET_UNAVAILABLE")
        target_git.require_target(target_ref, target["oid"])
        owner, rework, title = text(owner, 160), text(rework, 4000, required=False), text(title, 512, required=False)
        requested_checks = check_policy.requested(checks, test_target)
        checks = check_policy.select(configuration["publish_checks"], requested_checks, "")
        ledger, ref = self.repo.load()
        if item_id:
            item = find_item(ledger, item_id, expected_revision)
            if expected_revision is None or item["status"] in {"executing", "checking", "pausing", "paused"}:
                raise PublishError("PUBLISH_OPERATION_PENDING")
        else:
            if expected_revision is not None:
                raise PublishError("PUBLISH_INVALID_ARGUMENTS")
            item_id = self.operation_id
            item = {"id": item_id, "revision": 0, "created_at": now(), "evidence": [], "operations": []}
        self.effect_started = True  # Immutable object import/number reservation can leave gaps.
        git.import_candidate({"repository": target_git.workspace, "oid": target["oid"],
                              "tree_oid": target_git.oid(target["oid"], "tree")})
        candidates = preview.pin(self, git, target["oid"],
            {"source_role": source_role, "source_ref": source_ref, "base_oid": base_oid}, contributions or [])
        candidate = candidates[0]
        combined = preview.build(git, target["oid"], candidates, method, selected_commits=selected_commits, paths=paths)
        common = {"destination": "formal", "method": method, "repository": target_git.workspace,
                  "target_ref": target_ref, "expected_target_oid": target["oid"],
                  "policy_binding": digest(configuration), "checks": checks, "requested_checks": requested_checks,
                  "contributions": candidates, "merge_steps": combined["steps"],
                  "selected_commits": combined["selected_commits"], "paths": paths or []}
        if (getattr(self, "legacy_source", None) and not combined.get("conflicted")
                and combined["tree_oid"] == git.oid(target["oid"], "tree")):
            # Explicit handoff may record already present history. Its old
            # synced/pushed flags never become fresh verification evidence.
            plan = {**common, "tree_oid": combined["tree_oid"], "automatic_steps": [], "stop_after": "observe"}
            item.update(candidate=candidate, plan=plan, status="observed", revision=item["revision"] + 1)
            item.update(redact_for_persistence({"title": title or source_ref + " → Formal " + target_branch,
                                               "owner": owner, "rework": rework}))
            item["binding"] = digest({"candidate": candidate, "plan": plan, "revision": item["revision"]})
            item["evidence"].append({"kind": "legacy_observation", "scope": "existing_target_tree",
                "outcome": "unverified", "summary": "PUBLISH_LEGACY_HISTORY_OBSERVED", "binding": item["binding"],
                "tree_oid": combined["tree_oid"], "target_oid": target["oid"], "id": self.operation_id + "-history",
                "producer": "protected_git", "recorded_at": now()})
            ledger["items"][item_id] = item
            return self._save_item(ledger, item, ref)
        if combined.get("conflicted"):
            plan = {**common, "conflict_preview_tree": combined["tree_oid"], "conflict_paths": combined["conflict_paths"],
                    "automatic_steps": [], "stop_after": "resolve_conflicts"}
            item.update(candidate=candidate, plan=plan, status="conflicted", revision=item["revision"] + 1)
            item.update(redact_for_persistence({"title": title or source_ref + " → Formal " + target_branch,
                                              "owner": owner, "rework": rework}))
            item["binding"] = digest({"candidate": candidate, "plan": plan, "revision": item["revision"]})
            item["evidence"].append({"kind": "merge_preview", "scope": "combined_tree", "outcome": "failed",
                "summary": "PUBLISH_MERGE_CONFLICT", "binding": item["binding"], "tree_oid": combined["tree_oid"],
                "id": self.operation_id + "-merge", "producer": "protected_git", "recorded_at": now()})
            ledger["items"][item_id] = item
            return self._save_item(ledger, item, ref)
        files = formal.tree_files(git, combined["tree_oid"])
        original_blobs = {name: blob for name, _, blob, _ in files}
        tree_oid, files, exclusions = formal.filtered_tree(git, files, configuration, "ff" if method == "merge" else method)
        from .environment import dependencies
        if any(check["target"].split("::", 1)[0] not in {row[0] for row in files} for check in checks):
            raise PublishError("PUBLISH_INVALID_TEST_TARGET")
        if tree_oid == git.oid(target["oid"], "tree"):
            raise PublishError("PUBLISH_NO_FORMAL_CHANGES")
        message, number = "", None
        revisions = [target["oid"], *(c["oid"] for c in candidates)]
        if method in {"ff", "merge"}:
            preview.audit_history(git, target["oid"], candidates, configuration)
        series = []
        if method in {"rebase", "cherry_pick"}:
            for row in combined["series"]:
                step_files = formal.tree_files(git, row["tree_oid"])
                step_tree, step_files, _ = formal.filtered_tree(git, step_files, configuration, method)
                step_message, step_number = formal.commit_message(self.project, git, target["oid"], candidate["oid"],
                    configuration, self.workspace, selected=[row["source_oid"]], revisions=revisions)
                if formal.privacy(step_files, configuration, step_message)["outcome"] != "passed":
                    raise PublishError("PUBLISH_HISTORY_PRIVACY_BLOCKED")
                series.append({**row, "tree_oid": step_tree, "message": step_message, "number": step_number,
                               "author": preview.author(git, row["source_oid"])})
            message, number = series[-1]["message"], series[-1]["number"]
        elif method != "ff":
            message, number = formal.commit_message(self.project, git, target["oid"], candidate["oid"],
                configuration, self.workspace, selected=combined["selected_commits"], revisions=revisions)
        plan = {**common, "tree_oid": tree_oid, "commit_series": series,
                "parents": [target["oid"], *(c["oid"] for c in candidates)] if method == "merge" else [target["oid"]],
                "dependencies": dependencies(files), "environment_policy": "native-content-v1",
                "transformed_files": [{"path": name, "before": original_blobs[name], "after": blob}
                                      for name, _, blob, _ in files if original_blobs[name] != blob],
                "test_target": checks[0]["target"], "test_seeds": checks[0]["seeds"],
                "commit_message": redact_for_persistence({"message": message})["message"],
                "number": number, "excluded_path_fingerprints": exclusions,
                "automatic_steps": [], "stop_after": "execute"}
        if configuration["plugins"]:
            snapshots = plugins.capture(self.project)
            if plugins.identities(snapshots) != configuration["plugins"]:
                raise PublishError("PUBLISH_PLAN_STALE")
            entries = plugins.entries(formal.tree_files(git, target["oid"]), files)
            bundle = {"plugins": snapshots, "selected": plugins.selected_commits(git, target["oid"], candidate["oid"], oids=combined["selected_commits"]),
                      "configuration": configuration, "entries": entries,
                      "project_config": {"name": self.project.name, "commit_format": configuration["commit_format"],
                                         "authorship": configuration["authorship"], "publish_number": number}}
            plan.update(plugins=configuration["plugins"], plugin_snapshot_ref=self.repo.storage.put_blob(encoded(bundle), media_type="application/json"))
        item.update(candidate=candidate, plan=plan, status="prepared", revision=item["revision"] + 1)
        item.update(redact_for_persistence({"title": title or source_ref + " → Formal " + target_branch,
                                          "owner": owner, "rework": rework}))
        item["binding"] = digest({"candidate": candidate, "plan": plan, "revision": item["revision"]})
        checked = git.run(["diff", "--check", target["oid"], tree_oid, "--"])
        if checked.returncode not in (0, 1, 2):
            raise PublishError("PUBLISH_CHECK_UNAVAILABLE")
        for check in [{"kind": "git_diff_check", "scope": "final_tree_whitespace",
                       "outcome": "passed" if checked.returncode == 0 else "failed",
                       "summary": (checked.stdout or checked.stderr)[:8000]}, formal.privacy(files, configuration, message)]:
            item["evidence"].append(redact_for_persistence({**check, "binding": item["binding"],
                "tree_oid": tree_oid, "id": self.operation_id + "-" + check["kind"], "producer": "protected_git",
                "recorded_at": now()}))
        ledger["items"][item_id] = item
        return self._save_item(ledger, item, ref)

    def _validate_formal(self, item):
        if item["plan"].get("destination") != "formal" or item["status"] != "prepared":
            raise PublishError("PUBLISH_ACTION_UNAVAILABLE")
        git = self._formal_git()
        if git.workspace != item["plan"]["repository"] or formal.policy_binding(self.project) != item["plan"]["policy_binding"]:
            raise PublishError("PUBLISH_PLAN_STALE")
        git.require_target(item["plan"]["target_ref"], item["plan"]["expected_target_oid"])
        return git

    def _verify(self, *, item_id, expected_revision):
        from .service import now
        from backend.core.loop.service_runtime import service_runtime
        runtime = service_runtime()
        if runtime is None or not callable(getattr(runtime, "verify_tree", None)):
            raise PublishError("PUBLISH_TEST_RUNTIME_UNAVAILABLE")
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        self._validate_formal(item)
        if any(e["outcome"] != "passed" for e in active_evidence(item) if e["kind"] in {"git_diff_check", "outbound_privacy"}):
            raise PublishError("PUBLISH_CHECKS_FAILED")
        files = formal.tree_files(CandidateGit(self.workspace), item["plan"]["tree_oid"])
        intent = {"id": self.operation_id, "kind": "verify", "binding": item["binding"], "state": "intent",
                  "created_at": now(), "actor": self.actor, "task_id": self.task_id,
                  "request_key": self.request_key, "runtime_owner": list(runtime.owner)}
        intent["runtime_key"] = runtime.bind_operation(intent, item_id, self.repo)
        item["operations"].append(intent)
        item["status"], item["revision"] = "checking", item["revision"] + 1
        self._save_item(ledger, item, ref)
        result = runtime.verify_tree(formal.image(files), item["plan"])
        return self._finish_check(item_id, self.operation_id, result, runtime.producers)

    def _finish_check(self, item_id, operation_id, result, producers, *, recovered=False):
        # A concurrent pause is the only permitted item mutation while a check
        # owns its lease. CAS retry rereads its intent, never repeats native work.
        for attempt in range(4):
            try:
                return self._record_check(item_id, operation_id, result, producers, recovered=recovered)
            except PublishError as exc:
                if exc.code != "PUBLISH_REVISION_CONFLICT" or attempt == 3:
                    raise

    def _record_check(self, item_id, operation_id, result, producers, *, recovered):
        from .service import now
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id)
        if item["operations"][-1]["id"] != operation_id or item["status"] not in {"checking", "pausing"}:
            raise PublishError("PUBLISH_REVISION_CONFLICT")
        operation = item["operations"][-1]
        if result.get("cleanup_pending"):
            operation.update(state="cleanup_pending", native_producers=redact_for_persistence(producers))
            item["revision"] += 1
            saved = self._save_item(ledger, item, ref)
            return {**saved, "effect_state": "ambiguous", "status": "needs_reconciliation",
                    "reason": "SANDBOX_CLEANUP_PENDING"}
        paused = item["status"] == "pausing"
        if item["plan"].get("environment_policy"):
            environment = result.get("environment", {})
            environment_passed = bool(environment.get("digest")) and not result.get("environment_error") and not paused
            item["evidence"].append({"id": operation_id + "-environment", "binding": item["binding"],
                "kind": "environment", "scope": "native_runtime_content_and_dependencies", "tree_oid": item["plan"]["tree_oid"],
                "outcome": "passed" if environment_passed else "failed", "environment": environment,
                "dependencies": item["plan"]["dependencies"], "recorded_at": now(), "producer": "runtime_content_witness",
                "summary": result.get("environment_error") or ("PUBLISH_CHECK_PAUSED" if paused else "PUBLISH_ENVIRONMENT_RECORDED" if environment_passed else "PUBLISH_ENVIRONMENT_UNAVAILABLE")})
        if item["plan"].get("plugins"):
            plugin_result = result.get("plugin_result", {})
            plugin_passed = (not paused and plugin_result.get("passed") is True
                             and plugin_result.get("validated_plan") == digest(item["plan"]))
            if plugin_passed and plugin_result.get("selection"):
                try:
                    self._apply_plugin_selection(item, plugin_result, operation_id)
                    plugin_result = {**plugin_result, "error": "PUBLISH_PLUGIN_SELECTION_REVIEW_REQUIRED"}
                except PublishError as exc:
                    plugin_result = {**plugin_result, "error": exc.code}
                plugin_passed = False
                result = {**result, "passed": False, "error": plugin_result["error"]}
            if plugin_passed and plugin_result["message"] != item["plan"]["commit_message"]:
                prior = active_evidence(item)
                item["plan"]["commit_message"] = plugin_result["message"]
                item["binding"] = digest({"candidate": item["candidate"], "plan": item["plan"], "revision": item["revision"] + 1})
                for evidence in prior:
                    if evidence["kind"] in {"git_diff_check", "outbound_privacy", "environment"}:
                        # Whitespace is unchanged; privacy was revalidated on
                        # this exact tree and proposed message by the parent.
                        item["evidence"].append({**evidence, "binding": item["binding"],
                            "id": operation_id + "-" + evidence["kind"], "recorded_at": now(), "derived_from": evidence["id"]})
            item["evidence"].append(redact_for_persistence({"id": operation_id + "-plugins", "binding": item["binding"],
                "kind": "commit_plugins", "scope": "fixed_plugins_selection_message_and_pre_sync", "tree_oid": item["plan"]["tree_oid"],
                "outcome": "passed" if plugin_passed else "failed", "plugins": item["plan"]["plugins"],
                "summary": "PUBLISH_CHECK_PAUSED" if paused else plugin_result.get("error") or result.get("error") or (
                    "Commit plugin checks passed" if plugin_passed else "PUBLISH_PLUGIN_RESULT_MISSING"),
                "detail": plugin_result.get("summary", ""),
                "observations": plugin_result.get("observations", []), "recorded_at": now(), "producer": "registered_native_plugin"}))
        check = {"id": operation_id, "binding": item["binding"], "kind": "project_test",
                 "scope": "declared_pytest_on_final_tree", "tree_oid": item["plan"]["tree_oid"],
                 "outcome": "passed" if not paused and check_policy.accepted(item["plan"], result) else "failed",
                 "summary": "PUBLISH_CHECK_PAUSED" if paused else result.get("error") or "\n".join(r.get("output_tail", "") for r in result.get("seed_results", []))[:8000],
                 "test_target": item["plan"]["test_target"], "test_seeds": item["plan"]["test_seeds"],
                 "checks": result.get("check_results", []),
                 "producer": "registered_native_test", "recorded_at": now(),
                 "runtime_owner": operation["runtime_owner"], "native_producers": producers,
                 "recovered": recovered}
        item["evidence"].append(redact_for_persistence(check))
        operation.update(state="observed", outcome=check["outcome"], observed_at=now(), recovered=recovered)
        item["status"], item["revision"] = "paused" if paused else "prepared", item["revision"] + 1
        return self._save_item(ledger, item, ref)

    def _execute(self, *, item_id, expected_revision):
        from .service import now
        ledger, ref = self.repo.load()
        item = find_item(ledger, item_id, expected_revision)
        target_git = self._validate_formal(item)
        reviews = [r for r in item.get("reviews", []) if r["binding"] == item["binding"]]
        if reviews and reviews[-1]["decision"] != "accept":
            raise PublishError("PUBLISH_REVIEW_REQUIRED")
        if not formal_ready(item):
            raise PublishError("PUBLISH_CHECKS_REQUIRED")
        if item["plan"].get("environment_policy"):
            from .environment import snapshot
            previous = next(e["environment"] for e in active_evidence(item) if e["kind"] == "environment")
            if snapshot()["digest"] != previous["digest"]:
                raise PublishError("PUBLISH_ENVIRONMENT_CHANGED")
        plan, git = item["plan"], CandidateGit(self.workspace)
        if plan.get("plugins"):
            from backend.core.loop.service_runtime import service_runtime
            runtime = service_runtime()
            if runtime is None or not callable(getattr(runtime, "complete_tree", None)):
                raise PublishError("PUBLISH_TEST_RUNTIME_UNAVAILABLE")
        result_oid = item["candidate"]["oid"]
        if plan["method"] != "ff":
            self.effect_started = True
            if plan.get("commit_series"):
                result_oid = plan["expected_target_oid"]
                for step in plan["commit_series"]:
                    if step.get("author") != preview.author(git, step["source_oid"]):
                        raise PublishError("PUBLISH_PLAN_STALE")
                    result_oid = preview.commit(git, step["tree_oid"], [result_oid], step["message"], author=step["author"])
            else:
                result_oid = preview.commit(git, plan["tree_oid"], plan.get("parents", [plan["expected_target_oid"]]), plan["commit_message"])
        if git.oid(result_oid, "tree") != plan["tree_oid"]:
            raise PublishError("PUBLISH_PLAN_STALE")
        intent = {"id": self.operation_id, "kind": "formal", "request_key": self.request_key,
            "binding": item["binding"], "candidate": deepcopy(item["candidate"]), "plan": deepcopy(plan),
            "result_oid": result_oid, "state": "intent", "created_at": now(), "actor": self.actor, "task_id": self.task_id,
            "completion_required": bool(plan.get("plugins"))}
        item["operations"].append(intent)
        item["status"], item["revision"] = "executing", item["revision"] + 1
        self._save_item(ledger, item, ref)
        try:
            result = target_git.integrate({"repository": self.workspace}, plan, result_oid)
        except RefWriteRefused as exc:
            return self._refused(item_id, intent["id"], exc.code)
        except (PublishError, OSError, RuntimeError) as exc:
            return {"item": projection(item, None), "status": "needs_reconciliation", "effect_state": "ambiguous",
                    "reason": getattr(exc, "code", "PUBLISH_GIT_UNAVAILABLE")}
        if plan.get("plugins") and result.returncode > 0 and self._observe_formal_failure(item_id):
            return self._start_plugin_completion(item_id)
        if result.returncode > 0:
            return self._refused(item_id, intent["id"], "PUBLISH_REF_TRANSACTION_REJECTED")
        observed = self._observe(item_id, intent["id"], result.returncode == 0)
        if plan.get("plugins") and observed.get("effect_state") == "committed":
            return self._start_plugin_completion(item_id)
        return observed
