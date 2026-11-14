"""Contract correction text reaches the model without changing refusal authority."""
from copy import deepcopy

import pytest

from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import RingLevel
from backend.core.loop.task_contract import get_task_contract
from backend.core.loop.task_maintenance import add_change, state_for
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from tests.test_task_contract_activation import isolated_pipeline_runtime, revision_tool


ORIGINAL = ['Preserve raw total', 'Preserve original record count']
ADDITION = 'Subtract posted refunds and exclude cancelled entries'


def plan():
    return {'summary': 'Reconcile the current supplied ledger requirements', 'steps': [{
        'id': 'compute', 'goal': 'Keep original fields and compute accepted refund totals',
        'verification': 'Compare each result with supplied records'}]}


def declaration(process):
    return {'operation': 'declare', 'goal': 'Summarize the supplied ledger', 'execution_mode': 'answer',
        'delegation_required': False, 'deliverables': [{'kind': 'response', 'required': True}],
        'acceptance_criteria': ORIGINAL,
        'maintenance': {'expected_revision': state_for(process).get('revision', 0), 'plan': plan(),
            'related_review': {'summary': 'Only the supplied ledger is affected', 'candidates': []}}}


def revision(process, change):
    return {'operation': 'revise', 'expected_revision': get_task_contract(process)['revision'],
        'append': {'acceptance_criteria': [ADDITION]},
        'maintenance': {'expected_revision': state_for(process)['revision'], 'plan': plan(),
            'related_review': {'summary': 'The ledger change remains in the current task', 'candidates': []},
            'resolve_changes': [{'id': change['id'], 'reason': 'Keep old totals and add refund treatment',
                                 'contract_fields': ['acceptance_criteria']}]}}


@pytest.fixture
def actors(isolated_pipeline_runtime):
    manager, parent, store, workspace = isolated_pipeline_runtime
    parent.session.append_user('Calculate the supplied ledger and retain original totals.')
    parent_tool = revision_tool(parent, workspace)
    assert parent_tool.execute(declaration(parent)).get('accepted')
    child = manager.fork(parent.process_id, 'worker', ToolRegistry(['task_mechanism']), 12,
        RingLevel.RING_3, workspace_path=str(workspace), task_id='child-guidance', actor_kind='worker',
        capability_profile_id='governance.observe', context_snapshot={'task_contract': {
            'goal': 'Summarize the supplied ledger', 'execution_mode': 'answer',
            'acceptance_criteria': ORIGINAL,
            'deliverables': [{'kind': 'response', 'path': '', 'required': True}],
            'allowed_tools': ['task_mechanism'], 'target_files': []}})
    parent.delegated_contracts[child.process_id] = {'required_for_parent_completion': True,
                                                  'acceptance_criteria': ORIGINAL}
    child_change = add_change(child, ADDITION, identifier='feedback-child-message', source_ref='user:child-message',
        expected_task_id=child.active_task_id, expected_contract_revision=0, store=store, user_adopted=False)
    parent_change = add_change(parent, 'Keep the original field labels', identifier='feedback-parent-message',
        source_ref='user:parent-message', expected_task_id=parent.active_task_id,
        expected_contract_revision=1, store=store, user_adopted=False)
    return parent, child, store, workspace, parent_tool, revision_tool(child, workspace), parent_change, child_change


def snapshot(parent, child, store):
    def one(process):
        persisted = store.load_process_state(process.process_id)
        return {'context': deepcopy(process.context_snapshot),
                'persisted_context': deepcopy(persisted['runtime_state']['context_snapshot']) if persisted else None}
    return {'parent': one(parent), 'child': one(child)}


def execute(process, store, workspace, tool, args, label):
    context = ExecutionContext(process, process.session, str(workspace), storage=store._storage)
    return ToolPipeline().execute({'name': tool.name, 'args': args}, tool, context, 'guidance-' + label)


