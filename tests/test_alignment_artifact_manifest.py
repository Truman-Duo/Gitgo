"""Review certificates cover their witnessed subset, not every mentioned file."""
import pytest
from tests.test_delivery_artifacts import written
from backend.core.loop.delivery_artifacts import collect_artifacts, artifact_manifest, verify_manifest


def test_post_stage_delivery_keeps_exact_claim_files_without_promoting_other_claim(tmp_path_factory):
    from backend.core.loop.alignment_runtime import AlignmentRuntime
    from backend.core.loop.completion_protocol import CompletionClaim
    p = written(tmp_path_factory)
    p.completion_claim = CompletionClaim.from_args({'result': 'The plan is in plan.md; now submit for review.',
        'files': ['plan.md']}, step=1)
    runtime = AlignmentRuntime(p)
    payload = runtime.payload(candidate=p.completion_claim.result)
    assert [a['path'] for a in payload['published_artifacts']] == ['plan.md']
    assert not payload['artifact_notices']
    assert not runtime.payload(candidate='A different candidate').get('published_artifacts')
    assert not runtime.payload(candidate=p.completion_claim.result, proposed_files=[])['published_artifacts']
    (tmp_path_factory / 'plan.md').write_text('unwitnessed later change', encoding='utf-8')
    changed = runtime.payload(candidate=p.completion_claim.result)
    assert not changed['published_artifacts'] and changed['artifact_notices']


@pytest.mark.parametrize('extra', ['unchanged_test.py', 'absent_reference.txt'])
def test_unreviewed_extra_cannot_invalidate_or_join_reviewed_manifest(tmp_path_factory, extra):
    p=written(tmp_path_factory)
    if extra=='unchanged_test.py':
        (tmp_path_factory/extra).write_text('original assertion remains unchanged',encoding='utf-8')
    artifacts,notices=collect_artifacts(p,['plan.md',extra])
    assert len(notices)==1 and [a['path'] for a in artifacts]==['plan.md']
    manifest=artifact_manifest(artifacts)
    assert verify_manifest(p,manifest,['plan.md',extra])
    assert not verify_manifest(p,manifest,[extra]), 'The actual reviewed artifact must remain in the claim'
    (tmp_path_factory/'plan.md').write_text('changed since review',encoding='utf-8')
    assert not verify_manifest(p,manifest,['plan.md',extra]), 'Changed reviewed content must still block completion'
