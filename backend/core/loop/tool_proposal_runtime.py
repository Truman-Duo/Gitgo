"""Review model-initiated questions, claims and valid task boundaries."""
from .answer_continuation import clear_answer_fragments
from .delivery_runtime import DeliveryBoundary
from .lifecycle import transition
from .models import ProcessStatus
from .outcome import OutcomeStatus
from .runtime_outcomes import _make_result
from .loop_bookkeeping import _inc_nudge_counter, _get_nudge_count

class ToolProposalRuntime:
    def __init__(self, process, session, get_tools, check_alignment):
        self.process, self.session, self.get_tools, self.check_alignment = process, session, get_tools, check_alignment

    def review(self, tool_calls, content):
        process, session, tools_dict = self.process, self.session, self.get_tools()
        _check_alignment = self.check_alignment
        # A tool boundary makes prior public text commentary, not an
        # uninterrupted deliverable. A subsequent answer must stand alone.
        clear_answer_fragments(process)
        question_calls = [tc for tc in tool_calls
            if (tc['name'] == 'request_user_decision'
                and tc.get('args', {}).get('kind', 'clarification') in {'clarification', 'preference', 'direction', 'choice'})
            or (tc['name'] == 'engineering_workflow' and tc.get('args', {}).get('operation') == 'ask_semantic')]
        if question_calls:
            alignment_result = _check_alignment(proposed_questions=[
                (tc.get('args') or {}).get('request', {}) if tc['name'] == 'engineering_workflow'
                else tc.get('args', {}) for tc in question_calls])
            if alignment_result is not None:
                decision = (alignment_result.get('metadata') or {}).get('pending_decision')
                published = alignment_result.get('status') == 'awaiting_user' and isinstance(decision, dict)
                for tc in tool_calls:
                    session.append_tool_result(
                        'Requirement review published this real user decision; the original tool batch was not separately executed.'
                        if published else 'Host assessed question necessity before execution; no tool in this batch ran.',
                        tool_name=tc['name'], tool_call_id=tc.get('id', ''), is_error=not published,
                        data={'error_code': 'ALIGNMENT_QUESTION_REVIEWED', 'executed': False,
                              'decision_published': published, **({'decision': decision} if published else {})})
                if 'alignment_repair' not in alignment_result:
                    return DeliveryBoundary(result=alignment_result)
                _inc_nudge_counter(process, 'alignment_question_review')
                if _get_nudge_count(process, 'alignment_question_review') >= 3:
                    transition(process, ProcessStatus.FAILED)
                    return DeliveryBoundary(result=_make_result(process, session, '模型反复提出不必要的问题，任务已保留，尚未交付。',
                                        error_code='ALIGNMENT_QUESTION_REJECTED', error_message=alignment_result['alignment_repair'][:600], outcome_status=OutcomeStatus.FAILED))
                session.append_user('[HOST REQUIREMENT CHECK] ' + alignment_result['alignment_repair'],
                                    message_type='host_alignment_repair')
                return DeliveryBoundary(retry=True)
        completion_call = next((tc for tc in tool_calls if tc['name'] in {'complete_task', 'complete_supervision'}), None)
        if completion_call:
            proposed = str(completion_call.get('args', {}).get('result') or content)
            alignment_result = _check_alignment(proposed, trigger='completion_claim',
                proposed_files=completion_call.get('args', {}).get('files') or [])
            if alignment_result is not None:
                for tc in tool_calls:
                    session.append_tool_result('Host requirement check prevented execution; no tool in this batch ran.',
                        tool_name=tc['name'], tool_call_id=tc.get('id', ''), is_error=True,
                        data={'error_code': 'ALIGNMENT_DELIVERY_BLOCKED', 'executed': False})
                if 'alignment_repair' not in alignment_result:
                    return DeliveryBoundary(result=alignment_result)
                _inc_nudge_counter(process, 'alignment_repair')
                if _get_nudge_count(process, 'alignment_repair') >= 3:
                    transition(process, ProcessStatus.FAILED)
                    return DeliveryBoundary(result=_make_result(process, session, '交付仍未采用已确认需求。', error_code='ALIGNMENT_DELIVERY_REJECTED',
                                        error_message=alignment_result['alignment_repair'][:600],
                                        outcome_status=OutcomeStatus.FAILED))
                session.append_user('[HOST REQUIREMENT CHECK] ' + alignment_result['alignment_repair'],
                                    message_type='host_alignment_repair')
                return DeliveryBoundary(retry=True)
        # Non-trivial intent is already compiled by declare_task_contract.
        # Review its genuine boundary, not every preceding conversation.
        from .tool_boundary_preview import preview_tool_boundaries
        boundary = preview_tool_boundaries(tool_calls, tools_dict)
        declarations, effectful = boundary.contracts, boundary.effectful
        alignment_result = _check_alignment(trigger='contract' if declarations else 'execution' if effectful else 'admission',
            proposed_contracts=declarations or None) if not question_calls and (not completion_call or declarations or effectful) else None
        if alignment_result is not None:
            for tc in tool_calls:
                session.append_tool_result('Host requirement boundary prevented this batch; no tool executed.',
                    tool_name=tc['name'], tool_call_id=tc.get('id', ''), is_error=True,
                    data={'error_code': 'ALIGNMENT_BOUNDARY_REVIEWED', 'executed': False})
            if 'alignment_repair' not in alignment_result:
                return DeliveryBoundary(result=alignment_result)
            _inc_nudge_counter(process, 'alignment_boundary_repair')
            if _get_nudge_count(process, 'alignment_boundary_repair') >= 3:
                transition(process, ProcessStatus.FAILED)
                return DeliveryBoundary(result=_make_result(process, session, '需求边界仍未满足，操作没有执行。',
                    error_code='ALIGNMENT_BOUNDARY_REJECTED', error_message=alignment_result['alignment_repair'][:600], outcome_status=OutcomeStatus.FAILED))
            session.append_user('[HOST REQUIREMENT CHECK] ' + alignment_result['alignment_repair'],
                message_type='host_alignment_repair')
            return DeliveryBoundary(retry=True)
        return DeliveryBoundary()
