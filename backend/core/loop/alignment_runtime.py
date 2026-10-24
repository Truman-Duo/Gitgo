"""Bounded semantic admission and delivery checks over the existing Host DAG.

The semantic model proposes questions/interpretations; it never owns receipts,
dependency readiness, permissions, or completion. No domain keyword routing.
"""
from __future__ import annotations

from backend.core.prompt_templates import load_prompt

import copy
import hashlib
import json

KEY = 'alignment_runtime'


def optional_object(value, name):
    """Normalize empty nullable form objects, never weaken substantive commands."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an object or null')
    if all(v is None or (isinstance(v, str) and not v.strip()) for v in value.values()):
        return None
    return value


POLICY = load_prompt('alignment/assessor')


def assess_with_provider(provider, payload, *, before_attempt=None, on_notice=None, **kwargs):
    """Common provider facade; isolated messages never borrow conversation state."""
    wire_payload = payload
    if payload.get('stage') == 'delivery' and payload.get('candidate'):
        from .candidate_evidence import candidate_segments
        wire_payload = {**payload, 'candidate': {'segments': candidate_segments(payload['candidate'])}}
    messages = [{'role': 'system', 'content': POLICY},
                {'role': 'user', 'content': json.dumps(wire_payload, ensure_ascii=False)}]
    routed = hasattr(provider, 'stream_session_events')
    if before_attempt and not routed:
        kwargs['timeout'] = before_attempt(0, provider)
    # Reasoning-capable providers count hidden reasoning against this limit.
    # Use their configured ceiling, bounded independently of the task tree.
    from .provider_protocol import ProviderResponseIncomplete
    if getattr(provider, 'supports_generation_purpose', False):
        kwargs['purpose'] = 'alignment'
        if not routed:
            kwargs['on_notice'] = on_notice
            if before_attempt:
                kwargs['before_generation_fallback'] = lambda: before_attempt(1, provider)
    ceiling = 16384 if payload.get('actual_answers') or payload.get('stage') == 'delivery' or payload.get('provider_output_recovery') else 8192
    try:
        response = provider.chat(messages, max_tokens=min(ceiling, max(1, int(getattr(provider, 'max_output_tokens', 4096)))), max_retries=0,
                             preserve_provider_state=True,
                             **({'before_attempt': before_attempt, 'on_notice': on_notice} if routed else {}), **kwargs)
    except ProviderResponseIncomplete as exc:
        # Keep billing even when hidden reasoning consumed the entire response.
        # The existing two-attempt compiler recovery remains the only retry.
        return {'schema_error': 'provider_response_incomplete', 'incomplete_reason': exc.reason,
                '_provider_usage': exc.usage}
    raw = response.get('content', '') if isinstance(response, dict) else response
    text = str(raw).strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Keep billing and bounded diagnostic evidence for compiler recovery.
        result = {'schema_error': 'invalid_json', 'unparsed_output': text[:8000]}
    if not isinstance(result, dict):
        result = {'schema_error': 'object_required'}
    if not isinstance(result.get('request_frame'), dict):
        result = {**result, 'status': 'invalid_request_frame', 'schema_error': 'request_frame_required: classify the current requested response, not a hypothetical future task'}
    if payload.get('stage') == 'delivery' and not isinstance(result.get('delivery_check'), dict):
        result = {**result, 'status': 'invalid_delivery_check', 'schema_error': 'delivery_check_required'}
    if isinstance(response, dict) and response.get('usage'):
        result['_provider_usage'] = response['usage']
    return result


class AlignmentRuntime:
    def __init__(self, process, emit=None):
        from .engineering_workflow import EngineeringWorkflow
        self.process = process
        self.workflow = EngineeringWorkflow(process, emit)
        self.emit = emit

    def payload(self, instruction='', candidate='', proposed_questions=None, proposed_contracts=None, proposed_files=None):
        context, _ = self.process.read_context_snapshot()
        state = copy.deepcopy(context.get(KEY) or {})
        task_id = self.process.active_task_id or self.process.process_id
        owner = state.get('process_id', self.process.process_id if not self.process.parent_id else '')
        if state.get('task_id') != task_id or owner != self.process.process_id:
            state = {'task_id': task_id, 'process_id': self.process.process_id,
                     'request': instruction or self.process.task_description, 'enforced': True,
                     'message_start': max(0, len(self.process.session.messages) - 1)}
            self.process.update_context_snapshot(lambda ctx: {**ctx, KEY: state})
        elif not state.get('process_id'):
            state['process_id'] = self.process.process_id
            self.process.update_context_snapshot(lambda ctx: {**ctx, KEY: state})
        graph = self.workflow.status()
        raw = self.workflow._load()
        answers, confirmed = [], []
        for node in graph.get('nodes', []):
            if node['kind'] != 'decision':
                continue
            items = [a for a in self.workflow._answers(node) if a.get('decision_id') == raw.get('requests', {}).get(node['id'])]
            if items:
                item = {'node_id': node['id'], 'state_topic': node['state_topic'],
                        'decision_id': items[-1]['decision_id'], 'answer': items[-1]['answer']}
                (confirmed if node['status'] == 'complete' else answers).append(item)
        from .alignment_evidence import CONTROL_DECISION_KINDS
        requests = [e for e in self.process.session.host_ledger if e.get('event') == 'user_decision_requested'
                    and e.get('task_id') == task_id and e.get('kind') not in CONTROL_DECISION_KINDS]
        payload = {'request': (state.get('scope') or {}).get('request') or state['request'], 'original_request': state['request'],
                   'scope': state.get('scope'), 'stage': 'question_proposal' if proposed_questions is not None else 'delivery' if candidate else 'admission',
                   'questions_asked': requests, 'actual_answers': answers,
                   'confirmed_answers': confirmed, 'workflow': graph, 'candidate': candidate,
                   'task_constraints': self.process.task_constraints,
                   'user_updates': [m['content'] for m in self.process.session.messages[state['message_start']:]
                                    if m.get('mailbox_message_id') and m.get('message_type') == 'conversation']}
        from .decision_support import confirmed_user_state
        payload['historical_decisions'] = confirmed_user_state(self.process.session.host_ledger)
        payload['working_assumptions'] = state.get('assumptions') or {}
        if proposed_questions is not None:
            payload['proposed_questions'] = proposed_questions
        if proposed_contracts:
            payload['proposed_contracts'] = proposed_contracts
        payload['conversation_context'] = [{k: m[k] for k in ('role', 'content')}
            for m in self.process.session.messages[:state['message_start']]
            if m.get('content') and (m.get('role') == 'assistant' or m.get('message_type') == 'conversation')][-8:]
        for message in payload['conversation_context']:
            message['content'] = str(message['content'])[:6000]
        if candidate and proposed_files is not None:
            from .delivery_artifacts import collect_artifacts
            payload['published_artifacts'], payload['artifact_notices'] = collect_artifacts(self.process, proposed_files)
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 120_000:
            raise ValueError('alignment context exceeds bounded semantic review; inspect retained evidence')
        return payload

    def apply(self, assessment, payload):
        if not isinstance(assessment, dict) or len(json.dumps(assessment).encode()) > 64_000:
            raise ValueError('alignment assessment must be a bounded object')
        if assessment.get('schema_error'):
            raise ValueError(str(assessment['schema_error']))
        from .alignment_scope import validate_request_frame
        request_frame = validate_request_frame(assessment, payload)
        status = assessment.get('status')
        delivery = assessment.get('delivery_check')
        if payload.get('stage') != 'delivery' and delivery is not None:
            raise ValueError('delivery_check is only valid at delivery; admission assesses whether work may proceed, not whether an absent final answer is complete')
        if payload.get('stage') == 'delivery' and delivery is not None:
            if not isinstance(delivery, dict) or type(delivery.get('complete')) is not bool:
                raise ValueError('delivery_check requires a boolean complete verdict')
            evidence = delivery.get('evidence') or []
            if not isinstance(evidence, list) or len(evidence) > 256:
                raise ValueError('delivery_check evidence must be a bounded list')
            if any(not isinstance(quote, str) or not quote.strip() or quote not in payload['candidate'] for quote in evidence):
                raise ValueError('delivery_check evidence must quote the actual candidate')
            from .candidate_evidence import resolve_candidate_references
            evidence = evidence + resolve_candidate_references(payload['candidate'], delivery.get('evidence_refs', []),
                                                               payload.get('published_artifacts', []))
            if delivery['complete'] and not evidence:
                raise ValueError('complete delivery requires candidate evidence')
            if len(evidence) > 8:
                # All quotes were verified above. Keeping a bounded audit sample
                # is mechanical formatting, not a new semantic verdict; do not
                # spend another model call asking it to shorten valid evidence.
                assessment = {**assessment, 'delivery_check': {**delivery, 'evidence': evidence[:8]}}
            if not delivery['complete'] and status not in {'needs_input', 'waiting'}:
                return {'status': 'repair', 'reason': str(delivery.get('reason') or 'The candidate does not supply the requested deliverable.')}
        proposals = assessment.get('interpretations')
        questions = assessment.get('questions')
        assumptions = assessment.get('assumptions') or []
        if not isinstance(assumptions, list) or len(assumptions) > 16:
            raise ValueError('assumptions must be a bounded list')
        if status == 'waiting' and assumptions:
            raise ValueError('explicit waiting cannot introduce working defaults')
        if status not in {'ready', 'needs_input', 'waiting', 'repair'} or not isinstance(proposals, list) or not isinstance(questions, list) or len(questions) > 8:
            raise ValueError('invalid alignment assessment schema')
        # Prepare changes on a clone. Invalid final proposals cannot partially
        # resolve real requirements or publish orphaned questions.
        from .models import AgentProcess, RingLevel
        from .session import AgentSession
        from .engineering_workflow import EngineeringWorkflow, compile_plan, workspace
        context, _ = self.process.read_context_snapshot()
        shadow = AgentProcess(process_id=self.process.process_id, role=self.process.role,
                              ring_level=RingLevel.RING_3, active_task_id=self.process.active_task_id,
                              context_snapshot=copy.deepcopy(context),
                              worktree_path=self.process.worktree_path, workspace_root=self.process.workspace_root,
                              session=AgentSession(host_ledger=copy.deepcopy(self.process.session.host_ledger)),
                              tool_receipts=copy.deepcopy(self.process.tool_receipts), task_kind=self.process.task_kind)
        w = EngineeringWorkflow(shadow)
        scope = optional_object(assessment.get('scope_update'), 'scope_update')
        if scope:
            from .alignment_evidence import source_answer, answer_digest
            source = source_answer(shadow.session.host_ledger, shadow.active_task_id or shadow.process_id,
                                   scope.get('decision_id'), scope.get('source_node_id', ''))
            quote = str(scope.get('quote') or '').strip()
            if (not source or scope.get('mode') != 'exploration' or not quote or quote not in source['answer']
                    or scope.get('request') != quote or shadow.task_kind not in {'answer', 'plan'} or status == 'waiting'):
                raise ValueError('exploratory scope requires an exact actual user request and read-only task')
            scope = {**scope, 'answer_digest': answer_digest(source['answer'])}
            shadow.update_context_snapshot(lambda ctx: {**ctx, KEY: {**ctx[KEY], 'scope': scope}})
            shadow.session.host_ledger.append({'event': 'alignment_scope_updated', 'task_id': shadow.active_task_id or shadow.process_id, **scope})
        interpreted_nodes = set()
        for proposal in proposals:
            if not isinstance(proposal, dict) or not proposal.get('node_id'):
                raise ValueError('interpretations require an existing node identity')
            if proposal['node_id'] in interpreted_nodes:
                raise ValueError('each decision node requires ONE interpretation per assessment; preserve known constraints in a single value and distinguish remaining delegated choices')
            interpreted_nodes.add(proposal['node_id'])
            w.resolve_answer(proposal.get('node_id'), proposal.get('decision_id'), proposal)
        state = w._load()
        plan = copy.deepcopy(state.get('plan') or {'profiles': ['alignment'], 'nodes': []})
        by_topic = {n.get('state_topic'): n for n in plan['nodes'] if n['kind'] == 'decision'}
        by_id = {n['id']: n for n in by_topic.values()}
        aliases = {}
        questions = copy.deepcopy(questions)
        for question in questions:
            if question.get('node_id'):
                node = by_id.get(question['node_id'])
                if not node:
                    raise ValueError('renewed question must bind an existing decision node_id')
                aliases[question.get('state_topic')] = node['id']
                question['state_topic'] = node['state_topic']
        overrides = copy.deepcopy(state.get('request_overrides') or {})
        elicited = copy.deepcopy(state.get('elicitation_tiers') or {})
        topic_ids = {topic: n['id'] for topic, n in by_topic.items()}
        topic_ids.update({q.get('state_topic'): 'q_' + hashlib.sha256(str(q.get('state_topic')).encode()).hexdigest()[:16]
                          for q in questions if q.get('state_topic') not in topic_ids})
        topic_ids.update(aliases)
        bound_questions = set()
        for question in questions:
            topic = str(question.get('state_topic') or '').strip()
            if not topic or len(topic) > 120 or not question.get('question') or not question.get('why_user_must_decide'):
                raise ValueError('new questions require a stable facet and user-owned reason')
            from .alignment_question import question_request
            request = question_request(question, payload)
            node = by_topic.get(topic)
            if node:
                if node['id'] in state.get('records', {}):
                    raise ValueError('do not repeat a resolved question; use explicit revision when intent changes')
            else:
                node = {'id': 'q_' + hashlib.sha256(topic.encode()).hexdigest()[:16],
                        'kind': 'decision', 'state_topic': topic, 'before_mutation': True,
                        'depends_on': [topic_ids[t] for t in question.get('depends_on_topics', [])] if 'depends_on_topics' in question
                                      else question.get('depends_on') or [], 'request': request}
                plan['nodes'].append(node)
                by_topic[topic] = node
                tier = question.get('tier', 'foundational')
                if tier not in {'foundational', 'refinement'}:
                    raise ValueError('invalid semantic decision tier')
                elicited[node['id']] = tier
            if node['id'] in bound_questions:
                raise ValueError('a decision identity may only be asked once in a round')
            bound_questions.add(node['id'])
            overrides[node['id']] = request
        if plan['nodes']:
            prepared = w.configured_state(compile_plan(plan, workspace(shadow)))
            prepared['request_overrides'] = overrides
            prepared['elicitation_tiers'] = elicited
            w._save(prepared)
        # Optional elicitation is not an immutable user/test requirement. A
        # certified default can support a read-only proposal, never execution.
        from .alignment_evidence import answer_digest
        state = w._load()
        defaults = copy.deepcopy(shadow.context_snapshot.get(KEY, {}).get('assumptions') or {})
        for item in assumptions:
            topic, value, reason = (str(item.get(k) or '').strip() for k in ('state_topic', 'value', 'reason'))
            if not topic or not value or not reason or len(value) > 2000 or len(reason) > 2000:
                raise ValueError('working defaults require a bounded facet, value and reason')
            if item.get('tier') != 'refinement':
                raise ValueError('every default must be classified as refinement; foundational intent requires a user decision')
            node = by_topic.get(topic)
            if node:
                if elicited.get(node['id']) != 'refinement' or shadow.task_kind not in {'answer', 'plan'}:
                    raise ValueError('defaults cannot waive foundational intent, execution or user-declared requirements')
                if node['id'] in state.get('records', {}) and not state['records'][node['id']].get('defaulted'):
                    raise ValueError('defaults cannot replace confirmed user intent')
                latest = next((e for e in reversed(shadow.session.host_ledger) if e.get('event') == 'user_decision_interpreted'
                               and e.get('node_id') == node['id'] and e.get('task_id') == state['task_id']), {})
                if latest.get('disposition') == 'deferred':
                    raise ValueError('explicit waiting cannot be replaced by an assumption')
                source = w._answers(node)
                digest = answer_digest(source[-1]['answer']) if source else ''
                records = state['records']
                if all(d in records for d in node['depends_on']):
                    records[node['id']] = {'defaulted': True, 'value': value, 'source_digest': digest,
                        'dependencies': {d: hashlib.sha256(json.dumps(records[d], sort_keys=True).encode()).hexdigest() for d in node['depends_on']}}
                    shadow.session.host_ledger.append({'event': 'alignment_assumption', 'task_id': state['task_id'],
                        'node_id': node['id'], 'state_topic': topic, 'value': value, 'reason': reason, 'source_digest': digest,
                        'scope': 'read_only_proposal'})
            defaults[topic] = {'value': value, 'reason': reason, 'authority': 'assistant_default', 'scope': 'read_only_proposal'}
        if state:
            w._save(state)
        shadow.update_context_snapshot(lambda ctx: {**ctx, KEY: {**ctx.get(KEY, {}), 'assumptions': defaults}})
        graph = w.status()
        unresolved = [n for n in graph.get('nodes', []) if n['kind'] == 'decision' and n['status'] not in {'complete', 'assumed'}]
        if status == 'needs_input':
            partial = {p.get('node_id') for p in proposals if p.get('disposition') == 'partial'}
            missing = (partial & set(graph.get('question_frontier') or [])) - bound_questions
            if missing:
                raise ValueError('partial answers require narrowed questions bound to the SAME node_id: ' + ', '.join(sorted(missing)))
        from .alignment_evidence import exploratory_scope
        exploration = exploratory_scope(shadow)
        if status == 'ready' and any(not (exploration and n['id'] in elicited) for n in unresolved):
            raise ValueError('ready cannot discard unresolved actual questions. If the latest actual reply requests a current exploratory discussion/comparison, use scope_update(mode=exploration, decision_id, source_node_id if needed, quote=request=exact requested discussion text). Preserve the original unresolved nodes; do not change ready to waiting merely because those original choices remain open.')
        if status == 'needs_input' and not graph.get('question_frontier'):
            raise ValueError('needs_input requires an answerable frontier')
        if status == 'waiting' and not unresolved:
            raise ValueError('waiting requires retained unresolved intent')
        start = len(self.process.session.host_ledger)
        self.process.session.host_ledger.extend(shadow.session.host_ledger[start:])
        self.process.update_context_snapshot(lambda ctx: {**ctx, **{k: copy.deepcopy(v) for k, v in shadow.context_snapshot.items()
                                                                  if k in {'engineering_workflow', KEY}}})
        if request_frame is not None:
            self.process.update_context_snapshot(lambda ctx: {**ctx, KEY: {
                **ctx.get(KEY, {}), 'request_frame': copy.deepcopy(request_frame)}})
        if scope and scope != context.get(KEY, {}).get('scope'):
            self.process.session.append_host_steering('Current user-requested exploratory deliverable: ' + scope['request']
                + '\nUnanswered original choices remain open; no execution is authorized.', steering_type='alignment_scope', version=scope['decision_id'])
        previous_defaults = context.get(KEY, {}).get('assumptions') or {}
        changed_defaults = {k: v for k, v in defaults.items() if previous_defaults.get(k) != v}
        if changed_defaults:
            self.process.session.append_host_steering('Working defaults for this read-only proposal, not user-confirmed facts or execution authority:\n'
                + json.dumps(changed_defaults, ensure_ascii=False), steering_type='alignment_assumptions', version=str(len(self.process.session.host_ledger)))
        if status in {'needs_input', 'waiting'}:
            if status == 'waiting':
                # Reuse the exact unresolved question; no fabricated new task.
                original = next((r for r in reversed(payload['questions_asked'])
                                 if any(n['id'] in (r.get('questions') and [q['node_id'] for q in r['questions']] or [])
                                        or r.get('state_topic') == n['state_topic'] for n in unresolved)), None)
                if original:
                    for n in unresolved:
                        item = next((q for q in original.get('questions', []) if q['node_id'] == n['id']), original)
                        overrides[n['id']] = {k: item[k] for k in ('question', 'why_user_must_decide', 'options', 'kind', 'allow_free_form') if k in item}
                    state = self.workflow._load()
                    state['request_overrides'] = overrides
                    self.workflow._save(state)
            decision = self.workflow.ask_frontier()
            if decision.get('status') != 'awaiting_user':
                raise ValueError('alignment did not publish a valid pending frontier')
            return {'status': status, 'decision': decision, 'reason': str(assessment.get('reason') or '')}
        if status == 'ready' and payload.get('candidate'):
            from .alignment_evidence import alignment_input_digest, answer_digest
            from .delivery_artifacts import artifact_manifest
            refs = (delivery or {}).get('evidence_refs') or []
            used_artifacts = [artifact for artifact in payload.get('published_artifacts', [])
                              if any(part['id'] in refs for part in artifact['segments'])]
            self.process.update_context_snapshot(lambda ctx: {**ctx, KEY: {**ctx.get(KEY, {}),
                'delivery': {'candidate_digest': answer_digest(payload['candidate']),
                             'input_digest': alignment_input_digest(self.process),
                             'artifact_manifest': artifact_manifest(used_artifacts)}}})
        if status == 'ready' and payload.get('stage') != 'delivery':
            from .alignment_evidence import alignment_input_digest
            declarations = payload.get('proposed_contracts') or []
            digest = alignment_input_digest(self.process, contract_override=declarations[-1] if declarations else None)
            self.process.update_context_snapshot(lambda ctx: {**ctx, KEY: {**ctx.get(KEY, {}),
                'admission_review': {'status': 'ready', 'input_digest': digest}}})
        return {'status': status, 'reason': str(assessment.get('reason') or '')}
