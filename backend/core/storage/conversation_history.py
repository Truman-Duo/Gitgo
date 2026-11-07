"""Public input projection from existing task records, never Provider replay.

New tasks retain their actual user input in the task contract transaction.
Legacy repair uses archived user records only when their text exactly matches
the task's recorded request; a summary is never presented as user speech.
"""
from .models import StorageReferenceMissing


def restore_task_inputs(store, tasks, grouped, metadata):
    from .repositories.presentation import _is_public_user_message
    archived = None
    for task in tasks:
        turn = str(task['task_id'])
        reference = str(task['contract_ref'] or '')
        if not reference:
            continue
        try:
            contract = store._read_json_blob(reference)
            inputs = contract.get('conversation_inputs')
            if inputs is not None:
                # This list is the task's durable public-input authority. Live
                # messages may have moved, gained Provider metadata or vanished.
                grouped[turn] = [m for m in grouped.get(turn, []) if m.get('role') != 'user']
                grouped[turn] = [
                    {**m, 'turn_id': turn, 'role': 'user', 'final': True,
                     'kind': 'conversation', 'visibility': 'public', 'sequence': i}
                    for i, m in enumerate(inputs) if _is_public_user_message(m)
                ] + grouped[turn]
                continue
            if any(m.get('role') == 'user' for m in grouped.get(turn, [])):
                continue
            if archived is None:
                archived = (store._read_json_blob(str(metadata['epoch_archive_ref']))
                            if metadata.get('epoch_archive_ref') else metadata.get('epoch_archive') or [])
            description = str(contract.get('task_description') or '')
            original = next((m for epoch in archived for m in epoch.get('messages', [])
                             if _is_public_user_message(m) and m.get('content') == description), None)
            if original is not None:
                grouped.setdefault(turn, []).insert(0, {
                    'message_id': str(original.get('message_id') or f'archived-input:{turn}'),
                    'turn_id': turn, 'role': 'user', 'content': original['content'],
                    'timestamp': str(original.get('timestamp') or task['created_at']),
                    'final': True, 'kind': 'conversation', 'visibility': 'public', 'sequence': 0,
                })
        except StorageReferenceMissing as exc:
            grouped.setdefault(turn, []).append(store._storage_projection_warning(exc, turn_id=turn))
