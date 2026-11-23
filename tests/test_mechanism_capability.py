"""Migration invariants through real storage, lifecycle and ToolPipeline.

These fixtures are engineering regressions, not human semantic/API evidence.
"""
from copy import deepcopy
from types import SimpleNamespace
import pytest

from tests.test_mechanism_interactions import runtime
from backend.core.mechanism_runtime.application import Application
from backend.core.mechanism_runtime.repository import NAMESPACE
from backend.core.loop.task_maintenance import source_receipts


def declaration():
    return {'id': 'guidance', 'intent': 'Retain the actual user criteria before context assembly',
        'on': ['before_context'], 'calls': [{'id': 'note', 'use': 'context.project',
            'parameters': {'content': 'Preserve the public interface.'}}]}


def pipeline_tool(runtime):
    from backend.core.loop.protocol_tools.harness_composition import install
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    from backend.core.loop.tool_pipeline import ToolPipeline
    app, p, _, _, ctx, *_ = runtime
    bindings = SimpleNamespace(process=p, llm_provider=None, on_stream_event=None, tools={}, manager=None, dispatcher=None)
    install(bindings)
    tool = bindings.tools['harness_mechanism']
    def execute(args):
        call = {'name': tool.name, 'args': args}
        boundary = preview_tool_boundaries([call], bindings.tools, p)
        return boundary, ToolPipeline().execute(call, tool, ctx, 'protocol-migration', 0)
    return tool, execute


def test_current_discovery_and_legacy_saved_preview_have_one_canonical_compiler(runtime):
    from tests.test_mechanism_interactions import checkpoint_program
    from jsonschema import Draft202012Validator
    app, p, *_ = runtime
    tool, execute = pipeline_tool(runtime)
    schema = tool.to_openai_function()['function']['parameters']
    Draft202012Validator.check_schema(schema)
    assert 'publish' not in schema['properties']['operation']['enum']
    assert 'program' not in schema['properties']
    Draft202012Validator(schema).validate({'operation': 'preview', 'declaration': declaration()})
    boundary, result = execute({'operation': 'preview', 'program': checkpoint_program(p)})
    assert not boundary.contracts and boundary.observation_only
    assert not result.is_error, result.formatted
    assert result.data['compiled']['schema_version'] == 2
    assert app.repo.list_instances(app.task) == []


def test_factored_model_schema_keeps_operation_constraints_and_exact_admission(runtime):
    from jsonschema import Draft202012Validator
    from backend.core.mechanism_runtime.capability_protocol import authoring_schema, inbox_schema, lifecycle_schema, COMMIT
    import json
    tool, _ = pipeline_tool(runtime)
    schema = tool.to_openai_function()['function']['parameters']
    # Restore the previous full branches for an independent validation oracle.
    full = deepcopy(schema)
    full['oneOf'] = [authoring_schema(op) for op in ('apply','revise','use')]
    full['oneOf'].append(deepcopy(COMMIT))
    full['oneOf'] += [authoring_schema(op, preview=True) for op in ('apply','revise','use')]
    full['oneOf'] += [inbox_schema(op) for op in ('inbox_revise','inbox_merge','inbox_discard','inbox_adopt')]
    full['oneOf'] += [lifecycle_schema(op) for op in ('activate','suspend','detach','reconcile','require')]
    full['oneOf'] += [{'properties': {'operation': {'enum': ['catalog','status']}}}]
    compact, previous = Draft202012Validator(schema), Draft202012Validator(full)
    valid = [{'operation':'apply','declaration':declaration()},
        {'operation':'commit','preview_ref':'sha256:'+'a'*64},
        {'operation':'preview','declaration':declaration()},
        {'operation':'revise','definition_id':'guidance','revision':{'intent':'New semantic intent'}},
        {'operation':'use','definition_id':'guidance','definition_revision':2},
        {'operation':'suspend','instance_id':'guidance'},
        {'operation':'require','requirements':[]},
        {'operation':'inbox_merge','candidates':[{'id':'one'},{'id':'two'}],'text':'Combined requirement'},
        {'operation':'inbox_adopt','candidate_id':'one','quote':'Actual user adoption'}]
    cases = list(valid)
    for args in valid:
        cases.append({**args,'unknown_runtime_field':'forbidden'})
        cases.append({**args,'program':{'forged':True}})
        for key in args:
            cases.append({k:v for k,v in args.items() if k!=key})
        cases.append({**args,'definition_revision':False})
    cases += [{'operation':'apply','declaration':{**declaration(),'calls':[{'id':'missing-use'}]}},
        {'operation':'use','definition_id':''}, {'operation':'suspend','instance_id':''},
        {'operation':'inbox_merge','candidates':[{'id':'one','revision':9},{'id':'two'}],'text':'Not the current signature'}]
    assert all(compact.is_valid(value) for value in valid)
    assert all(compact.is_valid(value)==previous.is_valid(value) for value in cases)
    assert len(json.dumps(schema)) < len(json.dumps(full)) * .8


