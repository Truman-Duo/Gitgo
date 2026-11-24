"""On-demand evidence for the main Agent's qualitative delivery comparison.

This service quotes real source words and binds that excerpt, not semantic
equivalence. Optional answer_updates reuses the main Agent's existing source-bound
interpreter. No auxiliary model or notebook is needed; other prose is not certified.
"""
import hashlib
import json
import re

from backend.core.errors import error_payload
from backend.core.loop.alignment_evidence import alignment_input_digest, answer_digest
from backend.core.loop.dialogue_context import dialogue_view


def _basis(process, *, legacy=False):
    from backend.core.loop.alignment_evidence import CONTROL_DECISION_KINDS
    task = process.active_task_id or process.process_id
    ledger = process.session.host_ledger
    if legacy:
        # Read the first local binding format without invalidating preserved
        # task state merely because version computation was made cheaper.
        from backend.core.loop.alignment_evidence import business_answer_receipts
        from backend.core.loop.decision_support import confirmed_user_state
        records = {"confirmed": confirmed_user_state(ledger, task_id=task),
                   "received": business_answer_receipts(ledger, task)}
        return hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    controls = {e.get("decision_id") for e in ledger
                if e.get("task_id") == task and e.get("event") == "user_decision_requested"
                and e.get("kind") in CONTROL_DECISION_KINDS}
    # Track evidence versions without rebuilding every interpreted decision at
    # each delivery check. No cached result or changed authority is introduced.
    records = [e for e in ledger if e.get("task_id") == task
               and e.get("event") in {"user_decision_requested", "user_decision_received",
                                      "user_decision_interpreted", "user_decision_cancelled"}
               and e.get("decision_id") not in controls]
    return hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _heading(candidate, scope):
    if scope is not None:
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 120 or "\n" in scope or "\r" in scope:
            raise ValueError("scope must be a single short section heading")
        return "## " + scope.strip()
    headings = [line for line in candidate.splitlines() if re.fullmatch(r"#{1,6} [^\r\n]+", line)]
    if headings:
        return next((line for line in headings if line.startswith("## ")), headings[0])
    # Presentation fallback for the supported Chinese/English draft surfaces;
    # an explicit scope or existing heading always owns its exact wording.
    return "## 已明确的用户原话" if re.search(r"[\u4e00-\u9fff]", candidate) else "## Confirmed user statements"


def _section(candidate, heading):
    lines = candidate.replace("\r\n", "\n").splitlines()
    starts = [i for i, line in enumerate(lines) if line == heading]
    if len(starts) != 1:
        return None
    start = starts[0]
    level = len(heading.split(" ", 1)[0])
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^#{1," + str(level) + r"} ", lines[i])), len(lines))
    return "\n".join(lines[start:end]).strip()


def _status_version(process):
    context, _ = process.read_context_snapshot()
    graph = context.get('engineering_workflow') or {}
    scope = (context.get('alignment_runtime') or {}).get('scope') or {}
    return {'plan': (graph.get('plan') or {}).get('digest'), 'revision': graph.get('revision'),
            'scope': hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()}


def _distinct_source_quotes(quotes, decisions, partials):
    """Remove literal nested excerpts of the same receipt, not semantic repeats.

    No source/state is edited. Different receipts or interpretation categories
    remain distinct; oversized input keeps the original bounded failure path.
    """
    if sum(len(q.encode('utf-8')) for q in quotes) > 16000:
        return quotes
    origins = {}
    for item in [*decisions, *partials]:
        if item.get('disposition') not in {'confirmed', 'partial'}:
            continue
        quote = item.get('source_quote') or item.get('selected_label') or item.get('quote')
        if quote and item.get('decision_id'):
            origins.setdefault(quote, set()).add((item['decision_id'], item.get('source_node_id', ''),
                                                  item['disposition']))
    return [quote for quote in quotes if not any(
        len(other) > len(quote) and quote in other and origins.get(quote)
        and origins[quote].issubset(origins.get(other, set())) for other in quotes)]


