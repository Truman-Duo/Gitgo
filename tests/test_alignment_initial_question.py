"""A question after real work can have actual preparation dependencies."""
import pytest
from tests.test_alignment_semantics import process
from backend.core.loop.alignment_runtime import AlignmentRuntime
from backend.core.loop.alignment_routing import review_reason


@pytest.mark.parametrize('receipt, expected', [
    (None, ''),
    ({'task_id': 'earlier-task', 'committed': True}, ''),
    ({'task_id': 't', 'committed': False}, ''),
    ({'task_id': 't', 'committed': True}, 'question_necessity'),
])
def test_first_question_shortcut_does_not_skip_actual_current_work_dependencies(tmp_path_factory, receipt, expected):
    p = process(tmp_path_factory)
    p.task_kind = 'answer'
    if receipt:
        p.tool_receipts.append({'receipt_id': 'unit-observation', 'effect': 'read', **receipt})
    payload = AlignmentRuntime(p).payload(proposed_questions=[])
    assert review_reason(p, payload, 'question_proposal') == expected