def test_discovery_separates_callable_components_from_runtime_owned_entries(runtime):
    _, execute = pipeline_tool(runtime)
    _, catalog = execute({'operation': 'catalog'})
    assert not catalog.is_error, catalog.formatted
    assert 'completion' in catalog.data['lifecycle_boundaries']
    assert 'runtime.event' not in {entry['id'] for entry in catalog.data['elements']}
    assert all(entry['callable'] for entry in catalog.data['elements'])
    _, inspected = execute({'operation': 'catalog', 'element_id': 'runtime.event'})
    assert not inspected.is_error and inspected.data['elements'][0]['callable'] is False


def test_preview_is_read_only_and_does_not_prepare_an_unreviewed_commit(runtime):
    app, p, *_ = runtime
    _, execute = pipeline_tool(runtime)
    args = {'operation': 'apply', 'declaration': declaration()}
    boundary, preview = execute({**args, 'operation': 'preview'})
    assert not boundary.contracts and boundary.observation_only
    assert not preview.is_error, preview.formatted
    assert preview.data['committed'] is False
    assert app.repo.definition('guidance') == (None, None)
    assert not app.capability_runtime.prepared
    with pytest.raises(ValueError, match='PREPARATION_REQUIRED'):
        app.invoke(args)
    boundary, result = execute(args)
    assert len(boundary.contracts) == 1 and not boundary.observation_only
    assert result.data['committed'], result.formatted


def test_revision_new_call_missing_component_has_precise_diagnostic_without_writes(runtime):
    app, *_ = runtime
    _, execute = pipeline_tool(runtime)
    _, applied = execute({'operation': 'apply', 'declaration': declaration()})
    assert not applied.is_error
    before = (deepcopy(app.repo.definition('guidance')), deepcopy(app.repo.list_instances(app.task)))
    _, result = execute({'operation': 'preview', 'definition_id': 'guidance',
        'revision': {'calls': [{'id': 'new_call', 'parameters': {'content': 'New guidance'}}]}})
    assert result.is_error and 'CAPABILITY_REVISION_NEW_CALL_REQUIRES_USE: new_call' in result.formatted
    assert 'existing call ids: note' in result.formatted
    assert 'MECHANISM_ELEMENT_UNAVAILABLE: None' not in result.formatted
    assert (app.repo.definition('guidance'), app.repo.list_instances(app.task)) == before