def _decision_status(process, quotes, decisions, language_text):
    """Render existing decision evidence, never invent choices or their priority."""
    context, _ = process.read_context_snapshot()
    graph = context.get('engineering_workflow') or {}
    from backend.core.loop.dialogue_context import unresolved_interpretations
    partials = unresolved_interpretations(process, graph)
    chinese = bool(re.search(r'[\u4e00-\u9fff]', language_text))
    known, delegated, opened, body = (('## 已定', '## 委托范围', '## 未定', '## 比较与说明') if chinese
        else ('## Confirmed', '## Delegated scope', '## Open decisions', '## Discussion'))
    sections = [known + '\n\n' + ('\n\n'.join('\n'.join('> ' + line for line in q.splitlines())
        for q in quotes) or ('本轮尚无已确认决定。' if chinese else 'No confirmed decision in this scope.'))]
    nodes = graph.get('plan', {}).get('nodes', [])
    by_topic = {n.get('state_topic'): n for n in nodes if n['kind'] == 'decision'}

    def question(node):
        request = graph.get('request_overrides', {}).get(node['id']) or node.get('request') or {}
        return str(request.get('question') or node['state_topic'])

    def bullet(text):
        return '- ' + str(text).replace('\r\n', '\n').replace('\n', '\n  ')

    grants = []
    for d in decisions:
        quote = d.get('source_quote') or d.get('selected_label')
        if d.get('disposition') == 'delegated' and quote:
            topic = d.get('topic') or d.get('state_topic')
            node = by_topic.get(topic)
            request = (graph.get('request_overrides', {}).get(node['id']) or node.get('request') or {}) if node else {}
            label = (bullet(request['question']) + '\n') if request.get('question') else ''
            grants.append(label + '\n'.join(('  > ' if label else '> ') + line for line in quote.splitlines()))
    if grants:
        sections.append(delegated + '\n\n' + '\n\n'.join(grants))
    pending, blocked = [], False
    records = graph.get('records', {})
    for node in nodes:
        if node['kind'] != 'decision':
            continue
        if node['id'] in records:
            if records[node['id']].get('elicitation_retirement'):
                note = '本轮不依赖，仍未确定' if chinese else 'not needed for this response; still unknown'
                pending.append(bullet(question(node) + ' — ' + note))
            continue
        if not all(dep in records for dep in node['depends_on']):
            blocked = True
            continue  # Do not expose dependent interview branches prematurely.
        pending.append(bullet(partials.get(node['id'], {}).get('remaining') or question(node)))
    if blocked:
        pending.append('后续事项依赖上述决定，暂未展开。' if chinese
                       else 'Dependent choices remain in the workflow until their prerequisites are resolved.')
    sections.append(opened + '\n\n' + ('\n'.join(pending) or
        ('当前已登记问题中没有未定项。' if chinese else 'No open item among the recorded questions.')))
    excerpt = '\n\n'.join(sections)
    if len(excerpt.encode('utf-8')) > 16000:
        raise ValueError('The complete decision status exceeds the bounded summary. Read scoped decision details; no partial status was bound.')
    return {'heading': known, 'excerpt': excerpt, 'format': 'decision_status',
            'body_heading': body,
            **({'discussion_digest': context['alignment_runtime']['scope']['discussion_digest']}
               if (context.get('alignment_runtime') or {}).get('scope', {}).get('discussion_digest') else {}),
            'include_discussion': bool((context.get('alignment_runtime') or {}).get('scope', {}).get('include_discussion', True)),
            'status_version': _status_version(process)}


def _status_matches(candidate, binding):
    excerpt = binding['excerpt']
    if binding.get('discussion_digest'):
        prefix = excerpt + '\n\n' + binding['body_heading'] + '\n\n'
        return (candidate.startswith(prefix)
                and answer_digest(candidate[len(prefix):].strip()) == binding['discussion_digest'])
    return candidate == excerpt or (binding.get('include_discussion', True)
        and candidate.startswith(excerpt + '\n\n' + binding['body_heading'] + '\n\n'))


