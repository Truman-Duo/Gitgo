"""Compile the main Agent's meaning through existing evidence/workflow services.

No semantic classifier or model request lives here. The Agent supplies the
disposition, supported value and quote; Runtime supplies unambiguous identities.
"""
from copy import deepcopy
import hashlib
import json

POLICY = 'main_agent_v1'
SCOPE_MODES = ('exploration', 'decision_summary', 'decision_comparison')
RESPONSE_SCOPE_SCHEMA = {'type': 'object', 'properties': {
    'quote': {'type': 'string', 'maxLength': 2000},
    'discussion': {'type': 'string', 'minLength': 1, 'maxLength': 32000}},
    'required': ['quote'], 'additionalProperties': False}
UPDATE_SCHEMA = {'type': 'array', 'maxItems': 16, 'items': {'type': 'object'}}


class DialogueUpdateError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _discussion(item):
    if 'discussion' not in item:
        return None
    text = item['discussion']
    if not isinstance(text, str) or not text.strip() or len(text) > 32000 or 'mode' in item:
        raise DialogueUpdateError('ALIGNMENT_DISCUSSION_INVALID',
            'scope_update.discussion must be nonempty requested discussion text, at most 32000 characters, '
            'without a legacy mode. Omit it for record-only status. Nothing in the batch was applied.')
    return text.strip()


def completion_arguments(args):
    """Normalize an explicit response component, without inferring user intent.

    The original tool arguments stay in Trace. A source-bound status needs no
    duplicate prose; discussion text is the actual candidate, not a mode flag.
    """
    scope = args.get('scope_update')
    if not isinstance(scope, dict) or 'mode' in scope:
        return args
    body = _discussion(scope)
    return {**args, 'result': body if body is not None else (args.get('result') or 'Decision status.')}


def response_components_required(process):
    """An answered read-only dialogue needs a declared response boundary.

    This checks actual receipts and the existing valid scope, not words in the
    user's message. Ordinary direct answers and action contracts are untouched.
    """
    if getattr(process, 'task_kind', None) not in {'answer', 'plan'}:
        return False
    from .elicitation_relevance import read_only_eligible
    from .alignment_evidence import business_answer_receipts, exploratory_scope
    return bool(read_only_eligible(process)
                and business_answer_receipts(process.session.host_ledger, process.active_task_id or process.process_id)
                and not exploratory_scope(process))


def require_response_components(process, scope_update=None):
    if response_components_required(process) and (scope_update is None
            or isinstance(scope_update, dict) and 'mode' in scope_update):
        raise DialogueUpdateError('ALIGNMENT_RESPONSE_COMPONENTS_REQUIRED',
            'After an actual clarification reply, complete_task requires scope_update={quote:"exact reply/request", '
            'discussion?:"requested response or comparison only"}, plus any answer_updates. '
            'Omit discussion for recorded status only. A result-only draft or legacy mode cannot establish this boundary. '
            'No extra review, user question or permission is needed; the failed call applied no updates.')


def _response_scope(process, item):
    from .elicitation_relevance import read_only_eligible
    from .alignment_evidence import answer_digest, exploratory_scope
    if isinstance(item, dict) and 'mode' in item and item['mode'] not in SCOPE_MODES:
        raise DialogueUpdateError('ALIGNMENT_SCOPE_MODE_INVALID',
            'scope_update.mode is a legacy presentation field. Use {quote, discussion?} for current '
            'record status; legacy mode must be exploration, decision_summary or decision_comparison. '
            'The complete failed batch was not applied.')
    if not isinstance(item, dict) or not read_only_eligible(process):
        raise DialogueUpdateError('ALIGNMENT_SCOPE_UPDATE_INVALID',
            'scope_update requires a source-bound read-only response, not execution or permission.')
    body = _discussion(item)
    source = _source(process, item)
    quote = item['quote']
    if len(quote) > 2000:
        raise DialogueUpdateError('ALIGNMENT_SCOPE_UPDATE_INVALID', 'Quote the bounded actual request for this response.')
    context, _ = process.read_context_snapshot()
    previous = (context.get('alignment_runtime') or {}).get('scope') or {}
    if (previous.get('response_contract') == 'decision_status_v2' and exploratory_scope(process)
            and 'mode' in item):
        raise DialogueUpdateError('ALIGNMENT_RESPONSE_SCOPE_DOWNGRADE',
            'The current source-bound record response cannot be replaced by a legacy prose scope. '
            'Omit scope_update to retain it, or use {quote, discussion?} for an explicit current response. '
            'No updates were applied; a later actual reply is still evaluated from its own source.')
    scope = {'mode': 'exploration', 'decision_id': source['decision_id'],
             'source_node_id': source.get('source_node_id', ''), 'quote': quote,
             'request': quote, 'answer_digest': answer_digest(source['answer'])}
    if 'mode' not in item:
        scope.update(presentation='decision_status', response_contract='decision_status_v2',
                     include_discussion=body is not None)
        if body is not None:
            scope['discussion_digest'] = answer_digest(body)
    elif item['mode'] in ('decision_summary', 'decision_comparison'):
        scope.update(presentation='decision_status', include_discussion=item['mode'] == 'decision_comparison')
    return scope