@pytest.mark.parametrize('failure,diagnostic', [
    ('legacy_fields', 'unsupported fields: quote, source_refs'),
    ('declaration_source', 'declaration: unsupported fields: source_refs'),
    ('missing_input', 'CAPABILITY_INPUT_UNKNOWN'),
    ('missing_declaration', 'required fields'),
])
def test_invalid_requests_report_exact_diagnostics_through_the_whole_pipeline(runtime, failure, diagnostic):
    app, p, *_ = runtime
    _, execute = pipeline_tool(runtime)
    args = {'operation': 'apply', 'declaration': declaration()}
    if failure == 'legacy_fields':
        args.update(source_refs=list(source_receipts(p)), quote='copied legacy metadata')
    elif failure == 'declaration_source':
        args['declaration']['source_refs'] = list(source_receipts(p))
    elif failure == 'missing_input':
        args['declaration']['calls'][0]['inputs'] = {'nonexistent': 'lifecycle.event'}
    else:
        args.pop('declaration')
    boundary, result = execute(args)
    assert not boundary.contracts
    assert result.is_error and result.diagnostics['code'] == 'INVALID_TOOL_ARGUMENTS', result.formatted
    assert diagnostic in result.formatted, result.formatted
    if failure == 'missing_input':
        assert "call 'note' (context.project@" in result.formatted
        assert 'unknown inputs: nonexistent' in result.formatted
        assert 'available inputs: go:trigger' in result.formatted
        assert 'component configuration belongs in parameters' in result.formatted
    assert 'CAPABILITY_PREPARATION_REQUIRED' not in result.formatted
    assert not app.repo.list_instances(app.task) and not app.repo.scan('definition/')
    assert not p.approval_grants


def test_recheck_preserves_reviewed_subject_and_rejects_concurrent_change(runtime):
    app, p, *_ = runtime
    args = {'operation': 'apply', 'declaration': declaration()}
    subject = app.capability_runtime.prepare(args)
    assert app.capability_runtime.prepare(args) == subject
    other = Application(p)
    changed = declaration()
    changed['intent'] = 'A genuinely different competing proposal'
    apply(other, changed)
    with pytest.raises(ValueError, match='PREPARED_STATE_CHANGED'):
        app.capability_runtime.prepare(args)
    with pytest.raises(ValueError, match='PREPARATION_REQUIRED'):
        app.invoke(args)
    assert app.repo.definition('guidance')[0]['revision'] == 1


def test_semantic_admission_is_required_for_runtime_control_despite_read_transport(runtime):
    app, p, *_ = runtime
    _, execute = pipeline_tool(runtime)
    from backend.core.loop.alignment_routing import uses_on_demand_review, REVIEW_POLICY
    # No fake ready review: install the actual unready routing state and verify
    # that the Pipeline cannot deploy merely because its transport is READ.
    p.context_snapshot['alignment_runtime'] = {'task_id': app.task, 'process_id': p.process_id,
        'admission_review': {'status': 'required'}, 'review_policy': REVIEW_POLICY}
    state = p.context_snapshot['alignment_runtime']
    if not uses_on_demand_review(state):
        pytest.fail('Fixture must enable the real on-demand review contract')
    _, result = execute({'operation': 'apply', 'declaration': declaration()})
    assert result.is_error and result.diagnostics['code'] == 'ALIGNMENT_REVIEW_REQUIRED', result.formatted
    assert app.repo.definition('guidance') == (None, None)


def test_real_admission_record_binds_typed_subject_and_rejects_another(runtime):
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    from backend.core.loop.alignment_routing import REVIEW_POLICY
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    from backend.core.loop.tool_pipeline import ToolPipeline
    app, p, _, _, ctx, *_ = runtime
    tool, _ = pipeline_tool(runtime)
    args = {'operation': 'apply', 'declaration': declaration()}
    boundary = preview_tool_boundaries([{'name': tool.name, 'args': args}], {tool.name: tool}, p)
    reviewer = AlignmentRuntime(p)
    payload = reviewer.payload('Fixture review of the proposed mechanism', proposed_contracts=boundary.contracts)
    p.context_snapshot['alignment_runtime']['review_policy'] = REVIEW_POLICY
    # Unit stub for the semantic judgment; the admission record is produced
    # by the actual service. This is not real-model or human evaluation.
    reviewer.apply({'status': 'ready', 'questions': [], 'interpretations': []}, payload)
    assert p.context_snapshot['alignment_runtime']['review_policy'] == REVIEW_POLICY
    other = deepcopy(args)
    other['declaration']['calls'][0]['parameters']['content'] = 'Unreviewed replacement'
    rejected = ToolPipeline().execute({'name': tool.name, 'args': other}, tool, ctx, 'unreviewed', 0)
    assert rejected.is_error and rejected.diagnostics['code'] == 'ALIGNMENT_REVIEW_REQUIRED', rejected.formatted
    accepted = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'reviewed', 0)
    assert not accepted.is_error and accepted.data['committed'], accepted.formatted
    assert app.repo.definition('guidance')[0]['declaration'] == declaration()


