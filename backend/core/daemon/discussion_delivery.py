"""Deliver retained BTW facts to the same registered lifecycle after restart."""


def notify_discussion(result, manager, store, workspace, provider, emit):
    if not result.get('discussion_ref') or manager is None:
        return
    from backend.core.loop.conversation_continuation import resolve_conversation_process
    from backend.core.mechanism_runtime.application import bound_application
    for process_id in result['source_process_ids']:
        target = None
        try:
            # A bounded snapshot can refer to a cold checkpoint. Restore its
            # identity through the normal continuation boundary; never silently
            # drop its event just because it has no in-memory executor yet.
            # Interrupted execution still requires explicit recovery. Its
            # immutable discussion remains available to the existing catch-up.
            target = resolve_conversation_process(manager, store, workspace, process_id)
            application = bound_application(target)
            if application:
                if application.provider is None:
                    application.bind_provider(provider, emit)
                application.receive_discussion(result['discussion_ref'])
        except Exception as error:
            emit({'event': 'mechanism_maintenance_failed', 'process_id': process_id,
                  'source_ref': result['discussion_ref'], 'error': str(error),
                  'effect_state': 'inspect_required', 'discussion_committed': True})
        finally:
            # Wake normal state observers even if a consumer failed after a
            # committed effect. Storage remains owned by the invoked services.
            if target is not None:
                manager.notify_state_changed(process_id)
