"""Semantic reviewers receive exact registered contracts, not guessed names."""
from types import SimpleNamespace
from copy import deepcopy
import json
from tests.test_mechanism_interactions import runtime, checkpoint_program, enable


def test_review_evidence_prefix_keeps_every_fact_and_current_generation():
    from backend.core.mechanism_runtime.reviews import review_description
    context = SimpleNamespace(task_id='owner', instance_id='policy', epoch=2, event_id='attempt-a')
    payload = {'instructions': 'Read the result against the original signature.',
        'event': {'candidate': 'The current result', 'receipts': [{'receipt_id': 'actual-write',
            'task_id': 'owner', 'committed': True, 'after_sha256': 'actual-content-digest'}]}}
    first = review_description(context, payload)
    context.event_id = 'attempt-b'
    context.epoch = 3
    payload['event']['candidate'] = 'A new completion candidate'
    second = review_description(context, payload)
    # Reuse ends before the changed candidate, preserving both the current
    # candidate and epoch. No duplicate filler or omission earns that prefix.
    assert first.split(',"candidate":')[0] == second.split(',"candidate":')[0]
    restored = json.loads(second.split('\n\n', 1)[1])
    assert restored == {'instructions': payload['instructions'], 'receipts': payload['event']['receipts'],
        'candidate': 'A new completion candidate', 'task_id': 'owner',
        'instance_id': 'policy', 'epoch': 3, 'event_id': 'attempt-b'}
    assert payload['event']['receipts'][0]['receipt_id'] == 'actual-write'
    payload['instructions'] = 'Updated review criteria; do not change the public interface.'
    third = review_description(context, payload)
    assert third.split('\n\n', 1)[0] == second.split('\n\n', 1)[0]
    assert third.split(',"candidate":')[0] != second.split(',"candidate":')[0]
    assert json.loads(third.split('\n\n', 1)[1]) == {**restored, 'instructions': payload['instructions']}


def test_lifecycle_review_observes_saved_definition_without_claiming_activation(runtime):
    from backend.core.loop.task_maintenance import source_receipts
    app, process, *_ = runtime
    raw = checkpoint_program(process)
    preview = app.preview({'program': raw})
    saved = app.invoke({'operation': 'publish', 'program': raw,
        'expected_head': preview['expected_head'], 'preview_digest': preview['preview_digest']})
    proposal = app.lifecycle_preview({'operation': 'instantiate', 'definition_id': raw['id'],
        'version_policy': 'pinned', 'source_refs': list(source_receipts(process))})
    fact = proposal['target_publication']
    assert fact['reference'] == saved['reference']
    assert fact['revision'] == saved['definition']['revision']
    assert fact['program_digest'] == saved['definition']['compiled']['digest']
    assert fact['state'] == 'published'
    assert proposal['current'] is None and proposal['authority_granted'] is False
    assert app.repo.list_instances(app.task) == []


def test_semantic_preview_carries_exact_component_contracts_without_changing_graph(runtime):
    app,p,*_=runtime
    raw=checkpoint_program(p)
    preview=app.semantic_preview({'program':raw})
    contracts=preview['component_contracts']
    assert not contracts['authority_granted'] and not contracts['partial']
    compare=next(x for x in contracts['items'] if x['id']=='interaction.compare')
    node=next(x for x in app.preview({'program':raw})['compiled']['elements'] if x['use']=='interaction.compare')
    assert compare['digest']==node['contract_digest'] and compare['version']==node['version']
    assert compare['inputs']==node['interface']['inputs'] and compare['effect']=='projection'
    assert 'no projection' in compare['description'] and 'answered' in compare['description']
    assert not app.repo.list_instances(app.task)


def test_old_comparison_contract_remains_available_while_new_descriptor_is_explicit(runtime):
    app,p,*_=runtime
    entries={e['version']:e for e in app.registry.catalog()['elements'] if e['id']=='interaction.compare'}
    assert 1 in entries and 2 in entries
    assert entries[1]['inputs']==entries[2]['inputs']
    assert entries[1]['implementation']==entries[2]['implementation']
    raw=checkpoint_program(p)
    next(n for n in raw['elements'] if n['use']=='interaction.compare')['version']=1
    contract=next(x for x in app.semantic_preview({'program':raw})['component_contracts']['items'] if x['id']=='interaction.compare')
    assert contract['version']==1 and contract['digest']==entries[1]['digest']


def test_unmatched_after_event_does_not_create_a_prediction_or_projection(runtime):
    app,p,*_=runtime
    instance=enable(app,p,checkpoint_program(p))
    before=deepcopy(app.repo.instance(app.task,instance['id'])[0])
    ledger=deepcopy(p.session.host_ledger)
    ctx=SimpleNamespace(task_id=app.task,instance_id=instance['id'])
    assert app.interactions.compare(ctx,{'unit_id':'never-predicted','tool_name':'unrelated-operation'})=={'available':False}
    assert app.repo.instance(app.task,instance['id'])[0]==before
    assert p.session.host_ledger==ledger and not p.pending_decision and not p.approval_grants