def test_same_process_retry_of_committed_request_reads_receipt(runtime):
    app, *_ = runtime
    initial = apply(app)
    repeated = apply(app)
    assert repeated['replayed'] and repeated['instance']['epoch'] == initial['instance']['epoch']
    assert app.repo.definition('guidance')[0]['revision'] == 1


def apply(app, value=None):
    args = {'operation': 'apply', 'declaration': value or declaration()}
    app.capability_runtime.prepare(args)
    return app.invoke(args)


def test_one_pipeline_call_publishes_activates_and_projects_without_model_bookkeeping(runtime):
    app, p, _, _, ctx, _, store = runtime
    from backend.core.loop.protocol_tools.harness_composition import install
    from backend.core.loop.tool_pipeline import ToolPipeline
    bindings = SimpleNamespace(process=p, llm_provider=None, on_stream_event=None, tools={}, manager=None, dispatcher=None)
    install(bindings)
    args = {'operation': 'apply', 'declaration': declaration()}
    tool = bindings.tools['harness_mechanism']
    from backend.core.loop.tool_boundary_preview import preview_tool_boundaries
    boundary = preview_tool_boundaries([{'name': tool.name, 'args': args}], bindings.tools, p)
    assert boundary.contracts[0]['kind'] == 'harness_capability_deployment'
    assert app.repo.definition('guidance') == (None, None)
    result = ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'capability-apply', 0)
    assert not result.is_error, result.formatted
    assert result.data['committed'] and result.data['instance']['phase'] == 'ready'
    assert not result.data['authority_granted'] and not p.approval_grants
    definition, _ = app.repo.definition('guidance')
    assert definition['compiled']['source_refs'] == list(source_receipts(p))
    app.dispatch('before_context', 'actual-boundary', {})
    assert app.context_sections()['harness.projections']['guidance']['note']['content'] == 'Preserve the public interface.'


def test_registered_component_composes_and_named_revision_keeps_other_behavior(runtime):
    from backend.core.mechanism_runtime.contracts import Element, Port
    from backend.core.mechanism_runtime.runner import NodeResult
    app, process, *_ = runtime
    callbacks = []

    def threshold(context, parameters, inputs):
        callbacks.append((context.epoch, inputs["event"]["observed"]))
        return NodeResult({"matched": inputs["event"]["observed"] >= parameters["minimum"]},
                          state={"count": context.state.get("count", 0) + 1})

    app.registry.register(Element("fixture.threshold", 1,
        {"type": "object", "properties": {"minimum": {"type": "integer"}},
         "required": ["minimum"], "additionalProperties": False},
        (Port("go", "trigger"), Port("event", "record")), (Port("matched", "boolean"),),
        ("before_context",), threshold))
    _, execute = pipeline_tool(runtime)
    value = {"id": "threshold", "intent": "Project an observation only above the threshold",
        "on": ["before_context"], "calls": [
            {"id": "measure", "use": "fixture.threshold", "parameters": {"minimum": 5}},
            {"id": "matching", "use": "context.project", "parameters": {"content": "Threshold reached"},
             "when": "measure.matched"},
            {"id": "independent", "use": "context.project", "parameters": {"content": "Independent context"}}]}
    _, preview = execute({"operation": "preview", "declaration": value})
    assert not preview.is_error and not callbacks
    _, applied = execute({"operation": "apply", "declaration": value})
    assert not applied.is_error, applied.formatted
    assert applied.data["instance"]["phase"] == "ready" and not callbacks
    app.dispatch("before_context", "fixture-observation-1", {"observed": 7})
    before = app.context_sections()["harness.projections"]["threshold"]
    assert set(before) == {"matching", "independent"}
    previous, _ = app.repo.definition("threshold")

    process.session.append_user("The threshold is now nine. Keep the independent context.")
    _, revised = execute({"operation": "revise", "definition_id": "threshold",
        "revision": {"calls": [{"id": "measure", "parameters": {"minimum": 9}}]}})
    assert not revised.is_error, revised.formatted
    after, _ = app.repo.definition("threshold")
    assert [n for n in previous["compiled"]["elements"] if n["id"] != "measure"] == [
        n for n in after["compiled"]["elements"] if n["id"] != "measure"]
    assert len(callbacks) == 1
    app.dispatch("before_context", "fixture-observation-2", {"observed": 7})
    assert len(callbacks) == 2 and callbacks[0][0] != callbacks[1][0]
    assert set(app.context_sections()["harness.projections"]["threshold"]) == {"independent"}
    assert app.repo.list_instances(app.task)[0]["states"]["measure"] == {"count": 2}


