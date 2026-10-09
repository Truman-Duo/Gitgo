"""Task-kind-aware completion claims and deterministic host evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
import hashlib
import re

from backend.core.loop.operation_policy import is_effectful_mutation


class TaskKind(str, Enum):
    SUPERVISOR = "supervisor"
    ANSWER = "answer"
    ACTION = "action"
    PLAN = "plan"
    REVIEW = "review"


@dataclass(frozen=True)
class CompletionClaim:
    result: str
    verification: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    files: tuple[str, ...] = field(default_factory=tuple)
    diff_summary: str = ""
    manual_evidence: str = ""
    declared_at_step: int = 0
    declared_at: str = ""
    source: str = "complete_task"

    @classmethod
    def from_args(cls, args: dict, *, step: int) -> "CompletionClaim":
        result = str(args.get("result", "")).strip()
        if not result:
            raise ValueError("complete_task requires a non-empty result")
        verification_raw = args.get("verification", []) or []
        if not isinstance(verification_raw, list):
            raise ValueError("complete_task.verification must be an array")
        verification = tuple(
            dict(item) for item in verification_raw if isinstance(item, dict)
        )
        files = tuple(str(item) for item in (args.get("files", []) or []))
        return cls(
            result=result,
            verification=verification,
            files=files,
            diff_summary=str(args.get("diff_summary", "")).strip(),
            manual_evidence=str(args.get("manual_evidence", "")).strip(),
            declared_at_step=step,
            declared_at=datetime.now().isoformat(),
            source="complete_task",
        )

    @classmethod
    def from_response(cls, response: str, *, step: int) -> "CompletionClaim":
        """Capture an explicit model completion marker as a semantic claim.

        This does not manufacture action evidence.  The model supplied the
        declaration; receipts, tests and review remain independent Host facts.
        """
        result = re.sub(r"(?m)^\s*TASK_COMPLETE\s*$", "", response).strip()
        result = re.sub(r"(?im)^\s*FINAL_ANSWER\s*:\s*", "", result).strip()
        return cls(
            result=result or "Task completed",
            declared_at_step=step,
            declared_at=datetime.now().isoformat(),
            source="final_response",
        )

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CompletionClaim | None":
        if not isinstance(raw, dict) or not str(raw.get("result", "")).strip():
            return None
        return cls(
            result=str(raw.get("result", "")),
            verification=tuple(
                dict(item) for item in (raw.get("verification") or [])
                if isinstance(item, dict)
            ),
            files=tuple(str(item) for item in (raw.get("files") or [])),
            diff_summary=str(raw.get("diff_summary") or ""),
            manual_evidence=str(raw.get("manual_evidence") or ""),
            declared_at_step=int(raw.get("declared_at_step", 0) or 0),
            declared_at=str(raw.get("declared_at") or ""),
            source=str(raw.get("source") or "complete_task"),
        )

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "verification": [dict(v) for v in self.verification],
            "files": list(self.files),
            "diff_summary": self.diff_summary,
            "manual_evidence": self.manual_evidence,
            "declared_at_step": self.declared_at_step,
            "declared_at": self.declared_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class HostEvaluation:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class HostCompletionEvidence:
    """Host-observed facts, deliberately separate from the model claim."""

    factual_ready: bool
    action_receipt_ids: tuple[str, ...] = field(default_factory=tuple)
    required_test_ids: tuple[str, ...] = field(default_factory=tuple)
    test_failures: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "factual_ready": self.factual_ready,
            "action_receipt_ids": list(self.action_receipt_ids),
            "required_test_ids": list(self.required_test_ids),
            "test_failures": list(self.test_failures),
            "reasons": list(self.reasons),
        }

    @classmethod
    def collect(cls, process) -> "HostCompletionEvidence":
        all_receipts = _current_task_receipts(process)
        receipts = tuple(
            str(item.get("receipt_id"))
            for item in all_receipts
            if item.get("receipt_id")
            and item.get("succeeded") is True
            and item.get("committed") is True
            and is_effectful_mutation(str(item.get("effect", "read")))
        )
        required = tuple(str(item) for item in (
            getattr(process, "required_test_ids", []) or []
        ) if str(item))
        failures: tuple[str, ...] = ()
        reasons: list[str] = []
        if not receipts:
            reasons.append("no successful committed effectful receipt")
        if required:
            current_test_ids = {
                str(item.get("test_id") or "")
                for item in all_receipts
                if item.get("tool_name") == "run_test"
                and item.get("succeeded") is True
                and item.get("committed") is True
                and item.get("test_passed") is True
                and item.get("test_id")
            }
            missing_current = [
                item for item in required if item not in current_test_ids
            ]
            if missing_current:
                failures = tuple(
                    f"{item}:not_run_by_current_process"
                    for item in missing_current
                )
            workspace = str(getattr(process, "worktree_path", ""))
            if not workspace:
                failures += tuple(
                    f"{item}:workspace_unavailable" for item in required
                )
            else:
                from backend.core.loop.test_manifest import TestManifest
                _passed, raw_failures = TestManifest.load(workspace).evaluate_required(
                    list(required)
                )
                failures += tuple(raw_failures)
            if failures:
                reasons.append("required tests are not all passing")
        return cls(
            factual_ready=not reasons,
            action_receipt_ids=receipts,
            required_test_ids=required,
            test_failures=failures,
            reasons=tuple(reasons),
        )


class HostCompletionEvaluator:
    """Non-agent hard gates. Neither A nor B can override these checks."""

    @classmethod
    def outstanding_gates(cls, process, signals: list | None = None) -> dict:
        """Project the authoritative evaluator into an actionable checklist.

        This is a read model over :meth:`evaluate`, not another completion
        policy.  It lets the Host do receipt/test/child bookkeeping once so the
        model does not burn turns rediscovering why completion was rejected.
        """
        claim = getattr(process, "completion_claim", None)
        review_claim = getattr(process, "review_claim", None)
        probe = (
            str(getattr(claim, "result", "") or "")
            or str((review_claim or {}).get("summary", ""))
            or "Host completion readiness probe"
        )
        evaluation = cls.evaluate(process, probe, signals)

        def classify(reason: str) -> tuple[str, list[str], bool]:
            lowered = reason.casefold()
            if lowered.startswith("engineering workflow"):
                return "engineering_evidence", [
                    "inspect engineering_workflow status and satisfy the ready evidence nodes",
                    "if unavailable, preserve the missing facts and request a scope decision",
                ], "(decision)" not in lowered
            if "test" in lowered:
                return "test_evidence", [
                    "run the required test through the registered test system",
                    "fix the failing check and rerun it",
                ], True
            if "review" in lowered or "approval" in lowered:
                return "verification", [
                    "satisfy the verification plan with deterministic checks or an independent review",
                    "if unavailable, present a completion-exception decision to the user",
                ], True
            if "delegated" in lowered or "child" in lowered:
                return "subprocess", [
                    "wait for, recover, replace, or cancel the required subprocess",
                    "review its terminal outcome and record the decision",
                ], True
            if "artifact" in lowered or "workspace" in lowered:
                return "deliverable", [
                    "create and verify the required workspace artifact",
                    "ask the user to reduce or accept incomplete scope if it is impossible",
                ], True
            if "receipt" in lowered or "effectful" in lowered:
                return "effect_evidence", [
                    "perform the intended operation through a registered tool",
                    "cite the Host receipt in the completion claim",
                ], False
            if "governance" in lowered or "signal" in lowered:
                return "governance", [
                    "inspect and resolve the identified governance signal",
                    "ask the user only when a material semantic choice remains",
                ], False
            return "completion", [
                "inspect decision_evidence and satisfy the stated Host fact",
                "if the fact is impossible in this environment, ask the user how to proceed",
            ], True

        gates = []
        for reason in evaluation.reasons:
            kind, resolutions, degradable = classify(reason)
            digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
            gates.append({
                "gate_id": f"gate_{digest}",
                "kind": kind,
                "message": reason,
                "source": "HostCompletionEvaluator",
                "blocking": True,
                "degradable_by_explicit_user_decision": degradable,
                "resolution_options": resolutions,
            })
        return {
            "ready": evaluation.allowed,
            "gate_count": len(gates),
            "gates": gates,
        }

    @classmethod
    def evaluate(cls, process, response: str, signals: list | None = None) -> HostEvaluation:
        reasons: list[str] = []
        from backend.core.loop.engineering_workflow import EngineeringWorkflow
        try:
            if getattr(process, "read_context_snapshot", None):
                with process._context_lock:
                    reasons.extend(EngineeringWorkflow(process).completion_reasons())
        except (ValueError, KeyError, TypeError, OSError) as exc:
            reasons.append(f"engineering workflow recovery requires inspection: {exc}")
        try:
            task_kind = TaskKind(getattr(process, "task_kind", TaskKind.ANSWER.value))
        except ValueError:
            reasons.append(f"unknown task_kind: {getattr(process, 'task_kind', '')}")
            return HostEvaluation(False, tuple(reasons))

        if task_kind == TaskKind.SUPERVISOR:
            cls._evaluate_supervisor(process, response, reasons)
        elif task_kind == TaskKind.ACTION:
            claim = getattr(process, "completion_claim", None)
            if claim is None:
                reasons.append("action task requires an explicit semantic completion claim")
            else:
                cls._verify_receipt_references(process, claim, reasons)
            evidence = HostCompletionEvidence.collect(process)
            reasons.extend(evidence.reasons)
            reasons.extend(
                f"required test failed: {failure}"
                for failure in evidence.test_failures
            )
            cls._verify_required_tool_calls(process, reasons)
            from backend.core.loop.verification_policy import verification_plan
            plan = verification_plan(process)
            if plan.requires_independent_review and not getattr(
                process, "review_approvals", []
            ):
                reasons.append(
                    "independent reviewer approval is required "
                    f"(verification level {plan.level}: {', '.join(plan.reasons)})"
                )
        elif task_kind == TaskKind.REVIEW:
            review_claim = getattr(process, "review_claim", None)
            if not isinstance(review_claim, dict):
                reasons.append("review task requires a complete_review claim")
            elif review_claim.get("verdict") not in {"approved", "changes_required"}:
                reasons.append("review verdict must be approved or changes_required")
            elif not review_claim.get("summary"):
                reasons.append("review claim requires a summary")
        elif not response.strip():
            reasons.append(f"{task_kind.value} task requires a non-empty final response")

        for signal in signals or []:
            severity = getattr(getattr(signal, "severity", None), "value", "")
            category = getattr(getattr(signal, "category", None), "value", "")
            if severity == "critical" and category == "block":
                reasons.append(
                    "active critical governance block: "
                    + str(getattr(signal, "rule", "unspecified"))[:160]
                )

        if getattr(process, "actor_kind", "") == "supervisor":
            manager = getattr(process, "_manager", None)
            unresolved = []
            child_ids = (
                process.coordination_snapshot()[0]
                if getattr(process, "coordination_snapshot", None)
                else list(getattr(process, "child_ids", []) or [])
            )
            for child_id in child_ids:
                child = manager.get(child_id) if manager else None
                if child is not None and child.status.value in {
                    "running", "waiting", "cancelling", "awaiting_user",
                    "resume_available", "recovery_review_required", "recovering",
                }:
                    unresolved.append(child_id)
            if unresolved:
                reasons.append(
                    "delegated agents are still unresolved: " + ", ".join(unresolved)
                )

        return HostEvaluation(not reasons, tuple(reasons))

    @classmethod
    def _evaluate_supervisor(cls, process, response: str,
                             reasons: list[str]) -> None:
        """Validate delegated delivery without pretending A performed B's work."""
        if not response.strip():
            reasons.append("supervisor task requires a non-empty final response")

        manager = getattr(process, "_manager", None)
        if getattr(process, "coordination_snapshot", None):
            _child_ids, contracts, reviews = process.coordination_snapshot()
        else:
            contracts = dict(getattr(process, "delegated_contracts", {}) or {})
            reviews = dict(getattr(process, "child_reviews", {}) or {})
        from backend.core.loop.task_contract import get_task_contract
        root_contract = get_task_contract(process)
        required_minimum = int(
            root_contract.get("minimum_delegated_outcomes", 0) or 0
        )
        approved_required = 0
        for child_id, contract in contracts.items():
            if not contract.get("required_for_parent_completion", True):
                continue
            if contract.get("superseded_by"):
                continue
            child_reason_start = len(reasons)
            child = manager.get(child_id) if manager else None
            if child is None:
                reasons.append(f"required delegated agent is missing: {child_id}")
                continue
            result = child.result if isinstance(child.result, dict) else {}
            if child.status.value != "completed" or result.get("status") != "completed":
                reasons.append(
                    f"required delegated agent did not complete: {child_id} "
                    f"({child.status.value})"
                )
                continue
            review = reviews.get(child_id, {})
            if review.get("verdict") != "approved" or not review.get("summary"):
                reasons.append(
                    f"required delegated outcome lacks A structured approval: {child_id}"
                )
                continue
            if child.task_kind == TaskKind.ACTION.value:
                worktree = dict(getattr(child, "worktree", {}) or {})
                if (
                    worktree.get("isolated")
                    and worktree.get("own_commit")
                    and not worktree.get("promoted")
                ):
                    reasons.append(
                        f"approved child changes are not promoted: {child_id}"
                    )
                known_receipts = {
                    str(item.get("receipt_id", ""))
                    for item in getattr(child, "tool_receipts", []) or []
                    if item.get("receipt_id")
                }
                committed_action_receipts = {
                    str(item.get("receipt_id", ""))
                    for item in getattr(child, "tool_receipts", []) or []
                    if item.get("receipt_id")
                    and item.get("succeeded") is True
                    and item.get("committed") is True
                    and is_effectful_mutation(str(item.get("effect", "read")))
                }
                reviewed_receipts = {
                    str(item) for item in review.get("receipt_ids", []) if str(item)
                }
                if not reviewed_receipts:
                    reasons.append(
                        f"A approval cites no child tool receipt: {child_id}"
                    )
                elif not reviewed_receipts.issubset(known_receipts):
                    reasons.append(
                        f"A approval cites unknown child tool receipt: {child_id}"
                    )
                elif not reviewed_receipts.intersection(committed_action_receipts):
                    reasons.append(
                        "A approval cites no successful committed child action receipt: "
                        f"{child_id}"
                    )
            excluded = (root_contract.get("host_requirements") or {}).get("excluded_process_ids", [])
            if len(reasons) == child_reason_start and child_id not in excluded:
                approved_required += 1

        if approved_required < required_minimum:
            reasons.append(
                "[GITGO-E6101 REQUIRED_DELEGATION_UNSATISFIED] "
                f"task contract requires {required_minimum} approved delegated "
                f"outcome(s), Host observed {approved_required}"
            )
        cls._evaluate_required_deliverables(process, root_contract, reasons)

    @staticmethod
    def _missing_required_deliverables(process, contract: dict) -> list[str]:
        workspace_text = str(
            getattr(process, "workspace_root", "")
            or getattr(process, "worktree_path", "")
        )
        if not workspace_text:
            return [
                str(item.get("path") or item.get("description") or "<unknown>")
                for item in list(contract.get("deliverables") or [])
                if item.get("required", True) and item.get("kind") == "workspace_file"
            ]
        workspace = Path(workspace_text).resolve()
        missing = []
        for raw in list(contract.get("deliverables") or []):
            item = dict(raw or {})
            if not item.get("required", True) or item.get("kind") != "workspace_file":
                continue
            path = str(item.get("path") or "")
            candidate = (workspace / path).resolve(strict=False)
            try:
                candidate.relative_to(workspace)
            except ValueError:
                missing.append(path or "<invalid-path>")
                continue
            if not candidate.is_file():
                missing.append(path)
        return missing

    @classmethod
    def _evaluate_required_deliverables(
        cls, process, contract: dict, reasons: list[str],
    ) -> None:
        for path in cls._missing_required_deliverables(process, contract):
            reasons.append(
                "[GITGO-E6102 REQUIRED_ARTIFACT_MISSING] "
                f"required workspace artifact is missing: {path}"
            )

    @classmethod
    def evaluate_supervisor_failure(cls, process, response: str) -> HostEvaluation:
        """Validate a truthful terminal A report for failed delegated work.

        Failure is not successful completion, but it still needs a bounded
        protocol exit. The Host derives failure from child lifecycle and review
        facts so a model cannot merely claim failure to bypass delivery gates.
        """
        reasons: list[str] = []
        if not response.strip():
            reasons.append("failed supervision requires a non-empty final report")
        manager = getattr(process, "_manager", None)
        if getattr(process, "coordination_snapshot", None):
            _child_ids, contracts, reviews = process.coordination_snapshot()
        else:
            contracts = dict(getattr(process, "delegated_contracts", {}) or {})
            reviews = dict(getattr(process, "child_reviews", {}) or {})
        required = [
            (child_id, contract) for child_id, contract in contracts.items()
            if contract.get("required_for_parent_completion", True)
            and not contract.get("superseded_by")
        ]
        from backend.core.loop.task_contract import get_task_contract
        root_contract = get_task_contract(process)
        failed_attempts = [
            dict(item) for item in list(root_contract.get("delegation_attempts") or [])
            if item.get("required_for_parent_completion", True)
            and item.get("state") == "admission_failed"
        ]
        missing_deliverables = cls._missing_required_deliverables(
            process, root_contract,
        )
        contract_requires_delegation = int(
            root_contract.get("minimum_delegated_outcomes", 0) or 0
        ) > 0
        if not required and not failed_attempts and not missing_deliverables:
            reasons.append(
                "[GITGO-E6104 FAILURE_EVIDENCE_INSUFFICIENT] "
                "no failed admission, required artifact, or delegated outcome "
                "can substantiate failure"
            )
        failure_observed = False
        active = {
            "running", "waiting", "cancelling", "awaiting_user",
            "resume_available", "recovery_review_required", "recovering",
        }
        for child_id, _contract in required:
            child = manager.get(child_id) if manager else None
            if child is None:
                reasons.append(f"required delegated agent is missing: {child_id}")
                continue
            status = child.status.value
            if status in active:
                reasons.append(f"required delegated agent is unresolved: {child_id}")
                continue
            review = reviews.get(child_id, {})
            verdict = str(review.get("verdict") or "")
            if verdict not in {"approved", "changes_required"} or not review.get("summary"):
                reasons.append(
                    f"required delegated outcome lacks structured review: {child_id}"
                )
                continue
            worktree = dict(getattr(child, "worktree", {}) or {})
            delivery_failed = (
                status != "completed"
                or verdict == "changes_required"
                or (
                    worktree.get("isolated")
                    and worktree.get("own_commit")
                    and not worktree.get("promoted")
                )
            )
            if delivery_failed:
                failure_observed = True
                if status != "completed" and verdict != "changes_required":
                    reasons.append(
                        "failed delegated outcome must be reviewed as "
                        f"changes_required: {child_id}"
                    )
        if failed_attempts:
            last_error = dict(failed_attempts[-1].get("error_info") or {})
            recoverable = bool(last_error.get("retryable", False))
            prescribed = list(last_error.get("next_actions") or [])
            if recoverable and prescribed and len(failed_attempts) < 2:
                reasons.append(
                    "recoverable delegation admission error still has an untried "
                    "Host-prescribed next action"
                )
            else:
                failure_observed = True
        if missing_deliverables:
            failure_observed = True
        if contract_requires_delegation and not required and not failed_attempts:
            reasons.append(
                "[GITGO-E6101 REQUIRED_DELEGATION_UNSATISFIED] "
                "required delegation was never admitted or recorded as failed"
            )
        if required and not failure_observed:
            reasons.append("Host facts do not contain a failed delegated delivery")
        return HostEvaluation(not reasons, tuple(reasons))

    @staticmethod
    def _verify_receipt_references(process, claim: CompletionClaim, reasons: list[str]) -> None:
        known: dict[str, dict] = {}
        pending = _current_task_receipts(process)
        while pending:
            receipt = pending.pop()
            if not isinstance(receipt, dict):
                continue
            receipt_id = str(receipt.get("receipt_id", ""))
            if receipt_id:
                known[receipt_id] = receipt
            # Composite component receipts are generated by the Host pipeline
            # and nested under the committed parent receipt.  They are valid
            # provenance references even though only the parent is appended to
            # process.tool_receipts as the model-visible operation.
            children = receipt.get("child_receipts", [])
            if isinstance(children, list):
                pending.extend(children)
        for evidence in claim.verification:
            kind = str(evidence.get("kind") or "").strip().casefold()
            receipt_id = str(evidence.get("receipt_id", ""))
            if receipt_id and receipt_id not in known:
                reasons.append(f"unknown tool receipt referenced: {receipt_id}")
                continue
            if kind not in {"dynamic_definition", "component_step", "test"}:
                continue
            if not receipt_id:
                reasons.append(
                    f"{kind} verification requires a concrete Host receipt_id"
                )
                continue
            receipt = known[receipt_id]
            if receipt.get("succeeded") is not True or receipt.get("committed") is not True:
                reasons.append(f"{kind} verification cites an unsuccessful receipt")
                continue
            if kind == "dynamic_definition":
                expected_name = str(evidence.get("name") or "")
                if receipt.get("tool_name") != "define_tool":
                    reasons.append("dynamic_definition must cite a define_tool receipt")
                elif expected_name and str(receipt.get("dynamic_tool_name") or "") != expected_name:
                    reasons.append("dynamic_definition receipt names a different tool")
                expected_digest = str(evidence.get("digest") or "")
                if expected_digest and str(receipt.get("definition_digest") or "") != expected_digest:
                    reasons.append("dynamic_definition receipt digest does not match")
            elif kind == "component_step":
                expected_tool = str(evidence.get("tool") or "")
                expected_step = str(evidence.get("step_id") or "")
                if not receipt.get("composite_tool") or not receipt.get("composite_step"):
                    reasons.append("component_step must cite a nested composite receipt")
                elif expected_tool and str(receipt.get("tool_name") or "") != expected_tool:
                    reasons.append("component_step receipt names a different base tool")
                elif expected_step and str(receipt.get("composite_step") or "") != expected_step:
                    reasons.append("component_step receipt names a different step")
            elif kind == "test":
                expected_id = str(evidence.get("test_id") or "")
                if receipt.get("tool_name") != "run_test":
                    reasons.append("test verification must cite a run_test receipt")
                elif receipt.get("test_passed") is not True:
                    reasons.append("test verification cites a non-passing run_test receipt")
                elif expected_id and str(receipt.get("test_id") or "") != expected_id:
                    reasons.append("test receipt id does not match verification")

    @staticmethod
    def _verify_required_tool_calls(process, reasons: list[str]) -> None:
        """Enforce LLM-compiled task requirements with Host receipt counts."""
        from backend.core.loop.task_contract import get_task_contract

        requirements = list(
            get_task_contract(process).get("required_tool_calls") or []
        )
        if not requirements:
            return
        top_level = _current_task_receipts(process)
        for raw in requirements:
            requirement = dict(raw or {})
            name = str(requirement.get("tool_name") or "")
            candidates = list(top_level)
            if requirement.get("include_composite_steps"):
                pending = list(top_level)
                while pending:
                    current = pending.pop()
                    children = current.get("child_receipts", [])
                    if isinstance(children, list):
                        nested = [item for item in children if isinstance(item, dict)]
                        candidates.extend(nested)
                        pending.extend(nested)
            count = sum(
                1 for item in candidates
                if str(item.get("tool_name") or "") == name
                and item.get("succeeded") is True
                and item.get("committed") is True
            )
            minimum = int(requirement.get("min_calls", 1) or 0)
            maximum = requirement.get("max_calls")
            if count < minimum:
                reasons.append(
                    f"required tool {name} has {count} committed successful "
                    f"call(s); minimum is {minimum}"
                )
            if maximum is not None and count > int(maximum):
                reasons.append(
                    f"required tool {name} has {count} committed successful "
                    f"call(s); maximum is {int(maximum)}"
                )