def inspect_delivery(process, candidate, *, scope=None, answer_updates=None):
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 64000:
        return error_payload("WORKBENCH_EXPRESSION_CANDIDATE_INVALID")
    if process is None or getattr(process, "session", None) is None:
        return error_payload("WORKBENCH_EXPRESSION_SCOPE_UNAVAILABLE")
    task = process.active_task_id or process.process_id
    context, version = process.read_context_snapshot()
    state = context.get("alignment_runtime") or {}
    if state and (state.get("task_id") != task or state.get("process_id") != process.process_id):
        return error_payload("WORKBENCH_EXPRESSION_SCOPE_UNAVAILABLE",
                             message="The dialogue state belongs to a different task/process; do not adopt its evidence.")
    try:
        heading = _heading(candidate, scope)
    except ValueError as exc:
        return error_payload("WORKBENCH_INVALID_INPUT", message=str(exc))
    if answer_updates:
        from backend.core.loop.main_dialogue import apply_answer_updates, update_error, DialogueUpdateError
        try:
            apply_answer_updates(process, answer_updates)
        except DialogueUpdateError as exc:
            return update_error(exc)
        context, version = process.read_context_snapshot()
        state = context.get("alignment_runtime") or {}
    input_digest = alignment_input_digest(process)
    # Reuse current evidence/interpretation filtering and whole-item bounds.
    # Prior tasks may inform a conversation, but are not current user decisions.
    view = dialogue_view(process, include_interpretations=True) if state else None
    if view is not None:
        view.pop("prior_business_reference", None)
        view.pop("prior_relevance", None)
    if process.read_context_snapshot()[1] != version or alignment_input_digest(process) != input_digest:
        return error_payload("WORKBENCH_EXPRESSION_SOURCES_CHANGED")
    request = str(state.get("request") or process.task_description or "")
    request_visible = len(request.encode("utf-8")) <= 4000
    result = {
        "status": "evidence_ready_for_agent_comparison" if view else "no_current_dialogue_sources",
        "semantic_verdict": "not_assessed", "authority_granted": False,
        "task_id": task, "process_id": process.process_id,
        "candidate_digest": answer_digest(candidate), "candidate_chars": len(candidate),
        "input_digest": input_digest,
        "task_start_request": request if request_visible else None,
        "task_start_request_omitted": not request_visible,
        "dialogue_evidence": view,
        "comparison": {
            "evidence": "Compare the draft with exact source_quote/selected_label and received input. interpretation_proposals are unverified Agent expansions, not confirmed requirements.",
            "meaning": "Preserve the evidenced actor, action, conditions and scope. An effect does not establish an unstated later state or required user action.",
            "ownership": "Keep assistant assumptions/proposals and unresolved choices separate from user-confirmed rules, even when a default seems sensible.",
            "next_step": "Correct unsupported wording or clearly qualify a proposal in the final response. Use answer_updates only if the stored interpretation itself needs repair. Honor a request to stop discussion; inspection is not a reason to ask again.",
        },
        "coverage": "Current dialogue evidence for the main Agent to compare, not a semantic pass or a check of all factual claims. Read retained details for omitted items.",
    }
    approvals = []
    for receipt in reversed(process.tool_receipts):
        facts = receipt.get("approval_before_policy_denial")
        if facts and facts["task_id"] == task:
            approvals.append({**facts, "tool_name": receipt["tool_name"], "receipt_id": receipt["receipt_id"]})
            if len(approvals) == 8:
                break
    if approvals:
        result["approval_before_policy_denial"] = approvals
    # This is a source excerpt, not generated semantic equivalence. Keep the
    # user's actual words rather than promoting the Agent's paraphrase to fact.
    decisions = (view or {}).get("current_decisions")
    if state and (decisions is None or (view or {}).get("omitted_reference_items")):
        from backend.core.loop.decision_support import confirmed_user_state
        decisions = confirmed_user_state(process.session.host_ledger, task_id=task)
    decisions = decisions or []
    quotes = list(dict.fromkeys(
        d.get("source_quote") or d.get("selected_label") for d in decisions
        if d.get("disposition") == "confirmed" and (d.get("source_quote") or d.get("selected_label"))))
    partials = [n["interpretation"] for n in (view or {}).get("unresolved_decisions", [])
                if n.get("interpretation")]
    if (view or {}).get("omitted_reference_items"):
        from backend.core.loop.dialogue_context import unresolved_interpretations
        partials = list(unresolved_interpretations(process).values())
    quotes = list(dict.fromkeys([*quotes, *(i["quote"] for i in partials
        if i.get("disposition") == "partial" and str(i.get("value") or "").strip()
        and str(i.get("quote") or "").strip())]))
    structured = (state.get('scope') or {}).get('presentation') == 'decision_status'
    if structured:
        quotes = _distinct_source_quotes(quotes, decisions, partials)
    if quotes or structured:
        status_binding = None
        if structured:
            try:
                status_binding = _decision_status(process, quotes, decisions, request + candidate)
            except ValueError as exc:
                return error_payload('WORKBENCH_EXPRESSION_STATUS_TOO_LARGE', message=str(exc))
            heading, excerpt = status_binding['heading'], status_binding['excerpt']
        if sum(len(q.encode("utf-8")) for q in quotes) > 16000:
            return {**result, "status": "source_details_required",
                    "next_step": "The full confirmed excerpt exceeds this operation's bound. Read retained details; no partial replacement was bound or certified."}
        if not structured:
            excerpt = heading + "\n\n" + "\n\n".join(
                "\n".join("> " + line for line in quote.splitlines()) for quote in quotes)
        if len(excerpt.encode("utf-8")) > 16000:
            return {**result, "status": "source_details_required",
                    "next_step": "The complete confirmed excerpt exceeds this bounded operation. Read the retained decision details and keep source quotations separate from your interpretation; no excerpt was bound or certified."}
        binding = {"heading": heading, "excerpt": excerpt, "basis": _basis(process), "basis_version": 2,
                   **(status_binding or {})}
        from backend.core.loop.mechanism_lifecycle import fence
        from backend.core.loop.models import ProcessStatus
        from .delivery import CONTEXT_KEY
        with fence(process):
            if process.status is not ProcessStatus.RUNNING:
                return error_payload("WORKBENCH_EXPRESSION_SCOPE_UNAVAILABLE",
                                     message="Only the running task can bind its intended public excerpt.")
            if process.read_context_snapshot()[1] != version:
                return error_payload("WORKBENCH_EXPRESSION_SOURCES_CHANGED")
            def bind(current):
                old = current.get(CONTEXT_KEY) or {}
                same = old.get("task_id") == task and old.get("process_id") == process.process_id
                retained = old if same else {"task_id": task, "process_id": process.process_id,
                    "workspace": str(process.worktree_path or process.workspace_root or ""), "claims": []}
                return {**current, CONTEXT_KEY: {**retained, "dialogue_excerpt": binding}}
            process.update_context_snapshot(bind)
        result.update(confirmed_excerpt=excerpt, excerpt_heading=heading,
                      excerpt_status="match" if _section(candidate, heading) == excerpt else "replace_confirmed_section",
                      next_step="Runtime assembles confirmed_excerpt at delivery; no manual heading or quote copying is required. Compare meaning and keep interpretations, defaults and proposals separate. This binds source words and versions, not the truth of other prose.")
    return result