def test_source_change_after_preparation_cannot_commit_unreviewed_meaning(runtime):
    app, p, *_ = runtime
    args = {'operation': 'apply', 'declaration': declaration()}
    app.capability_runtime.prepare(args)
    p.session.append_user('Stop; this requirement changed after the preparation.')
    with pytest.raises(ValueError, match='SOURCE_OR_CONTRACT_CHANGED'):
        app.invoke(args)
    assert app.repo.definition('guidance') == (None, None)
    assert app.repo.list_instances(app.task) == []


def test_parallel_semantic_revision_never_overwrites_other_author(runtime):
    app, p, *_ = runtime
    apply(app)
    other = Application(p)
    first = {'operation': 'revise', 'definition_id': 'guidance', 'revision': {
        'calls': [{'id': 'note', 'parameters': {'content': 'First requested change'}}]}}
    second = deepcopy(first)
    second['revision']['calls'][0]['parameters']['content'] = 'Other requested change'
    app.capability_runtime.prepare(first)
    other.capability_runtime.prepare(second)
    app.invoke(first)
    with pytest.raises(ValueError, match='DEFINITION_CHANGED'):
        other.invoke(second)
    assert app.repo.definition('guidance')[0]['revision'] == 2
    assert app.repo.definition('guidance')[0]['declaration']['calls'][0]['parameters']['content'] == 'First requested change'


def test_failed_middle_write_rolls_back_definition_instance_and_receipt(runtime):
    app, p, _, _, _, _, store = runtime
    # An explicit test fault in the official storage transaction, not a
    # hand-constructed product state or approval.
    store._storage._state.execute("CREATE TEMP TRIGGER capability_fault BEFORE INSERT ON storage_kv "
        "WHEN NEW.key LIKE 'capability_operation/%' BEGIN SELECT RAISE(ABORT,'injected publication fault'); END")
    args = {'operation': 'apply', 'declaration': declaration()}
    app.capability_runtime.prepare(args)
    with pytest.raises(Exception, match='injected publication fault'):
        app.invoke(args)
    assert app.repo.definition('guidance') == (None, None)
    assert app.repo.definition('guidance', 1) == (None, None)
    assert app.repo.list_instances(app.task) == []
    assert app.repo.scan('capability_operation/') == []


def test_lost_reply_is_reconciled_by_committed_receipt_without_republication(runtime, monkeypatch):
    app, p, _, _, _, _, store = runtime
    original = store._storage.commit_state_batch
    def lost_reply(namespace, updates):
        assert original(namespace, updates)
        raise RuntimeError('injected lost reply after real commit')
    monkeypatch.setattr(store._storage, 'commit_state_batch', lost_reply)
    args = {'operation': 'apply', 'declaration': declaration()}
    app.capability_runtime.prepare(args)
    with pytest.raises(RuntimeError, match='lost reply'):
        app.invoke(args)
    monkeypatch.setattr(store._storage, 'commit_state_batch', original)
    restored = Application(p)
    restored.capability_runtime.prepare(args)
    result = restored.invoke(args)
    assert result['replayed'] and result['committed'] and result['instance']['phase'] == 'ready'
    assert restored.repo.definition('guidance')[0]['revision'] == 1
    epoch = result['instance']['epoch']
    restored.capability_runtime.prepare(args)
    assert restored.invoke(args)['instance']['epoch'] == epoch


