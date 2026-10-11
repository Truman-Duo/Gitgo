"""Host-owned engineering practices over the existing task/evidence protocol.

This is not a skill loader. Models propose semantic work; one bounded DAG
validates dependencies, receipts, user answers and immutable reports. All
workspace actions and permissions still go through ToolPipeline. Checkpoints
already persist context_snapshot, so no parallel workflow database is needed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from .operation_policy import is_effectful_mutation

LOG = logging.getLogger(__name__)
KEY = "engineering_workflow"
PROFILES = ("alignment", "domain_modeling", "diagnosis", "tdd", "architecture",
            "retrospective", "agent_documentation")
KINDS = ("decision", "observation", "check", "change", "document", "report")
REPORT_FIELDS = {
    "alignment": ("decisions", "unresolved"),
    "hypotheses": ("symptom", "hypotheses"),
    "architecture": ("candidates",),
    "cleanup": ("removed_instrumentation", "remaining_limitations"),
    "retrospective": ("findings",),
    "agent_documentation": ("triggers", "completion_criteria", "references"),
}


def practice_plan(config: dict) -> dict:
    """Compile built-in practices into the same graph used for custom plans.

    Tests and target paths are semantic proposals, never keyword inference.
    Missing inputs trigger recovery rather than silently weakening a practice.
    """
    profiles = config.get("profiles") or []
    nodes, prerequisites = [], []

    def add(name, kind, **fields):
        nodes.append({"id": name, "kind": kind, "depends_on": list(prerequisites), **fields})

    if "alignment" in profiles:
        for item in config.get("questions") or []:
            add(item["id"], "decision", state_topic=item["state_topic"],
                depends_on=item.get("depends_on", []), before_mutation=True,
                **{k: item[k] for k in ("request", "when") if k in item})
        if nodes:
            prerequisites.extend(n["id"] for n in nodes)
        else:
            add("alignment", "report", format="alignment", before_mutation=True)
            prerequisites.append("alignment")
    if "architecture" in profiles:
        add("inspection", "observation", tools=["read_file", "code_dossier"])
        prerequisites = ["inspection"]
        add("alternatives", "report", format="architecture", before_mutation=True)
        prerequisites = ["alternatives"]
    if "tdd" in profiles or "diagnosis" in profiles:
        test = config.get("test_id")
        if not isinstance(test, str) or not test.strip():
            raise ValueError("diagnosis/TDD presets require a concrete registered test_id")
        check_fields = {"tool_name": "exec_command", "argv": config["check_argv"], "cwd": config.get("check_cwd", ".")} if config.get("check_argv") else {}
        add("red", "check", test_id=test, passed=False, **check_fields)
        prerequisites = ["red"]
        if "diagnosis" in profiles:
            add("hypotheses", "report", format="hypotheses")
            prerequisites = ["hypotheses"]
        if "tdd" in profiles:
            add("change", "change", files=config.get("target_files") or [])
            prerequisites = ["change"]
        add("green", "check", test_id=test, passed=True, **check_fields)
        prerequisites = ["green"]
        if "diagnosis" in profiles:
            add("cleanup", "report", format="cleanup")
            prerequisites = ["cleanup"]
    if "domain_modeling" in profiles:
        add("glossary", "document", path=config.get("glossary_path", "GLOSSARY.md"))
        prerequisites.append("glossary")
    if "agent_documentation" in profiles:
        if not config.get("agent_document_path"):
            raise ValueError("agent documentation preset requires agent_document_path")
        add("agent_document", "document", path=config["agent_document_path"])
        prerequisites = ["agent_document"]
        add("navigation", "report", format="agent_documentation")
        prerequisites = ["navigation"]
    if "retrospective" in profiles:
        add("retro", "report", format="retrospective")
    return {"schema_version": 1, "profiles": profiles, "nodes": nodes,
            "preparation_files": config.get("preparation_files", [])}


def task_id(process):
    return str(process.active_task_id or process.process_id)


def workspace(process):
    raw = process.worktree_path or process.workspace_root
    if not raw:
        raise ValueError("engineering workflow requires an execution workspace")
    return Path(raw).resolve()


def _path(root: Path, raw: str) -> Path:
    if not raw or not isinstance(raw, str):
        raise ValueError("a concrete workspace path is required")
    target = (root / raw).resolve()
    target.relative_to(root)
    if target == root or ".git" in target.relative_to(root).parts:
        raise ValueError("workflow evidence cannot target the workspace root or .git")
    return target


def compile_plan(raw: dict, root: str | Path) -> dict:
    """Validate the entire proposal before publishing any part of it."""
    if not isinstance(raw, dict) or raw.get("schema_version", 1) != 1:
        raise ValueError("unsupported engineering workflow schema")
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > 64_000:
        raise ValueError("engineering workflow exceeds 64KB")
    if "nodes" not in raw:
        raw = practice_plan(raw)
    profiles = list(dict.fromkeys(raw.get("profiles") or []))
    if not profiles or any(item not in PROFILES for item in profiles):
        raise ValueError("profiles must select supported engineering practices")
    nodes = copy.deepcopy(raw.get("nodes") or [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 64:
        raise ValueError("workflow requires 1..64 concrete nodes")
    root = Path(root).resolve()
    ids = set()
    topics = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("workflow nodes must be objects")
        name = node.get("id", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in ids:
            raise ValueError("workflow node ids must be unique lowercase identifiers")
        ids.add(name)
        kind = node.get("kind")
        if kind not in KINDS:
            raise ValueError("unsupported workflow evidence kind")
        node["depends_on"] = list(dict.fromkeys(node.get("depends_on") or []))
        node["required"] = bool(node.get("required", True))
        node["before_mutation"] = bool(node.get("before_mutation", False))
        if node["before_mutation"] and kind == "change":
            raise ValueError("a change cannot be its own pre-mutation gate")
        if kind == "check" and (not node.get("test_id") or type(node.get("passed")) is not bool):
            raise ValueError("check nodes require test_id and an explicit passed boolean")
        if kind == "check":
            node["tool_name"] = node.get("tool_name", "run_test")
            if node["tool_name"] not in {"run_test", "exec_command"}:
                raise ValueError("check evidence must use registered tests or an exact argv command")
            if node["tool_name"] == "exec_command":
                if not isinstance(node.get("argv"), list) or not node["argv"] or not all(isinstance(a, str) for a in node["argv"]):
                    raise ValueError("command checks require exact argv")
                node["cwd"] = str(node.get("cwd") or ".")
                (root / node["cwd"]).resolve().relative_to(root)
        if kind == "decision" and not str(node.get("state_topic") or "").strip():
            raise ValueError("decision nodes require a stable state_topic")
        if kind == "decision":
            node["state_topic"] = str(node["state_topic"]).strip()
            if node["state_topic"] in topics:
                raise ValueError("decision state_topics must be unique within a workflow")
            topics.add(node["state_topic"])
        if kind == "decision" and "alignment" in profiles:
            node["before_mutation"] = True
        if kind == "document":
            node["path"] = _path(root, node.get("path")).relative_to(root).as_posix()
        if kind == "report" and node.get("format") not in REPORT_FIELDS:
            raise ValueError("report nodes require a supported structured format")
        if kind == "report" and node.get("format") == "alignment":
            node["before_mutation"] = True
        if kind == "observation":
            tools = node.get("tools") or []
            if not tools or not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                raise ValueError("observation nodes require concrete tool names")
        if kind == "change":
            files = node.get("files") or []
            if not files:
                raise ValueError("change nodes require target files")
            node["files"] = [_path(root, item).relative_to(root).as_posix() for item in files]
    by_id = {node["id"]: node for node in nodes}
    visiting, visited = set(), set()

    def visit(name):
        if name not in by_id:
            raise ValueError("workflow dependency references an unknown node")
        if name in visiting:
            raise ValueError("workflow dependency cycle")
        if name in visited:
            return
        visiting.add(name)
        for dep in by_id[name]["depends_on"]:
            visit(dep)
        visiting.remove(name)
        visited.add(name)

    for name in by_id:
        visit(name)
    @lru_cache(maxsize=64)
    def ancestors(name):
        return frozenset(by_id[name]["depends_on"]).union(*(ancestors(d) for d in by_id[name]["depends_on"]))

    for node in nodes:
        # Admitted evidence is mandatory; narrowing it requires a user-visible
        # amendment, rather than an optional flag supplied by the model.
        node["required"] = True
        if node.get("when"):
            if node["kind"] != "decision" or not isinstance(node["when"], dict):
                raise ValueError("conditional branches must be decision nodes")
            for parent, values in node["when"].items():
                if parent not in ancestors(node["id"]) or by_id[parent]["kind"] != "decision" or not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
                    raise ValueError("when must match nonempty user answers of ancestor decisions")
        if node.get("format") == "architecture":
            node["before_mutation"] = True
            if not any(by_id[a]["kind"] == "observation" for a in ancestors(node["id"])):
                raise ValueError("architecture alternatives must follow inspection evidence")
        if node.get("format") == "agent_documentation" and not any(by_id[a]["kind"] == "document" for a in ancestors(node["id"])):
            raise ValueError("navigation reports must follow a verified document")
    # Practice invariants are compiled, not left to a prompt convention.
    def has(kind, **fields):
        return any(n["kind"] == kind and all(n.get(k) == v for k, v in fields.items()) for n in nodes)

    if "alignment" in profiles and not (has("decision") or has("report", format="alignment")):
        raise ValueError("alignment requires decisions or an alignment report")
    if "domain_modeling" in profiles and not has("document"):
        raise ValueError("domain modeling requires a concrete glossary/ADR document")
    if "architecture" in profiles and not (has("observation") and has("report", format="architecture")):
        raise ValueError("architecture requires inspection evidence and an alternatives report")
    if "retrospective" in profiles and not has("report", format="retrospective"):
        raise ValueError("retrospective requires a findings report")
    if "agent_documentation" in profiles and not (has("document") and has("report", format="agent_documentation")):
        raise ValueError("agent documentation requires a document and its navigation contract")
    if "diagnosis" in profiles or "tdd" in profiles:
        reds = [n for n in nodes if n["kind"] == "check" and n["passed"] is False]
        greens = [n for n in nodes if n["kind"] == "check" and n["passed"] is True]

        def same_check(red, green):
            return (red["test_id"] == green["test_id"] and red["tool_name"] == green["tool_name"]
                    and (red["tool_name"] != "exec_command" or command_digest(red) == command_digest(green)))

        if not reds or not greens or not all(any(r["id"] in ancestors(g["id"]) and same_check(r, g) for r in reds) for g in greens):
            raise ValueError("diagnosis/TDD require ordered failing and passing evidence for the same test")
        for red in reds:
            red["before_mutation"] = True
        if "diagnosis" in profiles:
            if not has("report", format="hypotheses") or not has("report", format="cleanup"):
                raise ValueError("diagnosis requires hypothesis and cleanup reports")
            for node in nodes:
                if node.get("format") == "hypotheses":
                    if not any(r["id"] in ancestors(node["id"]) for r in reds):
                        raise ValueError("hypotheses must follow reproduction evidence")
                    node["before_mutation"] = True
            hypotheses_ids = [n["id"] for n in nodes if n.get("format") == "hypotheses"]
            if not all(any(h in ancestors(g["id"]) for h in hypotheses_ids) for g in greens):
                raise ValueError("diagnosis passing evidence must follow the hypothesis stage")
            if not all(any(g["id"] in ancestors(n["id"]) for g in greens) for n in nodes if n.get("format") == "cleanup"):
                raise ValueError("cleanup must follow passing verification")
        if "tdd" in profiles:
            if not has("change") or not all(any(by_id[a]["kind"] == "change" for a in ancestors(g["id"])) for g in greens):
                raise ValueError("TDD passing checks must depend on a recorded change")
            for node in nodes:
                if node["kind"] == "change" and not any(r["id"] in ancestors(node["id"]) for r in reds):
                    raise ValueError("TDD changes must follow failing evidence")
    preparation = [_path(root, p).relative_to(root).as_posix() for p in raw.get("preparation_files", [])]
    if set(preparation).intersection(p for n in nodes if n["kind"] == "change" for p in n["files"]):
        raise ValueError("evidence preparation paths cannot overlap product change targets")
    plan = {"schema_version": 1, "profiles": profiles, "nodes": nodes, "preparation_files": preparation}
    plan["digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


class EngineeringWorkflow:
    def __init__(self, process, on_event=None):
        self.process = process
        self.on_event = on_event

    def _load(self):
        if not getattr(self.process, "read_context_snapshot", None):
            return {}
        context, _ = self.process.read_context_snapshot()
        state = copy.deepcopy(context.get(KEY) or {})
        return state if state.get("task_id") == task_id(self.process) else {}

    def _save(self, state):
        self.process.update_context_snapshot(lambda ctx: {**ctx, KEY: copy.deepcopy(state)})

    def notice(self, code, message, **details):
        event = {"event": "progress_summary", "phase": KEY, "code": code,
                 "message": message, "task_id": task_id(self.process),
                 "process_id": self.process.process_id, **details}
        # An event transport failure never erases the durable, model-visible notice.
        self.process.session.host_ledger.append({**event, "event": "engineering_notice"})
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                LOG.exception("engineering notice delivery failed; retained in host ledger")
        return event

    def configure(self, raw):
        plan = compile_plan(raw, workspace(self.process))
        state = self.configured_state(plan)
        self._save(state)
        self.notice("WORKFLOW_CONFIGURED", "工程工作流已启用；依赖、证据和完成条件由 Host 检查。")
        return self.status()

    def configured_state(self, plan):
        """Pure publication preparation, also used by atomic contract admission."""
        state = self._load()
        if state:
            if state["plan"]["digest"] == plan["digest"]:
                return state
            # Additive evolution prevents changing a requirement into an easier
            # predicate or silently deleting a user decision during recovery.
            new = {n["id"]: n for n in plan["nodes"]}
            if not set(state["plan"]["profiles"]).issubset(plan["profiles"]) or any(new.get(n["id"]) != n for n in state["plan"]["nodes"]):
                raise ValueError("active workflow can only be extended; use explicit task scope amendment for replacement")
        else:
            state = {"schema_version": 1, "task_id": task_id(self.process), "records": {}, "requests": {},
                     "receipt_start": len(self.process.tool_receipts), "revision": 0}
        state["plan"] = plan
        state["revision"] += 1
        return state

    def _receipts(self):
        rows = []

        def walk(receipt, index):
            rows.append((index, receipt))
            for child in receipt.get("child_receipts") or []:
                walk(child, index)

        for index, receipt in enumerate(self.process.tool_receipts):
            if receipt.get("task_id") == task_id(self.process):
                walk(receipt, index)
        return rows

    def _valid_record(self, node, record):
        if record.get('defaulted'):
            source = self._answers(node)
            from .alignment_evidence import answer_digest
            digest = answer_digest(source[-1]['answer']) if source else ''
            return bool(record.get('source_digest') == digest and any(
                e.get('event') == 'alignment_assumption' and e.get('task_id') == task_id(self.process)
                and e.get('node_id') == node['id'] and e.get('value') == record.get('value')
                and e.get('source_digest') == digest for e in self.process.session.host_ledger))
        if record.get("inapplicable"):
            return True  # dependency digests below invalidate this projection
        if node["kind"] == "document":
            try:
                path = _path(workspace(self.process), node["path"])
                raw = path.read_bytes()
                if "agent_documentation" in self._load()["plan"]["profiles"]:
                    _validate_local_links(path, raw.decode("utf-8-sig"), workspace(self.process))
                return hashlib.sha256(raw).hexdigest() == record["digest"]
            except (OSError, ValueError, UnicodeError):
                return False
        if node["kind"] == "report":
            from .context_store import ContextObjectStore
            try:
                ContextObjectStore(workspace(self.process)).resolve(record["ref"])
                return True
            except (KeyError, OSError, ValueError):
                return False
        if node["kind"] == "decision":
            answers = self._answers(node)
            from .alignment_evidence import resolved_answer
            resolution = resolved_answer(self.process.session.host_ledger, answers[-1], node["state_topic"]) if answers else None
            return bool(resolution and answers[-1].get("decision_id") == record["decision_id"]
                        and resolution.get("value") == record.get("value")
                        and resolution.get('choice_key', resolution['value']) == record.get('choice_key', record['value']))
        ids = record.get("receipt_ids") or [record.get("receipt_id")]
        return all(any(r.get("receipt_id") == rid and (node["kind"] == "check" or r.get("committed") is True)
                       for _, r in self._receipts()) for rid in ids)

    def _answers(self, node):
        answers = []
        for event in self.process.session.host_ledger:
            if event.get("event") != "user_decision_received" or event.get("task_id") != task_id(self.process):
                continue
            if event.get("state_topic") == node["state_topic"]:
                answers.append(event)
            else:
                item = (event.get("answers") or {}).get(node["id"])
                if item and item.get("state_topic") == node["state_topic"]:
                    answers.append({**event, **item})
        from .alignment_evidence import source_answer, answer_digest
        for event in self.process.session.host_ledger:
            if (event.get('event') == 'user_decision_interpreted' and event.get('node_id') == node['id']
                    and event.get('state_topic') == node['state_topic'] and event.get('task_id') == task_id(self.process)):
                source = source_answer(self.process.session.host_ledger, task_id(self.process), event['decision_id'], event.get('source_node_id', ''))
                if source and answer_digest(source.get('answer', '')) == event['answer_digest'] and not any(a.get('decision_id') == source['decision_id'] for a in answers):
                    answers.append({**source, 'state_topic': node['state_topic']})
        order = {e.get('decision_id'): i for i, e in enumerate(self.process.session.host_ledger) if e.get('event') == 'user_decision_received'}
        answers.sort(key=lambda a: order.get(a.get('decision_id'), -1))
        return answers

    def _refresh(self, state):
        amendment = state.get("amendment")
        if amendment:
            answer = next((n for n in reversed(self.process.session.host_ledger)
                           if n.get("event") == "user_decision_received" and n.get("task_id") == task_id(self.process)
                           and n.get("decision_id") == amendment["decision_id"]), None)
            if answer and answer.get("selected_action") in {"accept_engineering_amendment", "keep_engineering_workflow"}:
                if answer["selected_action"] == "accept_engineering_amendment":
                    old = {n["id"]: n for n in state["plan"]["nodes"]}
                    plan = amendment["plan"]
                    unchanged = {n["id"] for n in plan["nodes"] if old.get(n["id"]) == n}
                    state["records"] = {k: v for k, v in state["records"].items() if k in unchanged}
                    state["requests"] = {k: v for k, v in state.get("requests", {}).items() if k in unchanged}
                    state["plan"] = plan
                    state["receipt_start"] = len(self.process.tool_receipts)
                state.pop("amendment")
                state["revision"] += 1
                if answer["selected_action"] == "accept_engineering_amendment":
                    self.process.update_context_snapshot(lambda ctx: {**ctx, KEY: copy.deepcopy(state), "task_contract": {
                        **dict(ctx.get("task_contract") or {}), KEY: copy.deepcopy(plan)}})
                    self.notice("WORKFLOW_SCOPE_AMENDED", "已按你的明确选择调整工程要求；未完成事实仍保留在历史中。")
                else:
                    self._save(state)
                    self.notice("WORKFLOW_SCOPE_KEPT", "已保留原工程要求。")
            elif answer and not amendment.get("discussion_received"):
                amendment["discussion_received"] = True
                self._save(state)
                self.notice("WORKFLOW_SCOPE_UNCONFIRMED", "已收到你的补充意见；尚未明确采用该范围调整，原要求继续有效。")
        nodes = state["plan"]["nodes"]
        records = state["records"]
        receipts = self._receipts()
        changed = False
        def dependency_version(node):
            return {d: hashlib.sha256(json.dumps(records.get(d), sort_keys=True).encode()).hexdigest()
                    for d in node["depends_on"]}
        # Invalidation propagates through dependencies, including decisions
        # amended by the user and documents changed after their last review.
        for _ in range(len(nodes) + 1):
            progress = False
            for node in nodes:
                name = node["id"]
                record = records.get(name)
                # A green result cannot certify later edits. Change evidence
                # also advances to the latest write, invalidating descendants.
                later_write = record and (node["kind"] == "change" or (node["kind"] == "check" and node["passed"])) and any(
                    index > record.get("receipt_index", -1) and r.get("committed") is True
                    and r.get("effect") == "workspace_write"
                    and (node["kind"] == "check" or bool(set(node["files"]).intersection(self._receipt_files(r))))
                    for index, r in receipts)
                if record and (later_write or any(d not in records for d in node["depends_on"]) or record.get("dependencies", {}) != dependency_version(node) or not self._valid_record(node, record)):
                    records.pop(name)
                    if node["kind"] == "decision" and (any(d not in records for d in node['depends_on'])
                            or record.get('dependencies', {}) != dependency_version(node)):
                        state.get("requests", {}).pop(name, None)
                    progress = changed = True
                    self.notice("WORKFLOW_EVIDENCE_STALE", f"步骤 {name} 的证据已失效，需要重新验证。", node_id=name)
                if name in records or any(d not in records for d in node["depends_on"]):
                    continue
                inactive = any(records[d].get("inapplicable") for d in node["depends_on"]) or any(
                    records[parent].get('choice_key', records[parent].get("value")) not in values for parent, values in (node.get("when") or {}).items())
                if inactive:
                    records[name] = {"inapplicable": True, "dependencies": dependency_version(node)}
                    progress = changed = True
                    self.notice("ALIGNMENT_BRANCH_INACTIVE", f"根据已确认答案，问题 {name} 的分支不适用。", node_id=name)
                    continue
                minimum = max([state["receipt_start"] - 1, *[records[d].get("receipt_index", state["receipt_start"] - 1) for d in node["depends_on"]]])
                if node["kind"] == "check" and node["passed"]:
                    minimum = max([minimum, *[index for index, r in receipts if r.get("effect") == "workspace_write" and r.get("committed") is True]])
                if node["kind"] == "change":
                    # File tools commonly produce one receipt per file. Bind
                    # the latest committed receipt for every declared target.
                    latest = {}
                    for index, receipt in receipts:
                        if index > minimum and receipt.get("effect") == "workspace_write" and receipt.get("succeeded") is True and receipt.get("committed") is True:
                            for path in set(node["files"]).intersection(self._receipt_files(receipt)):
                                latest[path] = (index, receipt["receipt_id"])
                    if set(node["files"]).issubset(latest):
                        records[name] = {"receipt_ids": list(dict.fromkeys(v[1] for v in latest.values())),
                                         "receipt_index": max(v[0] for v in latest.values()),
                                         "dependencies": dependency_version(node)}
                        progress = changed = True
                    continue
                for index, receipt in receipts:
                    if index <= minimum:
                        continue
                    kind = node["kind"]
                    matched = (kind == "check" and receipt.get("tool_name") == "run_test" and receipt.get("test_id") == node["test_id"] and receipt.get("test_completed") is True and bool(receipt.get("test_seeds")) and receipt.get("test_passed") is node["passed"])
                    if kind == "check" and node["tool_name"] == "exec_command":
                        matched = receipt.get("tool_name") == "exec_command" and receipt.get("command_digest") == command_digest(node) and receipt.get("command_exit_code") == (0 if node["passed"] else 1) and receipt.get("command_completed") is True
                    if kind == "observation":
                        matched = receipt.get("tool_name") in node["tools"] and receipt.get("succeeded") is True and receipt.get("effect") in {"read", "external_read"}
                    if matched:
                        records[name] = {"receipt_id": receipt["receipt_id"], "receipt_index": index,
                                         "dependencies": dependency_version(node)}
                        progress = changed = True
                        break
                if node["kind"] == "decision":
                    requested = state.get("requests", {}).get(name)
                    answers = [r for r in self._answers(node) if r.get("answer") and r.get("decision_id") == requested]
                    from .alignment_evidence import resolved_answer
                    resolution = resolved_answer(self.process.session.host_ledger, answers[-1], node["state_topic"]) if answers else None
                    if resolution:
                        records[name] = {"decision_id": answers[-1]["decision_id"], "receipt_index": len(self.process.tool_receipts) - 1,
                                         "value": resolution["value"], "disposition": resolution["disposition"],
                                         **({'choice_key': resolution['choice_key']} if resolution.get('choice_key') else {}),
                                         "dependencies": dependency_version(node)}
                        progress = changed = True
            if not progress:
                break
        if changed:
            state["revision"] += 1
            self._save(state)
        return state

    def _receipt_files(self, receipt):
        paths = set()
        for raw in receipt.get("files") or []:
            try:
                paths.add(_path(workspace(self.process), raw).relative_to(workspace(self.process)).as_posix())
            except (ValueError, TypeError, OSError):
                continue
        return paths

    def status(self):
        lock = getattr(self.process, "_context_lock", None)
        if lock is None:
            return self._status()
        with lock:
            return self._status()

    def _status(self):
        state = self._load()
        notices = [n for n in self.process.session.host_ledger if n.get("event") == "engineering_notice" and n.get("task_id") == task_id(self.process)][-16:]
        if not state:
            return {"active": False, "profiles": list(PROFILES), "notices": notices}
        state = self._refresh(state)
        notices = [n for n in self.process.session.host_ledger if n.get("event") == "engineering_notice" and n.get("task_id") == task_id(self.process)][-16:]
        records = state["records"]
        nodes = [{**n, "status": "assumed" if records.get(n['id'], {}).get('defaulted') else "complete" if n["id"] in records else "ready" if all(d in records for d in n["depends_on"]) else "blocked",
                  "evidence": records.get(n["id"])} for n in state["plan"]["nodes"]]
        return {"active": True, "task_id": state["task_id"], "revision": state["revision"],
                "plan_digest": state["plan"]["digest"], "profiles": state["plan"]["profiles"],
                "nodes": nodes, "ready": [n["id"] for n in nodes if n["status"] == "ready"],
                "question_frontier": [n["id"] for n in nodes if n["kind"] == "decision" and n["status"] == "ready"],
                "notices": notices,
                "amendment": ({"decision_id": state["amendment"]["decision_id"],
                               "plan_digest": state["amendment"]["plan"]["digest"]} if state.get("amendment") else None),
                "complete": all(not n["required"] or n["status"] == "complete" or (n['status'] == 'assumed' and self.process.task_kind in {'answer', 'plan'}) for n in nodes)}

    def request_decision(self, node_id, request):
        state, node = self._ready_node(node_id, "decision")
        if node_id in state["records"]:
            return {"status": "resolved", **state["records"][node_id]}
        if request.get("kind") == "permission":
            raise ValueError("authority requests must use request_permission, not engineering questions")
        result = self._publish_question(state, {**request, "state_topic": node["state_topic"]}, [node_id])
        self.notice("WORKFLOW_AWAITING_USER", f"步骤 {node_id} 正在等待你的选择。", node_id=node_id)
        return result

    def _publish_question(self, state, request, node_ids):
        from .decision_support import create_user_decision
        result = create_user_decision(self.process, request)
        state.setdefault("requests", {}).update({name: result["decision_id"] for name in node_ids})
        try:
            self._save(state)
        except Exception:
            self.process.pending_decision = None
            self.process.session.host_ledger.append({"event": "user_decision_cancelled", "decision_id": result["decision_id"], "reason": "alignment publication failed"})
            if self.process.task_budget:
                self.process.task_budget.resume_from_user()
            raise
        return result

    def ask_frontier(self):
        """Publish one ready facet; adopt its answer before selecting the next.

        The graph can contain several independent candidates. They are not a
        questionnaire: a reply may resolve other facets or delegate them, so
        publishing the whole frontier would force obsolete questions on users.
        """
        state = self._refresh(self._load()) if self._load() else {}
        if not state:
            raise ValueError("configure an alignment tree before asking its frontier")
        frontier = self.status()["question_frontier"][:1]
        if not frontier:
            return {"status": "no_ready_questions", **self.status()}
        questions = []
        for node in state["plan"]["nodes"]:
            if node["id"] not in frontier:
                continue
            request = copy.deepcopy(state.get('request_overrides', {}).get(node['id']) or node.get("request") or {})
            request.setdefault("kind", "clarification")
            request.setdefault("options", [])
            questions.append({**request, "node_id": node["id"], "state_topic": node["state_topic"]})
        result = self._publish_question(state, {
            "kind": "clarification", "question": questions[0]["question"], "why_user_must_decide": questions[0]["why_user_must_decide"],
            "questions": questions, "options": [], "allow_free_form": True,
        }, frontier)
        self.notice("ALIGNMENT_ROUND_AWAITING_USER", "等待当前问题的回答；采用回答后再决定是否需要下一题。", node_ids=frontier)
        return result

    def revise_decision(self, node_id, request):
        state, node = self._ready_node(node_id, "decision")
        if request.get("kind") == "permission":
            raise ValueError("authority requests must use request_permission")
        if self.process.pending_decision:
            raise ValueError("answer the pending question before revising another decision")
        previous = state["records"].get(node_id, {}).get("decision_id", "")
        if request.get("supersedes_decision_id") and request["supersedes_decision_id"] != previous:
            raise ValueError("superseded decision is not the current confirmed answer")
        # Publish the new question before invalidating evidence, so validation
        # errors leave the prior confirmed tree intact.
        state["records"].pop(node_id, None)
        result = self._publish_question(state, {**request, "state_topic": node["state_topic"],
                                               "supersedes_decision_id": previous}, [node_id])
        self._refresh(state)
        self.notice("ALIGNMENT_DECISION_REOPENED", f"问题 {node_id} 已重新打开；旧答案与依赖证据仍可审计。", node_id=node_id)
        return result

    def ask_semantic(self, request):
        """Default questions use the same DAG, without a separate skill API."""
        kind = request.get("kind", "choice")
        if kind not in {"clarification", "preference", "direction", "verification", "choice"}:
            from .decision_support import create_user_decision
            return create_user_decision(self.process, request)
        topic = str(request.get("state_topic") or "").strip()
        if not topic:
            if kind != "clarification":
                from .decision_support import create_user_decision
                return create_user_decision(self.process, request)
            topic = "clarification:" + hashlib.sha256(str(request.get("question") or "").encode()).hexdigest()[:16]
        state = self._load()
        existing = next((n for n in state.get("plan", {}).get("nodes", []) if n.get("state_topic") == topic and n["kind"] == "decision"), None)
        if existing:
            if request.get("supersedes_decision_id"):
                return self.revise_decision(existing["id"], request)
            return self.request_decision(existing["id"], request)
        name = "q_" + hashlib.sha256(topic.encode()).hexdigest()[:16]
        plan = copy.deepcopy(state.get("plan") or {"profiles": ["alignment"], "nodes": []})
        plan["profiles"] = list(dict.fromkeys([*plan["profiles"], "alignment"]))
        plan["nodes"].append({"id": name, "kind": "decision", "state_topic": topic, "before_mutation": True})
        # Validate the question before extending requirements. No pending card
        # should survive a failed graph publication.
        compiled = compile_plan(plan, workspace(self.process))
        prepared = self.configured_state(compiled)
        if kind == 'clarification':
            # A model-elicited clarification is distinct from an explicitly
            # configured workflow requirement. Preserve its conservative tier
            # and origin so a source-bound user request for exploratory output
            # can change the read-only deliverable without resolving this node.
            prepared['elicitation_tiers'] = {**prepared.get('elicitation_tiers', {}), name: 'foundational'}
        result = self._publish_question(prepared, {**request, "state_topic": topic}, [name])
        return result

    def resolve_answer(self, node_id, decision_id, content):
        state = self._load()
        node = next((n for n in state.get('plan', {}).get('nodes', []) if n['id'] == node_id and n['kind'] == 'decision'), None)
        if node is None or (state.get('requests', {}).get(node_id) not in {None, decision_id}):
            raise ValueError('interpretation must bind the current question of this task')
        answer = next((a for a in reversed(self._answers(node)) if a.get('decision_id') == decision_id), None)
        if answer is None and not state.get('requests', {}).get(node_id):
            from .alignment_evidence import source_answer, answer_digest
            answer = source_answer(self.process.session.host_ledger, task_id(self.process), decision_id, content.get('source_node_id', ''))
            if answer is None and not content.get('source_node_id') and content.get('quote'):
                receipt = next((e for e in reversed(self.process.session.host_ledger)
                    if e.get('event') == 'user_decision_received' and e.get('task_id') == task_id(self.process)
                    and e.get('decision_id') == decision_id), {})
                sources = [(key, item) for key, item in (receipt.get('answers') or {}).items()
                           if content['quote'] in str(item.get('answer', ''))]
                # Derive omitted plumbing from immutable evidence, not invented
                # facts. Identical copies of one reply in a round are equivalent;
                # genuinely different sources still require explicit selection.
                if sources and len({answer_digest(item['answer']) for _, item in sources}) == 1:
                    key, item = sources[0]
                    content = {**content, 'source_node_id': key}
                    answer = {**receipt, **item}
        if not answer or not answer.get('answer'):
            raise ValueError('no actual user receipt for this question')
        if answer.get('answers') and node_id in answer['answers'] and not content.get('source_node_id'):
            content = {**content, 'source_node_id': node_id}
        from .alignment_evidence import interpret_answer, resolved_answer
        event = interpret_answer(self.process, node, answer, content, publish=False)
        previous = resolved_answer(self.process.session.host_ledger, answer, node['state_topic'])
        if previous and content.get('disposition') == previous.get('disposition'):
            # Delivery review must not churn a source-bound value just because
            # the same model paraphrases it differently on the next call.
            return previous
        conditional = any(node_id in (n.get('when') or {}) for n in state['plan']['nodes'])
        if conditional and event['disposition'] in {'confirmed', 'delegated'} and not event.get('choice_key'):
            # A semantic paraphrase is not a branch key. Rich free-form answers
            # require explicit classification; exact user values remain literal.
            literal = answer.get('selected_label') or str(answer['answer']).strip()
            labels = {v for n in state['plan']['nodes'] for v in (n.get('when') or {}).get(node_id, [])}
            labels.update(o['label'] for o in (node.get('request') or {}).get('options', []) if o.get('label'))
            if not answer.get('selected_label') and literal not in labels:
                raise ValueError('conditional decision requires choice_key separate from its semantic value')
            event['choice_key'] = literal
        state.setdefault('requests', {})[node_id] = decision_id
        self.process.session.host_ledger.append(event)
        self._save(state)
        self._refresh(state)
        return event

    def _ready_node(self, node_id, kind=None):
        state = self._load()
        if not state:
            raise ValueError("no active engineering workflow")
        state = self._refresh(state)
        node = next((n for n in state["plan"]["nodes"] if n["id"] == node_id), None)
        if node is None or (kind and node["kind"] != kind):
            raise ValueError("unknown node or incompatible evidence kind")
        if any(d not in state["records"] for d in node["depends_on"]):
            raise ValueError("node prerequisites are unresolved; inspect the ready frontier")
        return state, node

    def propose_amendment(self, raw, reason):
        state = self._load()
        if not state or not str(reason or "").strip():
            raise ValueError("scope amendment requires an active workflow and a user-visible reason")
        plan = compile_plan(raw, workspace(self.process))
        old = {n["id"]: n for n in state["plan"]["nodes"]}
        new = {n["id"]: n for n in plan["nodes"]}
        removed = sorted(set(old) - set(new))
        modified = sorted(k for k in set(old) & set(new) if old[k] != new[k])
        added = sorted(set(new) - set(old))
        changes = {"profiles": {"before": state["plan"]["profiles"], "after": plan["profiles"]},
                   "preparation_files": {"before": state["plan"].get("preparation_files", []), "after": plan.get("preparation_files", [])},
                   "nodes": [{"id": k, "before": old.get(k), "after": new.get(k)} for k in sorted(set(removed + modified + added))]}
        from .decision_support import create_user_decision
        result = create_user_decision(self.process, {
            "kind": "direction", "state_topic": "engineering_scope",
            "question": str(reason) + "\n具体变更（before 为原要求，after 为新要求）：\n" + json.dumps(changes, ensure_ascii=False, indent=2) + "\n是否采用此调整？",
            "why_user_must_decide": "Existing engineering requirements would change; missing evidence cannot be silently waived.",
            "allow_free_form": True,
            "options": [
                {"label": "采用调整", "principle": "明确修改工程要求，保留原始事实。", "immediate_effect": "采用当前展示的步骤调整。",
                 "downstream_effect": "Host 按新的依赖与证据检查；旧证据不会被标为通过。", "risks": "移除的检查不再保证交付质量。",
                 "reversibility": "可以明确申请再次调整。", "action": "accept_engineering_amendment"},
                {"label": "保留原要求", "principle": "继续满足原来的工程条件。", "immediate_effect": "不修改当前工作流。",
                 "downstream_effect": "仍需补齐现有缺失证据。", "risks": "可能需要额外时间或当前环境无法提供的验证。",
                 "reversibility": "稍后仍可提出新调整。", "action": "keep_engineering_workflow", "recommended": True},
            ],
        })
        state["amendment"] = {"plan": plan, "decision_id": result["decision_id"]}
        self._save(state)
        self.notice("WORKFLOW_SCOPE_DECISION_REQUIRED", "工程范围调整正在等待你的决定；原要求继续有效。")
        return result

    def record(self, node_id, content=None):
        state, node = self._ready_node(node_id)
        kind = node["kind"]
        if kind not in {"document", "report"}:
            raise ValueError("receipts and user answers are observed by Host, never asserted by model")
        if kind == "document":
            path = _path(workspace(self.process), node["path"])
            raw = path.read_bytes()
            if not raw.strip() or len(raw) > 1_000_000:
                raise ValueError("document evidence must be nonempty and at most 1MB")
            text = raw.decode("utf-8-sig")
            if "agent_documentation" in state["plan"]["profiles"]:
                _validate_local_links(path, text, workspace(self.process))
            record = {"path": node["path"], "digest": hashlib.sha256(raw).hexdigest()}
        else:
            content = self.retrospective() if node["format"] == "retrospective" and content is None else content
            _validate_report(node["format"], content)
            from .context_store import ContextObjectStore
            ref = ContextObjectStore(workspace(self.process)).put(
                f"engineering/{self.process.process_id}/{node_id}", content,
                metadata={"authority": "llm_proposed_host_validated", "producer": KEY,
                          "task_id": task_id(self.process)})
            record = {"ref": ref["pinned"], "digest": ref["digest"]}
        record["receipt_index"] = len(self.process.tool_receipts) - 1
        record["dependencies"] = {d: hashlib.sha256(json.dumps(state["records"][d], sort_keys=True).encode()).hexdigest()
                                  for d in node["depends_on"]}
        state["records"][node_id] = record
        state["revision"] += 1
        self._save(state)
        self.notice("WORKFLOW_EVIDENCE_RECORDED", f"步骤 {node_id} 的证据已保存。", node_id=node_id)
        return self.status()

    def retrospective(self):
        findings = []
        failures = {}
        for _, row in self._receipts():
            if row.get("succeeded") is False:
                key = (row.get("tool_name"), row.get("error_code", "TOOL_FAILED"))
                failures.setdefault(key, []).append(row.get("receipt_id"))
        for (tool, code), ids in failures.items():
            findings.append({"category": "feedback_loop", "tool": tool, "code": code,
                             "receipt_ids": ids, "recommendation": "inspect the failed operation and improve its feedback before repeating it"})
        return {"findings": findings, "authority": "host_observed",
                "scope": "current_task_receipts", "limitations": ["No finding does not prove absence of architectural or semantic problems."]}

    def guard(self, tool_name, effect, arguments=None):
        # Registered checks must be able to collect red evidence. Control-plane
        # operations do not mutate product files and cannot bypass actual tools.
        if tool_name in {"run_test", "engineering_workflow", "request_user_decision", "request_permission", "declare_task_contract"} or not is_effectful_mutation(effect):
            return {"allowed": True}
        status = self.status()
        if not status["active"]:
            return {"allowed": True}
        nodes = status.get("nodes", [])
        if tool_name == "exec_command" and any(n["kind"] == "check" and n["status"] == "ready" and n.get("tool_name") == "exec_command" and command_digest(n) == command_digest(arguments or {}) for n in nodes):
            return {"allowed": True}
        if effect == "workspace_write" and tool_name in {"write_file", "edit_file", "document_create"} and (arguments or {}).get("path"):
            target = _path(workspace(self.process), arguments["path"]).relative_to(workspace(self.process)).as_posix()
            if target in self._load().get("plan", {}).get("preparation_files", []):
                return {"allowed": True}
        # A later vertical slice does not stop the current slice. Its red check
        # becomes a gate only when the previous slice has finished.
        blockers = [n["id"] for n in nodes if n["before_mutation"] and n["status"] != "complete"
                    and ("tdd" not in status.get("profiles", []) or n["status"] == "ready")]
        if "tdd" in status.get("profiles", []) and not any(n["kind"] == "change" and n["status"] in {"ready", "complete"} and all(
            next(d for d in nodes if d["id"] == dep)["status"] == "complete" for dep in n["depends_on"]) for n in nodes):
            blockers.extend(n["id"] for n in nodes if n["kind"] == "check" and n.get("passed") is True and n["status"] != "complete")
        if blockers:
            self.notice("WORKFLOW_PREREQUISITE_BLOCKED", "工程前置证据尚未齐全，操作未执行：" + ", ".join(blockers))
            return {"allowed": False, "code": "ENGINEERING_PREREQUISITE_REQUIRED",
                    "reason": "Engineering prerequisites are unresolved: " + ", ".join(blockers),
                    "next_actions": [{"action": "engineering_workflow", "operation": "status"}]}
        return {"allowed": True}

    def completion_reasons(self):
        status = self.status()
        from .alignment_evidence import exploratory_scope
        exploration = exploratory_scope(self.process)
        elicited = self._load().get('elicitation_tiers', {})
        reasons = [f"engineering workflow {n['id']} ({n['kind']}) requires current evidence"
                   for n in status.get("nodes", []) if n["required"] and n["status"] != "complete"
                   and not (n['status'] == 'assumed' and self.process.task_kind in {'answer', 'plan'})
                   and not (exploration and n['kind'] == 'decision' and n['id'] in elicited)]
        if status.get("amendment"):
            reasons.append("engineering workflow scope amendment (decision) requires an explicit user choice")
        return reasons

    def invoke(self, args):
        try:
            with self.process._context_lock:
                operation = args.get("operation", "status")
                if operation == "configure":
                    return self.configure(args.get("plan"))
                if operation == "record":
                    return self.record(args.get("node_id"), args.get("content"))
                if operation == "ask":
                    return self.request_decision(args.get("node_id"), args.get("request") or {})
                if operation == "ask_frontier":
                    return self.ask_frontier()
                if operation == "ask_semantic":
                    return self.ask_semantic(args.get("request") or {})
                if operation == "revise_decision":
                    return self.revise_decision(args.get("node_id"), args.get("request") or {})
                if operation == "resolve_answer":
                    return self.resolve_answer(args.get("node_id"), args.get("decision_id"), args.get("content"))
                if operation == "propose_amendment":
                    return self.propose_amendment(args.get("plan"), args.get("reason"))
                if operation == "retrospective":
                    return self.retrospective()
                if operation == "status":
                    return self.status()
                raise ValueError("unknown engineering workflow operation")
        except (ValueError, TypeError, KeyError, OSError, UnicodeError, RuntimeError) as exc:
            notice = self.notice("WORKFLOW_RECOVERY_REQUIRED", f"工程工作流未完成操作：{exc}。已保留原有要求和证据。")
            from backend.core.errors import error_payload
            return error_payload("ENGINEERING_WORKFLOW_RECOVERY_REQUIRED", message=notice["message"],
                                 next_actions=[{"action": "engineering_workflow", "operation": "status"},
                                               {"action": "request_user_decision", "when": "a material scope choice remains"}])


def _validate_report(format, content):
    if not isinstance(content, dict) or len(json.dumps(content).encode()) > 64_000:
        raise ValueError("report must be a bounded structured object")
    missing = [name for name in REPORT_FIELDS[format] if name not in content]
    if missing:
        raise ValueError("report fields missing: " + ", ".join(missing))
    if format == "hypotheses":
        hypotheses = content["hypotheses"]
        if not isinstance(hypotheses, list) or not 2 <= len(hypotheses) <= 5 or any(not isinstance(h, dict) or not h.get("prediction") or not h.get("probe") for h in hypotheses):
            raise ValueError("hypotheses require 2..5 alternatives with falsifiable predictions and probes")
    if format == "architecture":
        candidates = content["candidates"]
        if not isinstance(candidates, list) or not candidates or any(not isinstance(c, dict) or not c.get("files") or not c.get("problem") or not c.get("test_surface") or not isinstance(c.get("alternatives"), list) or len(c["alternatives"]) < 2 for c in candidates):
            raise ValueError("architecture candidates require files, problem, test_surface and at least two alternatives")
    if format == "alignment" and (not isinstance(content["decisions"], list) or not isinstance(content["unresolved"], list) or content["unresolved"]):
        raise ValueError("alignment report still has unresolved choices; use decision nodes")
    if format == "agent_documentation" and (not isinstance(content["references"], list) or any(not isinstance(content[field], list) or not content[field] for field in ("triggers", "completion_criteria"))):
        raise ValueError("agent documentation requires triggers, completion criteria and reference declarations")


def _validate_local_links(path, text, root):
    # Examples in fenced code are not live document references.
    text = re.sub(r"(?ms)^\s*(```|~~~)[^\n]*\n.*?^\s*\1[^\n]*(?:\n|$)", "", text)
    for raw in re.findall(r"\]\(([^)]+)\)", text):
        raw = raw.strip()
        target = raw[1:].split(">", 1)[0] if raw.startswith("<") else re.sub(r'''\s+["'].*["']$''', "", raw)
        target = target.split("#", 1)[0]
        if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]+:", target):
            continue
        resolved = (path.parent / unquote(target)).resolve()
        resolved.relative_to(root)
        if not resolved.exists():
            raise ValueError(f"agent documentation contains a broken local reference: {target}")


def command_digest(arguments):
    return hashlib.sha256(json.dumps({"argv": arguments.get("argv"), "cwd": arguments.get("cwd") or "."}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
