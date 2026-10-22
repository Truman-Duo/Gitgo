"""End-to-end state boundaries for published evidence and failed-worker continuation."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from backend.core.loop.worktree import AgentWorktreeManager, WorktreeLease, WorktreeError
from backend.core.loop.models import ProcessStatus
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.delivery_artifacts import collect_artifacts, artifact_manifest, verify_manifest
from backend.core.loop.budget import TaskBudgetExceeded
from backend.core.loop.executor import _build_internal_tools
from backend.core.storage import StorageRuntime
from .test_agent_dag_worktree import _repo, _git
from .test_task_contract_recovery import _supervisor


def setup(tmp):
    repo = _repo(tmp)
    storage = StorageRuntime(repo, state_home=tmp/'state')
    root, _ = _supervisor(repo)
    manager = root._manager
    manager.worktree_manager = AgentWorktreeManager(repo, storage)
    tools = _build_internal_tools(root, {}, str(repo), object(), SimpleNamespace(_executors={}))
    return repo, storage, root, manager, tools


def delegate(tools, **kwargs):
    return tools['delegate_task']({'task_description': 'Finish the retained implementation',
        'target_files': ['app.txt', 'new.txt'], 'acceptance_criteria': ['both changes delivered'], **kwargs})


def cleanup(manager, storage):
    for child in list(manager._processes.values()):
        if child.worktree.get('path'):
            manager.dispose_process_worktree(child)
    storage.close()


def record_promotion(root, result):
    receipt = {'receipt_id': 'real-promotion', 'task_id': root.active_task_id,
        'succeeded': True, 'committed': True, 'effect_state': 'committed', 'effect': 'workspace_write'}
    from backend.core.loop.execution_contract import data_broker
    ToolPipeline._attach_result_facts(receipt, 'promote_agent_changes', result,
        execution_contract=data_broker('files.workspace.promote_agent_changes'))
    root.tool_receipts.append(receipt)
    root.session.append_tool_result('Promoted', tool_name='promote_agent_changes', data=result, receipt=receipt)
    return receipt


@pytest.mark.parametrize('tamper', ['', 'changed', 'old_task', 'failed', 'scope', 'message_only'])
def test_promotion_flows_through_real_receipt_and_current_artifact_gate(tmp_path_factory, tamper):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'):
            response = delegate(tools)
        child = manager.get(response['process_id'])
        path = Path(manager.materialize_process_worktree(child))
        (path/'app.txt').write_text('updated\n', encoding='utf-8')
        (path/'new.txt').write_text('new output\n', encoding='utf-8')
        child.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(child)
        root.child_reviews[child.process_id] = {'verdict': 'approved'}
        result = manager.promote_process_results(root, [child.process_id])
        assert result['promoted']
        receipt = record_promotion(root, result)
        assert set(receipt['files']) == {'app.txt', 'new.txt'}
        artifacts, notices = collect_artifacts(root, ['app.txt', 'new.txt'])
        assert not notices and len(artifacts) == 2
        manifest = artifact_manifest(artifacts)
        if tamper == 'changed': (repo/'app.txt').write_text('later change', encoding='utf-8')
        elif tamper == 'old_task': root.active_task_id = 'another-task'
        elif tamper == 'failed': receipt['succeeded'] = False
        elif tamper == 'scope': receipt['files'] = ['new.txt']
        elif tamper == 'message_only': root.tool_receipts.clear()
        assert verify_manifest(root, manifest, ['app.txt', 'new.txt']) is (not tamper)
        _, arbitrary = collect_artifacts(root, ['../outside.txt', 'not-published.txt'])
        assert len(arbitrary) == 2
    finally:
        cleanup(manager, storage)


@pytest.mark.parametrize('committed_partial', [False, True])
def test_escrow_failure_continues_partial_work_then_seals_full_delta(tmp_path_factory, committed_partial):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'):
            first = delegate(tools, budget_request={'provider_calls': 1, 'output_tokens': 1024})
        old = manager.get(first['process_id'])
        source = Path(manager.materialize_process_worktree(old))
        (source/'app.txt').write_text('retained partial\n', encoding='utf-8')
        (source/'new.txt').write_text('retained new file\n', encoding='utf-8')
        if committed_partial:
            _git(source, 'add', '-A'); _git(source, 'commit', '-m', 'worker partial commit')
        old.session.append_user('preserve the selected implementation')
        budget = root.task_budget
        budget.begin_provider_call(old.process_id)
        with pytest.raises(TaskBudgetExceeded): budget.begin_provider_call(old.process_id)
        old.status = ProcessStatus.FAILED
        old.result = {'status': 'failed', 'code': 'TASK_TREE_CHILD_PROVIDER_ESCROW_EXHAUSTED'}
        budget.release_process(old.process_id, status='failed')
        captured = []
        with patch.object(manager, 'start', side_effect=lambda pid, target, **kw: captured.append(target)):
            second = delegate(tools, continue_process_id=old.process_id,
                              budget_request={'provider_calls': 2, 'output_tokens': 2048})
        assert second['delegated'], second
        new = manager.get(second['process_id'])
        assert new.session is old.session and new.active_task_id != old.active_task_id
        assert new.tool_receipts == [] and new.completion_claim is None
        assert root.delegated_contracts[old.process_id]['superseded_by'] == new.process_id
        def finish(**kwargs):
            target = Path(kwargs['workspace_path'])
            assert target != source and target != repo
            assert (target/'app.txt').read_text(encoding='utf-8') == 'retained partial\n'
            assert (target/'new.txt').read_text(encoding='utf-8') == 'retained new file\n'
            budget.begin_provider_call(new.process_id)
            (target/'new.txt').write_text('finished new file\n', encoding='utf-8')
            new.status = ProcessStatus.COMPLETED
            new.result = {'status': 'completed'}
            return new.result
        with patch('backend.core.loop.protocol_tools.child_execution._run_agent_step', side_effect=finish):
            outcome = captured[0]()
        assert outcome['status'] == 'completed', outcome
        assert new.worktree['state'] == 'sealed'
        assert new.worktree['resumed_from_process_id'] == old.process_id
        assert sorted(new.worktree['changed_files']) == ['app.txt', 'new.txt']
        assert old.result['code'] == 'TASK_TREE_CHILD_PROVIDER_ESCROW_EXHAUSTED'
        assert (repo/'app.txt').read_text(encoding='utf-8') == 'base\n'
        assert budget.snapshot()['used']['provider_calls'] == 2
        root.child_reviews[new.process_id] = {'verdict': 'approved'}
        promoted = manager.promote_process_results(root, [new.process_id])
        record_promotion(root, promoted)
        artifacts, notices = collect_artifacts(root, ['app.txt', 'new.txt'])
        assert len(artifacts) == 2 and not notices
        assert (repo/'app.txt').read_text(encoding='utf-8') == 'retained partial\n'
        assert (repo/'new.txt').read_text(encoding='utf-8') == 'finished new file\n'
        assert not delegate(tools, continue_process_id=old.process_id)['delegated']
    finally:
        cleanup(manager, storage)


@pytest.mark.parametrize('boundary', ['completed', 'active', 'narrow_scope', 'new_dependency', 'no_budget', 'missing'])
def test_continuation_cannot_relax_execution_or_workspace_authority(tmp_path_factory, boundary):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'):
            first = delegate(tools)
        old = manager.get(first['process_id'])
        source = Path(manager.materialize_process_worktree(old))
        (source/'app.txt').write_text('partial\n', encoding='utf-8')
        old.status = ProcessStatus.FAILED
        root.task_budget.release_process(old.process_id, status='failed')
        args = {'continue_process_id': old.process_id}
        if boundary == 'completed':
            old.status = ProcessStatus.COMPLETED
            manager.seal_process_worktree(old)
        elif boundary == 'active': old.status = ProcessStatus.RUNNING
        elif boundary == 'narrow_scope': args['target_files'] = ['app.txt']
        elif boundary == 'new_dependency':
            with patch.object(manager, 'start'): other = delegate(tools)
            args['depends_on'] = [other['process_id']]
        elif boundary == 'no_budget': args['budget_request'] = {'provider_calls': 100000}
        elif boundary == 'missing':
            manager.dispose_process_worktree(old)
        with patch.object(manager, 'start') as start:
            response = delegate(tools, **args)
        assert not response['delegated'], response
        start.assert_not_called()
        assert (repo/'app.txt').read_text(encoding='utf-8') == 'base\n'
    finally:
        cleanup(manager, storage)


@pytest.mark.parametrize('committed', [False, True])
def test_partial_restore_privacy_block_keeps_original_failure_and_retained_work(tmp_path_factory, committed):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'): first = delegate(tools)
        old = manager.get(first['process_id'])
        source = Path(manager.materialize_process_worktree(old))
        sensitive = 'sk-' + 'x' * 24
        (source/'app.txt').write_text(sensitive, encoding='utf-8')
        if committed:
            _git(source, 'add', '-A'); _git(source, 'commit', '-m', 'partial from worker')
        old.status = ProcessStatus.FAILED
        old.result = {'status': 'failed', 'code': 'TASK_TREE_CHILD_PROVIDER_ESCROW_EXHAUSTED'}
        root.task_budget.release_process(old.process_id, status='failed')
        captured = []
        with patch.object(manager, 'start', side_effect=lambda pid, target, **kw: captured.append(target)):
            second = delegate(tools, continue_process_id=old.process_id)
        new = manager.get(second['process_id'])
        with patch('backend.core.loop.protocol_tools.child_execution._run_agent_step') as model:
            outcome = captured[0]()
        model.assert_not_called()
        assert outcome['code'] == 'CHILD_LIFECYCLE_FAILED'
        assert 'privacy scan blocked' in outcome['error'] and sensitive not in outcome['error']
        assert new.worktree['state'] == 'failed'
        assert new.worktree['resumed_from_process_id'] == old.process_id
        assert (source/'app.txt').read_text(encoding='utf-8') == sensitive
        assert old.result['code'] == 'TASK_TREE_CHILD_PROVIDER_ESCROW_EXHAUSTED'
        assert not delegate(tools, continue_process_id=new.process_id)['delegated']
        assert (repo/'app.txt').read_text(encoding='utf-8') == 'base\n'
    finally:
        cleanup(manager, storage)


def test_resumed_promotion_still_rejects_concurrent_user_change(tmp_path_factory):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'): first = delegate(tools)
        old = manager.get(first['process_id'])
        source = Path(manager.materialize_process_worktree(old))
        (source/'app.txt').write_text('partial\n', encoding='utf-8')
        old.status = ProcessStatus.FAILED
        root.task_budget.release_process(old.process_id, status='failed')
        with patch.object(manager, 'start'): second = delegate(tools, continue_process_id=old.process_id)
        new = manager.get(second['process_id'])
        target = Path(manager.materialize_process_worktree(new, continue_process_id=old.process_id))
        assert (target/'app.txt').read_text(encoding='utf-8') == 'partial\n'
        new.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(new)
        root.child_reviews[new.process_id] = {'verdict': 'approved'}
        (repo/'app.txt').write_text('user concurrent edit\n', encoding='utf-8')
        _git(repo, 'add', 'app.txt')
        index_before = _git(repo, 'diff', '--cached')
        with pytest.raises(WorktreeError, match='workspace changed'):
            manager.promote_process_results(root, [new.process_id])
        assert (repo/'app.txt').read_text(encoding='utf-8') == 'user concurrent edit\n'
        assert _git(repo, 'diff', '--cached') == index_before
    finally:
        cleanup(manager, storage)


def test_promotion_reports_unsupported_evidence_and_handles_unicode_paths(tmp_path_factory):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'): first = delegate(tools, target_files=['*'])
        child = manager.get(first['process_id'])
        target = Path(manager.materialize_process_worktree(child))
        name = '报告 [结果].md'
        (target/name).write_text('verified text\n', encoding='utf-8')
        (target/'binary.dat').write_bytes(b'\0data')
        (target/'large.md').write_bytes(b'a' * 64001)
        (target/'app.txt').unlink()
        child.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(child)
        root.child_reviews[child.process_id] = {'verdict': 'approved'}
        result = manager.promote_process_results(root, [child.process_id])
        assert result['promoted'] and name in result['changed_files']
        assert len(result['artifact_notices']) == 3
        record_promotion(root, result)
        artifacts, notices = collect_artifacts(root, [name])
        assert not notices and len(artifacts) == 1
        assert artifacts[0]['path'] == name
        artifacts, notices = collect_artifacts(root, ['binary.dat', 'large.md', 'app.txt'])
        assert not artifacts and len(notices) == 3
    finally:
        cleanup(manager, storage)


def test_partial_restore_keeps_original_upstream_without_republishing_it(tmp_path_factory):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'): first = delegate(tools)
        upstream = manager.get(first['process_id'])
        path = Path(manager.materialize_process_worktree(upstream))
        (path/'app.txt').write_text('upstream base\n', encoding='utf-8')
        upstream.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(upstream)
        root.task_budget.release_process(upstream.process_id, status='completed')
        with patch.object(manager, 'start'): old_response = delegate(tools, depends_on=[upstream.process_id])
        old = manager.get(old_response['process_id'])
        source = Path(manager.materialize_process_worktree(old))
        (source/'new.txt').write_text('partial consumer\n', encoding='utf-8')
        old.status = ProcessStatus.FAILED
        root.task_budget.release_process(old.process_id, status='failed')
        with patch.object(manager, 'start'): resumed = delegate(tools, continue_process_id=old.process_id)
        new = manager.get(resumed['process_id'])
        assert new.depends_on == [upstream.process_id]
        target = Path(manager.materialize_process_worktree(new, continue_process_id=old.process_id))
        assert (target/'app.txt').read_text(encoding='utf-8') == 'upstream base\n'
        assert (target/'new.txt').read_text(encoding='utf-8') == 'partial consumer\n'
        new.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(new)
        assert new.worktree['changed_files'] == ['new.txt']
        for child in (upstream, new): root.child_reviews[child.process_id] = {'verdict': 'approved'}
        result = manager.promote_process_results(root, [new.process_id])
        assert set(result['process_ids']) == {upstream.process_id, new.process_id}
        assert set(result['changed_files']) == {'app.txt', 'new.txt'}
        retained = storage.load_worktree_state(new.process_id)
        restored = WorktreeLease.from_dict(retained)
        assert restored.resumed_from_process_id == old.process_id and restored.continuation_commit
    finally:
        cleanup(manager, storage)


@pytest.mark.parametrize('changed_after_snapshot', [False, True])
def test_noop_approved_result_has_explicit_snapshot_evidence_not_fake_write(tmp_path_factory, changed_after_snapshot):
    repo, storage, root, manager, tools = setup(tmp_path_factory)
    try:
        with patch.object(manager, 'start'): response = delegate(tools, target_files=['app.txt'])
        child = manager.get(response['process_id'])
        manager.materialize_process_worktree(child)
        child.status = ProcessStatus.COMPLETED
        manager.seal_process_worktree(child)
        root.child_reviews[child.process_id] = {'verdict': 'approved'}
        manager.dispose_process_worktree(child, keep_ref=True)
        if changed_after_snapshot:
            (repo/'app.txt').write_text('concurrent user edit\n', encoding='utf-8')
        result = manager.promote_process_results(root, [child.process_id])
        assert result['promoted'] and result['changed_files'] == [] and result['commits'] == []
        record_promotion(root, result)
        artifacts, notices = collect_artifacts(root, ['app.txt'])
        if changed_after_snapshot:
            assert not artifacts and notices
            assert (repo/'app.txt').read_text(encoding='utf-8') == 'concurrent user edit\n'
        else:
            assert not notices and artifacts[0]['evidence_kind'] == 'snapshot_verified'
            assert artifacts[0]['segments'][0]['text'] == (repo/'app.txt').read_bytes().decode('utf-8')
    finally:
        cleanup(manager, storage)