def test_mechanical_callback_conflict_retries_without_losing_state(runtime, monkeypatch):
    app, p, _, _, _, _, store = runtime
    apply(app)
    original = store._storage.commit_state_batch
    attempts = []
    def concurrent_progress(namespace, updates):
        attempts.append(1)
        if len(attempts) == 1:
            app.repo.mutate(app.task, 'guidance', lambda value: value.update(return_counters={'stable-return': 2}))
        return original(namespace, updates)
    monkeypatch.setattr(store._storage, 'commit_state_batch', concurrent_progress)
    args = {'operation': 'revise', 'definition_id': 'guidance', 'revision': {
        'calls': [{'id': 'note', 'parameters': {'content': 'Changed guidance'}}]}}
    app.capability_runtime.prepare(args)
    assert app.invoke(args)['committed']
    assert len(attempts) == 2
    assert app.repo.instance(app.task, 'guidance')[0]['return_counters'] == {'stable-return': 2}
    assert app.repo.definition('guidance')[0]['revision'] == 2


def test_authoring_inactive_instance_never_resumes_it(runtime):
    app, p, *_ = runtime
    apply(app)
    current, _ = app.repo.instance(app.task, 'guidance')
    app.invoke({'operation': 'suspend', 'instance_id': 'guidance', 'expected_revision': current['revision'],
        'source_refs': list(source_receipts(p))})
    args = {'operation': 'revise', 'definition_id': 'guidance', 'revision': {
        'calls': [{'id': 'note', 'parameters': {'content': 'Keep inactive after this edit'}}]}}
    app.capability_runtime.prepare(args)
    result = app.invoke(args)
    assert result['instance']['phase'] == 'suspended'
    assert result['definition_revision'] == 2


@pytest.mark.parametrize('kind', ['source', 'binding', 'cycle', 'unknown_input'])
def test_bad_declarations_never_publish(runtime, kind):
    app, *_ = runtime
    args = {'operation': 'apply', 'declaration': declaration()}
    call = args['declaration']['calls'][0]
    if kind == 'source':
        args['source_refs'] = ['fabricated:user']
    elif kind == 'binding':
        call['when'] = 'note.projected'  # trigger is not a boolean predicate
    elif kind == 'cycle':
        call['after'] = 'note.projected'
    else:
        call['inputs'] = {'does_not_exist': 'lifecycle.event'}
    with pytest.raises(ValueError):
        app.capability_runtime.prepare(args)
    assert app.repo.definition('guidance') == (None, None)


def test_readonly_actor_and_unprepared_calls_cannot_mutate(runtime):
    app, p, *_ = runtime
    args = {'operation': 'apply', 'declaration': declaration()}
    with pytest.raises(ValueError, match='PREPARATION_REQUIRED'):
        app.invoke(args)
    app.capability_runtime.prepare(args)
    p.capability_profile_id = 'text.only'
    with pytest.raises(ValueError, match='CAPABILITY_REQUIRED'):
        app.invoke(args)
    assert app.repo.list_instances(app.task) == []


def test_saved_capability_reuse_has_no_new_definition_or_creation_steps(runtime):
    app, p, *_ = runtime
    apply(app)
    p.session.append_user('Reuse the saved capability in another instance.')
    args = {'operation': 'use', 'definition_id': 'guidance', 'instance_id': 'reused'}
    app.capability_runtime.prepare(args)
    result = app.invoke(args)
    assert result['committed'] and result['instance']['phase'] == 'ready'
    assert result['definition_revision'] == 1
    assert len(app.repo.list_instances(app.task)) == 2