def _current_task_receipts(process) -> list[dict]:
    """Return current-task receipts while retaining pre-migration compatibility.

    New receipts carry task_id.  A receipt explicitly bound to another task is
    never completion evidence for the active task.  Legacy receipts without a
    task_id remain readable so an in-flight process created before this schema
    addition can still finish instead of being stranded during an upgrade.
    """
    active = str(
        getattr(process, "active_task_id", "")
        or getattr(process, "process_id", "")
    )
    return [
        item for item in (getattr(process, "tool_receipts", []) or [])
        if isinstance(item, dict)
        and (not item.get("task_id") or str(item.get("task_id")) == active)
    ]


COMPLETE_TASK_PARAMETERS = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "maxLength": 2400, "description": (
            "The actual user-facing final answer or delivery summary. State the result, "
            "how to use or locate it, the core approach, and only material limitations. "
            "Keep it concise (normally no more than eight short lines); a single sentence "
            "that verification passed is enough. Do not enumerate test cases or put search "
            "attempts, provider/network diagnostics, process IDs, receipts, hashes, "
            "governance gates, validation procedure, or internal execution narration here "
            "unless the user explicitly requested an audit. Put verification facts in the "
            "verification field instead."
        )},
        "verification": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Verification records, preferably referencing host receipt_id values.",
        },
        "files": {"type": "array", "items": {"type": "string"}},
        "diff_summary": {"type": "string"},
        "manual_evidence": {"type": "string"},
    },
    "required": ["result", "verification"],
}