def owns_dialogue(state):
    return state.get('review_policy') == POLICY


def activate(process, instruction=''):
    task = process.active_task_id or process.process_id
    def transform(ctx):
        state = ctx.get('alignment_runtime') or {}
        if state.get('task_id') != task or state.get('process_id') != process.process_id:
            state = {'task_id': task, 'process_id': process.process_id,
                     'request': instruction or process.task_description,
                     'message_start': max(0, len(process.session.messages) - 1)}
        # Existing receipts, graph and old review observations remain intact.
        # This marker selects the new owner, never a semantic pass certificate.
        return {**ctx, 'alignment_runtime': {**state, 'review_policy': POLICY,
                                           'semantic_owner': 'main_agent'}}
    process.update_context_snapshot(transform)


def bind_candidate(process, candidate, files=None):
    """Identity of actual delivery, not a verdict on its semantic quality."""
    from .completion_protocol import CompletionClaim
    from .alignment_evidence import alignment_input_digest, answer_digest
    normalized = CompletionClaim.from_response(candidate, step=process.steps_used).result
    binding = {'candidate_digest': answer_digest(normalized),
               'input_digest': alignment_input_digest(process), 'task_kind': process.task_kind,
               'source': 'main_agent_candidate', 'scope': 'candidate_identity_only'}
    claim = process.completion_claim
    if files is None and claim is not None and claim.result == normalized:
        files = list(claim.files)
    if files:
        from .delivery_artifacts import collect_artifacts, artifact_manifest
        artifacts, _ = collect_artifacts(process, files)
        binding['artifact_manifest'] = artifact_manifest(artifacts)
    process.update_context_snapshot(lambda ctx: {**ctx, 'alignment_runtime': {
        **ctx['alignment_runtime'], 'delivery': binding}})


def _target(workflow, item):
    state = workflow._load()
    nodes = [n for n in state.get('plan', {}).get('nodes', []) if n['kind'] == 'decision']
    candidates = [n for n in nodes if
        (not item.get('node_id') or n['id'] == item['node_id'])
        and (not item.get('state_topic') or n['state_topic'] == item['state_topic'])]
    if not candidates and not item.get('node_id') and isinstance(item.get('state_topic'), str) and item['state_topic'].strip() and item.get('disposition') in {'confirmed', 'partial', 'delegated'}:
        # The user can volunteer a new fact/choice in a real reply. The Agent
        # names its meaning; compile an additive, source-bound node without
        # manufacturing another question or weakening existing requirements.
        from .engineering_workflow import declared_plan
        topic = item['state_topic']
        plan = deepcopy(declared_plan(state['plan']) if state else {'profiles': ['alignment'], 'nodes': []})
        name = 'q_' + hashlib.sha256(topic.encode()).hexdigest()[:16]
        plan['nodes'].append({'id': name, 'kind': 'decision', 'state_topic': topic,
                              'depends_on': [], 'before_mutation': True})
        prepared = workflow.configured_state(workflow.compile_declared(plan))
        # Match the existing model-elicited decision boundary. A volunteered
        # partial fact must stay open for execution, but it is not a new
        # user-configured obligation preventing a source-bound readonly summary.
        # Existing nodes never enter this branch and cannot be reclassified.
        prepared['elicitation_tiers'] = {**prepared.get('elicitation_tiers', {}),
                                       name: 'foundational'}
        workflow._save(prepared)
        return next(n for n in prepared['plan']['nodes'] if n['id'] == name)
    if len(candidates) != 1:
        code = 'ALIGNMENT_REPLY_TARGET_AMBIGUOUS' if len(candidates) > 1 else 'ALIGNMENT_REPLY_TARGET_MISSING'
        raise DialogueUpdateError(code, 'Select one current decision by its exact state_topic or node_id; available topics: '
                                  + ', '.join(n['state_topic'] for n in nodes)
                                  + '. New topics require an actual volunteered confirmed/partial fact or delegated choice. '
                                    'Unasked unknowns belong in the open-items prose, not answer_updates.')
    return candidates[0]