def test_new_registered_component_needs_no_case_in_declaration_compiler(runtime, request):
    app, p, *_ = runtime
    from backend.core.mechanism_runtime.contracts import Element, Port
    from backend.core.mechanism_runtime.runner import NodeResult
    def observe(ctx, params, inputs):
        return NodeResult({}, {'seen': params['label']})
    unregister = app.registry.register(Element('fixture.observer', 1,
        {'type': 'object', 'properties': {'label': {'type': 'string'}}, 'required': ['label'], 'additionalProperties': False},
        (Port('go', 'trigger'), Port('event', 'record')), (), ('before_context',), observe))
    import asyncio
    request.addfinalizer(lambda: asyncio.run(unregister()))
    value = declaration()
    value['calls'] = [{'id': 'observer', 'use': 'fixture.observer', 'parameters': {'label': 'extension value'}}]
    apply(app, value)
    app.dispatch('before_context', 'extension-event', {})
    assert app.repo.instance(app.task, 'guidance')[0]['states']['observer']['seen'] == 'extension value'


def test_running_callback_finishes_old_generation_then_new_call_uses_new_generation(runtime):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    app, p, *_ = runtime
    apply(app)
    entered, release = threading.Event(), threading.Event()
    original = app.services['context.project']
    observed = []
    def in_flight(context, args):
        observed.append(args['content'])
        if len(observed) == 1:
            entered.set()
            assert release.wait(5), 'test driver failed to release actual callback'
        return original(context, args)
    app.services['context.project'] = in_flight
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(app.dispatch, 'before_context', 'old-generation', {})
        try:
            assert entered.wait(5)
            revision = {'operation': 'revise', 'definition_id': 'guidance', 'revision': {
                'calls': [{'id': 'note', 'parameters': {'content': 'New generation guidance'}}]}}
            app.capability_runtime.prepare(revision)
            result = app.invoke(revision)
            assert result['instance']['phase'] == 'pending_activation'
            assert result['instance']['definition_revision'] == 1
        finally:
            release.set()
        pending.result(timeout=5)
    app.dispatch('before_context', 'new-generation', {})
    assert observed == ['Preserve the public interface.', 'New generation guidance']
    assert app.repo.instance(app.task, 'guidance')[0]['definition_revision'] == 2


def test_state_batch_conflict_leaves_every_head_and_revision_untouched(runtime):
    app, *_ = runtime
    storage = app.repo.storage
    first = storage.put_blob(b'first')
    second = storage.put_blob(b'second')
    storage.compare_state_ref('fixture', 'a', first, expected_ref=None)
    storage.compare_state_ref('fixture', 'b', first, expected_ref=None)
    assert not storage.commit_state_batch('fixture', [
        {'key': 'a', 'value_ref': second, 'expected_ref': first, 'revision_key': 'a/revision/2'},
        {'key': 'b', 'value_ref': second, 'expected_ref': None}])
    assert storage.get_state_ref('fixture', 'a') == first
    assert storage.get_state_ref('fixture', 'b') == first
    assert storage.get_state_ref('fixture', 'a/revision/2') is None


def test_later_suspend_cannot_be_overwritten_by_prepared_revision(runtime):
    app, p, *_ = runtime
    apply(app)
    args = {'operation': 'revise', 'definition_id': 'guidance', 'revision': {'intent': 'Changed guidance intent'}}
    app.capability_runtime.prepare(args)
    current, _ = app.repo.instance(app.task, 'guidance')
    app.invoke({'operation': 'suspend', 'instance_id': 'guidance', 'expected_revision': current['revision'],
        'source_refs': list(source_receipts(p))})
    with pytest.raises(ValueError, match='INSTANCE_CHANGED'):
        app.invoke(args)
    assert app.repo.instance(app.task, 'guidance')[0]['phase'] == 'suspended'
    assert app.repo.definition('guidance')[0]['revision'] == 1
