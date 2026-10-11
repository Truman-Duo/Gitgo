from hashlib import sha256
import pytest
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.completion_protocol import CompletionClaim, HostCompletionEvaluator
from backend.core.loop.delivery_artifacts import collect_artifacts, MAX_ARTIFACT_BYTES


def written(tmp_path):
    p = AgentProcess(process_id='p', role='supervisor', ring_level=RingLevel.RING_3,
        actor_kind='supervisor', active_task_id='t', worktree_path=str(tmp_path), session=AgentSession())
    content = '# Plan\nModules, data, validation and milestones.'
    (tmp_path / 'plan.md').write_bytes(content.encode('utf-8'))
    receipt = {'receipt_id': 'written', 'task_id': 't', 'succeeded': True, 'committed': True,
               'effect_state': 'committed', 'effect': 'workspace_write', 'files': ['plan.md']}
    p.tool_receipts.append(receipt)
    p.session.append_tool_result('Written', tool_name='write_file', data={'path': 'plan.md',
        'after_sha256': sha256(content.encode()).hexdigest()}, receipt=receipt)
    return p


def test_file_delivery_uses_real_content_and_gate_rechecks_source(tmp_path_factory):
    tmp_path = tmp_path_factory
    p = written(tmp_path)
    runtime = AlignmentRuntime(p)
    candidate = 'The complete plan is in plan.md.'
    payload = runtime.payload('Give a complete plan', candidate, proposed_files=['plan.md'])
    artifacts = payload['published_artifacts']
    assert not payload['artifact_notices']
    assert 'Modules, data' in artifacts[0]['segments'][0]['text']
    result = runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [],
        'delivery_check': {'complete': True, 'evidence_refs': [artifacts[0]['segments'][0]['id']]}}, payload)
    assert result['status'] == 'ready'
    p.completion_claim = CompletionClaim.from_args({'result': candidate, 'files': ['plan.md']}, step=1)
    assert HostCompletionEvaluator.evaluate(p, candidate).allowed
    (tmp_path / 'plan.md').write_text('changed', encoding='utf-8')
    assert 'published artifact evidence changed' in ' '.join(HostCompletionEvaluator.evaluate(p, candidate).reasons)


@pytest.mark.parametrize('change', ['task', 'failed', 'uncommitted', 'receipt_removed', 'hash', 'too_large', 'deleted', 'binary'])
def test_artifact_fallback_retains_missing_facts_instead_of_trusting_filename(tmp_path_factory, change):
    tmp_path = tmp_path_factory
    p = written(tmp_path)
    if change == 'task': p.tool_receipts[0]['task_id'] = 'old'
    elif change == 'failed': p.tool_receipts[0]['succeeded'] = False
    elif change == 'uncommitted': p.tool_receipts[0]['committed'] = False
    elif change == 'receipt_removed': p.tool_receipts.clear()
    elif change == 'hash': (tmp_path / 'plan.md').write_text('different', encoding='utf-8')
    elif change == 'too_large': (tmp_path / 'plan.md').write_bytes(b'a' * (MAX_ARTIFACT_BYTES + 1))
    elif change == 'deleted': (tmp_path / 'plan.md').unlink()
    elif change == 'binary': (tmp_path / 'plan.md').write_bytes(b'\x00')
    artifacts, notices = collect_artifacts(p, ['plan.md'])
    assert not artifacts and notices


def test_arbitrary_file_and_escape_are_never_read_as_artifact_evidence(tmp_path_factory):
    tmp_path = tmp_path_factory
    p = written(tmp_path)
    (tmp_path / 'unrelated.md').write_text('not delivered', encoding='utf-8')
    artifacts, notices = collect_artifacts(p, ['unrelated.md', '../outside.md'])
    assert not artifacts and len(notices) == 2


def test_forged_artifact_ref_cannot_close_delivery_gate(tmp_path_factory):
    tmp_path = tmp_path_factory
    p = written(tmp_path)
    runtime = AlignmentRuntime(p)
    payload = runtime.payload('Give a plan', 'See plan.md', proposed_files=['plan.md'])
    ref = payload['published_artifacts'][0]['segments'][0]['id']
    with pytest.raises(ValueError, match='actual nonblank'):
        runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [],
            'delivery_check': {'complete': True, 'evidence_refs': [ref + 'forged']}}, payload)


def test_removed_file_from_completion_claim_cannot_reuse_artifact_certificate(tmp_path_factory):
    tmp_path = tmp_path_factory
    p = written(tmp_path)
    runtime = AlignmentRuntime(p)
    candidate = 'See plan.md'
    payload = runtime.payload('Give a plan', candidate, proposed_files=['plan.md'])
    ref = payload['published_artifacts'][0]['segments'][0]['id']
    runtime.apply({'status': 'ready', 'questions': [], 'interpretations': [],
        'delivery_check': {'complete': True, 'evidence_refs': [ref]}}, payload)
    p.completion_claim = CompletionClaim.from_args({'result': candidate}, step=1)
    assert not HostCompletionEvaluator.evaluate(p, candidate).allowed