def assess_excerpt(process, candidate, binding):
    check = {"scope": binding["heading"].lstrip("# "), "kind": "dialogue_source_excerpt",
             "candidate_digest": answer_digest(candidate), "blocking": True,
             "semantic_verdict": "not_assessed"}
    structured = binding.get('format') == 'decision_status'
    if (_basis(process, legacy=binding.get("basis_version", 1) == 1) != binding["basis"]
            or structured and _status_version(process) != binding['status_version']):
        return {**check, "status": "stale", "diagnostic_code": "WORKBENCH_EXPRESSION_EVIDENCE_STALE",
                "reason": "Dialogue facts changed; inspect_delivery again after applying current answer updates."}
    if not (_status_matches(candidate, binding) if structured else _section(candidate, binding["heading"]) == binding["excerpt"]):
        if binding.get('discussion_digest') and candidate.startswith(binding['excerpt']):
            return {**check, 'status': 'contradiction', 'diagnostic_code': 'WORKBENCH_DISCUSSION_CHANGED',
                    'reason': 'The requested discussion differs from its bound response component. '
                    'Retain that exact body, or explicitly update scope_update.discussion with its current user-source quote.'}
        return {**check, "status": "contradiction", "diagnostic_code": "WORKBENCH_EXPRESSION_EXCERPT_CHANGED",
                "reason": "The confirmed section differs from the source excerpt. Copy the source wording unchanged; put proposals and inferences in another section."
                + ("\nRequired confirmed section:\n" + binding["excerpt"] if len(binding["excerpt"]) <= 4000
                   else " Use inspect_delivery to read the complete confirmed_excerpt; no partial quotation is a substitute.")}
    return {**check, "status": "match", "blocking": False,
            "reason": "The confirmed section retains the pinned user wording; other prose is not semantically certified."}


