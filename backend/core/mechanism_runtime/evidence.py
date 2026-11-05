"""Task-scoped read adapters over canonical decisions and context assembly."""
from copy import deepcopy
from .contracts import digest


def services(application):
    def decisions(ctx, payload):
        from backend.core.loop.decision_support import confirmed_user_state
        from backend.core.loop.question_broker import decision_evidence
        offset = payload.get('offset', 0)
        limit = payload.get('limit', 16)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('MECHANISM_EVIDENCE_PAGE_INVALID')
        process = application.process
        items = confirmed_user_state(process.session.host_ledger, task_id=ctx.task_id)
        page = deepcopy(items[offset:offset + limit])
        result = {'task_id': ctx.task_id, 'confirmed': page, 'total': len(items),
            'next_offset': offset + limit if len(items) > offset + limit else None,
            'questions': decision_evidence(process, limit=32),
            'authority': 'canonical_current_task_decision_ledger', 'authority_granted': False}
        if 'material_reference' in payload:
            reference = payload['material_reference']
            if not isinstance(reference, str) or not reference or len(reference) > 2000:
                raise ValueError('MECHANISM_EVIDENCE_REFERENCE_INVALID')
            result['material_facts'] = deepcopy([e for e in process.session.host_ledger
                if e.get('event') == 'user_decision_material_attached' and e.get('task_id') == ctx.task_id
                    and e.get('reference') == reference][-32:])
        return result

    def context(ctx, payload):
        process = application.process
        # This is the assembly input snapshot, not a claim that a request has
        # already reached the provider. Actual sent requests remain in Trace.
        messages = process.session.to_provider_messages(dynamic_envelope=application.envelope())
        limit = payload.get('limit', 16)
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('MECHANISM_EVIDENCE_PAGE_INVALID')
        offset = payload.get('offset', 0)
        if type(offset) is not int or offset < 0:
            raise ValueError('MECHANISM_EVIDENCE_PAGE_INVALID')
        selected = messages[offset:offset + limit]
        return {'task_id': ctx.task_id, 'context_epoch': process.session.context_epoch,
            'stage': 'pre_request_assembly_input', 'total': len(messages),
            'messages': [{key: deepcopy(value) for key, value in message.items() if key in
                {'role', 'content', 'message_type', 'provider_visible', 'message_id'}} for message in selected],
            'next_offset': offset + limit if len(messages) > offset + limit else None,
            'assembly_digest': digest(messages), 'authority_granted': False}
    return {'decision.read': decisions, 'context.inspect': context}