def _source(process, item):
    from .alignment_evidence import business_answer_receipts
    receipts = business_answer_receipts(process.session.host_ledger, process.active_task_id or process.process_id)
    if not receipts:
        raise DialogueUpdateError('ALIGNMENT_REPLY_SOURCE_UNAVAILABLE', 'No current business reply is available; control, cancelled and foreign-task receipts cannot be used.')
    decision = item.get('decision_id') or receipts[-1]['decision_id']
    quote = item.get('quote')
    deferred_reference = (item.get('disposition') == 'deferred'
        and (quote is None or isinstance(quote, str) and not quote.strip()))
    if not deferred_reference and (not isinstance(quote, str) or not quote.strip()):
        raise DialogueUpdateError('ALIGNMENT_REPLY_UPDATE_INVALID', 'quote must be one nonempty contiguous exact substring of the received reply; preserve punctuation/newlines and do not join clauses with an ellipsis.')
    candidates = [r for r in receipts if r['decision_id'] == decision and (deferred_reference or quote in r['answer'])
        and (not item.get('source_node_id') or r.get('source_node_id') == item['source_node_id'])
        and (not item.get('source_topic') or r.get('state_topic') == item['source_topic'])]
    if not candidates:
        raise DialogueUpdateError('ALIGNMENT_REPLY_SOURCE_UNAVAILABLE', 'quote is not a contiguous exact substring of an actual reply. Preserve punctuation/newlines; do not paraphrase, reorder or join clauses with an ellipsis. Use the current input, or identify an actual earlier receipt.')
    if len(candidates) != 1:
        raise DialogueUpdateError('ALIGNMENT_REPLY_SOURCE_AMBIGUOUS', 'The quote matches several answered facets; select its exact source_topic or source_node_id.')
    # Deferral asserts no new fact and leaves the requirement unresolved. Bind
    # its one unambiguous actual receipt mechanically; confirmations, partial
    # facts and scope changes still require the Agent's exact selected quote.
    return {**candidates[0], '_bound_quote': candidates[0]['answer']} if deferred_reference else candidates[0]