def prepare_task_delivery(process, candidate):
    """Assemble evidence at an already source-bound read-only summary boundary.

    The Agent chose completion and exploration through the existing protocol;
    Runtime supplies this mechanical source binding. No semantic classifier,
    extra model, permission, or mandatory notebook is introduced.
    """
    from backend.core.loop.main_dialogue import owns_dialogue
    from backend.core.loop.alignment_evidence import exploratory_scope
    from .delivery import CONTEXT_KEY
    context, _ = process.read_context_snapshot()
    state = context.get("alignment_runtime") or {}
    task = process.active_task_id or process.process_id
    previous = context.get(CONTEXT_KEY) or {}
    if (previous.get("task_id") == task and previous.get("process_id") == process.process_id
            and previous.get("dialogue_excerpt")):
        binding = previous["dialogue_excerpt"]
        current_format = (state.get('scope') or {}).get('presentation') == 'decision_status'
        if (binding.get('format') == 'decision_status') != current_format:
            return inspect_delivery(process, candidate)
        if assess_excerpt(process, candidate, binding)["status"] == "stale":
            return inspect_delivery(process, candidate, scope=binding["heading"].lstrip("# "))
        return None
    if not owns_dialogue(state) or not exploratory_scope(process):
        return None
    return inspect_delivery(process, candidate)


def compose_task_delivery(process, candidate):
    """Fill the declared confirmed-source section before existing review gates.

    This is construction from real evidence, not semantic editing of other
    prose. An unstructured draft still uses the existing bounded repair path.
    """
    context, _ = process.read_context_snapshot()
    state = context.get("workbench_delivery") or {}
    if (state.get("task_id") != (process.active_task_id or process.process_id)
            or state.get("process_id") != process.process_id):
        return candidate
    binding = state.get("dialogue_excerpt")
    if not binding or assess_excerpt(process, candidate, binding)["status"] == "stale":
        return candidate
    if binding.get('format') == 'decision_status':
        if not binding.get('include_discussion', True):
            # The Agent explicitly chose a record-only public response. Its
            # original tool arguments remain in Trace; no prose classifier or
            # keyword-based deletion chooses which draft statements to keep.
            return binding['excerpt']
        if _status_matches(candidate, binding):
            return candidate
        if binding.get('discussion_digest') and candidate.startswith(binding['excerpt'] + '\n\n'):
            return candidate  # Keep a changed structured draft for its discussion-specific diagnostic.
        # The Agent owns this discussion body. Do not discard any of it by
        # guessing which heading describes confirmed/unresolved decisions.
        return binding['excerpt'] + '\n\n' + binding['body_heading'] + '\n\n' + candidate.strip()
    lines = candidate.replace("\r\n", "\n").splitlines()
    starts = [i for i, line in enumerate(lines) if line == binding["heading"]]
    if len(starts) > 1:
        return candidate  # Never pick one of ambiguous duplicated sections.
    if not starts:
        starts = [i for i, line in enumerate(lines) if line.startswith("## ")][:1]
    if not starts:
        # Source text is a product-owned section, not a Markdown exercise for
        # the Agent. Separate an unstructured draft with an explicit boundary
        # so assess_excerpt does not absorb the remaining response as source.
        heading = binding['heading']
        level = heading.split(' ', 1)[0]
        label = '正文' if re.search(r'[\u4e00-\u9fff]', heading) else 'Response'
        return binding['excerpt'] + '\n\n' + level + ' ' + label + '\n\n' + candidate.strip()
    start = starts[0]
    level = len(lines[start].split(" ", 1)[0])
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^#{1," + str(level) + r"} ", lines[i])), len(lines))
    return "\n".join([*lines[:start], binding["excerpt"], "", *lines[end:]]).strip()