@pytest.mark.parametrize(('fault', 'prefix', 'required_text'), [
    ('revision-zero', 'TASK_CONTRACT_INITIAL_DECLARATION_REQUIRED',
        ('declare', 'revise', 'contract.acceptance_criteria', 'contract.deliverables')),
    ('wrong-own-change', 'change resolution must identify',
        ('state.changes', 'pending', 'maintenance.resolve_changes', 'message_id')),
    ('parent-uses-child-change', 'change resolution must identify',
        ('state.changes', 'owned_scope_updates', 'state.maintenance_items', 'child_scope', 'maintenance.resolve_maintenance')),
    ('missing-plan', 'adoption requires a revised plan',
        ('maintenance.plan', 'acceptance_criteria', 'reason', 'plan_review')),
    ('drop-assigned-criteria', 'worker revisions cannot remove supervisor-assigned acceptance obligations',
        ('contract.acceptance_criteria', 'append.acceptance_criteria')),
    ('drop-required-deliverable', 'worker revisions cannot remove supervisor-assigned deliverables',
        ('contract.deliverables', 'append.deliverables')),
    ('wrong-maintenance-id', 'maintenance resolution requires an actual pending trigger id',
        ('state.maintenance_items', 'pending', 'maintenance.resolve_maintenance', 'change')),
])
def test_rejected_contract_keeps_state_and_gives_current_task_correction(actors, fault, prefix, required_text):
    parent, child, store, workspace, parent_tool, child_tool, parent_change, child_change = actors
    if fault != 'revision-zero':
        assert child_tool.execute(declaration(child)).get('accepted')
    target, tool = child, child_tool
    if fault == 'revision-zero':
        args = {'operation': 'revise', 'append': {'acceptance_criteria': [ADDITION]}}
    else:
        args = revision(child, child_change)
    if fault == 'wrong-own-change': args['maintenance']['resolve_changes'][0]['id'] = 'child-message'
    elif fault == 'parent-uses-child-change':
        target, tool = parent, parent_tool
        args = revision(parent, parent_change)
        args['maintenance']['resolve_changes'][0]['id'] = child_change['id']
    elif fault == 'missing-plan': args['maintenance'].pop('plan')
    elif fault == 'drop-assigned-criteria':
        args.pop('append'); args['acceptance_criteria'] = [ADDITION]
    elif fault == 'drop-required-deliverable': args['deliverables'] = []
    elif fault == 'wrong-maintenance-id':
        target, tool = parent, parent_tool
        args = revision(parent, parent_change)
        args['maintenance']['resolve_maintenance'] = [child_change['id']]
    before = snapshot(parent, child, store)
    # Direct service callers retain the original error code and noncommit state.
    direct = tool.execute(deepcopy(args))
    expected_direct_code = ('INVALID_TASK_CONTRACT' if fault in {
        'revision-zero', 'drop-assigned-criteria', 'drop-required-deliverable'} else 'INVALID_TASK_ROUTING')
    assert direct['error'] == expected_direct_code and direct['effect_state'] == 'not_committed'
    assert prefix in direct['detail']
    assert snapshot(parent, child, store) == before
    # The actual model surface is Pipeline.formatted, not the raw Python error.
    result = execute(target, store, workspace, tool, args, fault)
    assert result.is_error and result.diagnostics['code'] == 'INVALID_TOOL_ARGUMENTS', result.formatted
    assert result.diagnostics['nature'] == 'business'
    assert result.receipt['effect_state'] == 'not_committed'
    assert result.diagnostics['next_actions'] == []
    assert snapshot(parent, child, store) == before
    assert prefix in result.formatted
    for text in ('task_mechanism', 'status', *required_text):
        assert text in result.formatted, (fault, text, result.formatted)


def test_unknown_value_error_text_does_not_gain_contract_specific_actions(actors, monkeypatch):
    parent, child, store, workspace, _parent_tool, tool, _parent_change, _child_change = actors
    before = snapshot(parent, child, store)
    message = 'Fixture-only ValueError: change resolution must identify this task pending adopted request'
    def unknown(_args): raise ValueError(message)
    monkeypatch.setattr(tool, 'prepare_args', unknown)
    result = execute(child, store, workspace, tool, declaration(child), 'unknown')
    assert result.is_error and result.diagnostics['code'] == 'INVALID_TOOL_ARGUMENTS'
    assert result.diagnostics['nature'] == 'business'
    assert result.diagnostics['next_actions'] == []
    assert message in result.formatted
    assert 'task_mechanism' not in result.formatted
    assert 'maintenance.resolve_changes' not in result.formatted
    assert snapshot(parent, child, store) == before


def test_adopted_change_withdrawal_missing_plan_keeps_history_and_explains_lookup(actors):
    parent, child, store, workspace, tool, _child_tool, change, _child_change = actors
    assert tool.execute(revision(parent, change)).get('accepted')
    assert state_for(parent)['changes'][change['id']]['status'] == 'adopted'
    args = revision(parent, change)
    args['maintenance'].pop('plan')
    args['maintenance']['resolve_changes'][0]['disposition'] = 'withdraw'
    before = snapshot(parent, child, store)
    result = execute(parent, store, workspace, tool, args, 'withdraw-missing-plan')
    assert result.is_error and result.diagnostics['code'] == 'INVALID_TOOL_ARGUMENTS'
    assert result.diagnostics['nature'] == 'business'
    assert result.receipt['effect_state'] == 'not_committed'
    assert result.diagnostics['next_actions'] == []
    assert snapshot(parent, child, store) == before
    for text in ('adoption requires a revised plan', 'task_mechanism', 'status',
                 'full task state', 'adopted', 'withdrawing', 'maintenance.plan',
                 'maintenance.resolve_changes'):
        assert text in result.formatted
    assert 'pending state.changes' not in result.formatted