def apply_answer_updates(process, updates, scope_update=None):
    """Atomically bind a small batch; never infer an answer or permission."""
    if not isinstance(updates, list) or len(updates) > 16 or len(json.dumps(updates).encode()) > 32000:
        raise DialogueUpdateError('ALIGNMENT_REPLY_UPDATE_INVALID', 'answer_updates requires at most 16 bounded interpretation objects.')
    if not updates and scope_update is None:
        return
    from .models import AgentProcess
    from .session import AgentSession
    from .engineering_workflow import EngineeringWorkflow, DecisionRevisionError
    from .elicitation_relevance import retire_questions, ElicitationRelevanceError
    with process._context_lock:
        context, _ = process.read_context_snapshot()
        shadow = AgentProcess(process_id=process.process_id, role=process.role,
            ring_level=process.ring_level, active_task_id=process.active_task_id,
            actor_kind=process.actor_kind, context_snapshot=deepcopy(context),
            worktree_path=process.worktree_path, workspace_root=process.workspace_root,
            session=AgentSession(host_ledger=deepcopy(process.session.host_ledger)),
            tool_receipts=deepcopy(process.tool_receipts), task_kind=process.task_kind,
            task_description=process.task_description)
        workflow = EngineeringWorkflow(shadow)
        start = len(shadow.session.host_ledger)
        targets, retirements = set(), []
        try:
            if updates:
                activate(shadow)
            for item in updates:
                if not isinstance(item, dict):
                    raise DialogueUpdateError('ALIGNMENT_REPLY_UPDATE_INVALID', 'Each answer update must be an object.')
                if item.get('disposition') not in ('confirmed', 'partial', 'deferred', 'delegated', 'not_needed'):
                    raise DialogueUpdateError('ALIGNMENT_REPLY_DISPOSITION_INVALID',
                        'disposition must be confirmed, partial, deferred, delegated or not_needed; '
                        'choose the meaning supported by the actual reply. Nothing in this batch was applied.')
                node = _target(workflow, item)
                source_input = item
                if item.get('correct') is True and not item.get('decision_id'):
                    current = workflow._load()
                    decision = (current.get('records', {}).get(node['id'], {}).get('decision_id')
                                or current.get('requests', {}).get(node['id']))
                    if decision:
                        source_input = {**item, 'decision_id': decision}
                        if not item.get('source_node_id') and not item.get('source_topic'):
                            previous = next((e for e in reversed(shadow.session.host_ledger)
                                if e.get('event') == 'user_decision_interpreted'
                                and e.get('task_id') == shadow.active_task_id and e.get('node_id') == node['id']
                                and e.get('decision_id') == decision), {})
                            if previous.get('source_node_id'):
                                source_input['source_node_id'] = previous['source_node_id']
                source = _source(shadow, source_input)
                if node['id'] in targets:
                    raise DialogueUpdateError('ALIGNMENT_REPLY_UPDATE_INVALID', 'Interpret each target once in a batch; do not overwrite a confirmed value with a second disposition.')
                targets.add(node['id'])
                if item.get('disposition') in {'partial', 'deferred'} and not str(item.get('remaining') or '').strip():
                    raise DialogueUpdateError('ALIGNMENT_REPLY_REMAINDER_MISSING', 'A partial or deferred interpretation must state the remaining issue; it is not a confirmed answer.')
                bound = {**item, 'node_id': node['id'], 'decision_id': source['decision_id']}
                if '_bound_quote' in source:
                    bound['quote'] = source['_bound_quote']
                if item.get('disposition') in {'confirmed', 'delegated', 'partial'}:
                    # A literal source is the accepted requirement. Keep a
                    # supplied paraphrase as an unverified Agent proposal;
                    # quoting a source cannot certify additional consequences.
                    bound['value_basis'] = 'source_quote'
                if source.get('source_node_id'):
                    bound['source_node_id'] = source['source_node_id']
                if item.get('disposition') == 'not_needed':
                    retirements.append(bound)
                else:
                    workflow.resolve_answer(node['id'], source['decision_id'], bound)
            retire_questions(workflow, retirements)
            if scope_update is not None:
                scope = _response_scope(shadow, scope_update)
                activate(shadow)
                shadow.update_context_snapshot(lambda ctx: {**ctx, 'alignment_runtime': {
                    **ctx['alignment_runtime'], 'scope': scope}})
                shadow.session.host_ledger.append({'event': 'alignment_scope_updated',
                    'task_id': shadow.active_task_id or shadow.process_id, **scope, 'interpreter': 'main_agent'})
        except ElicitationRelevanceError as exc:
            raise DialogueUpdateError('ALIGNMENT_QUESTION_RETIREMENT_INVALID', str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, DialogueUpdateError):
                raise
            if isinstance(exc, DecisionRevisionError):
                raise DialogueUpdateError(exc.code, str(exc)) from exc
            raise DialogueUpdateError('ALIGNMENT_REPLY_UPDATE_INVALID', str(exc)) from exc
        for event in shadow.session.host_ledger[start:]:
            if event.get('event') == 'user_decision_interpreted':
                event['interpreter'] = 'main_agent'
        process.session.host_ledger.extend(shadow.session.host_ledger[start:])
        if 'engineering_workflow' in shadow.context_snapshot:
            process.update_context_snapshot(lambda ctx: {**ctx, 'engineering_workflow':
                deepcopy(shadow.context_snapshot['engineering_workflow'])})
        if updates or scope_update is not None:
            process.update_context_snapshot(lambda ctx: {**ctx, 'alignment_runtime':
                deepcopy(shadow.context_snapshot['alignment_runtime'])})


def update_error(exc):
    from backend.core.errors import error_payload
    return error_payload(exc.code, message=str(exc), details={
        'phase': 'main_dialogue_update', 'effect_state': 'not_executed', 'authority_granted': False})
