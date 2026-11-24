"""Public input projection from existing task records, never Provider replay.

New tasks retain their actual user input in the task contract transaction.
Legacy repair uses archived user records only when their text exactly matches
the task's recorded request; a summary is never presented as user speech.
"""
from .models import StorageReferenceMissing
import hashlib
import uuid


def _legacy_boundary_sources(archive, session_id, journal_size):
    """Identity evidence for the old eight-message boundary-copy format.

    Before the fix, copies lost their IDs and acquired journal-position hashes.
    Recognize positional IDs within the observed journal/archive footprint and
    the archived retained tail's original identity. This is not text deduplication.
    """
    sources = {}
    footprint = journal_size
    for epoch in archive:
        footprint = max(footprint, len(epoch.get('messages', [])))
        if epoch.get('reason') != 'completed_task_boundary':
            continue
        public = [m for m in epoch.get('messages', []) if m.get('provider_visible') is not False
                  and m.get('role') in {'user', 'assistant'}
                  and m.get('message_type', '') in {'', 'conversation'} and str(m.get('content') or '').strip()]
        for message in public[-8:]:
            if message.get('role') != 'user' or not message.get('message_id') or not message.get('turn_id'):
                continue
            original = (message['message_id'], message['turn_id'], str(message['content']).strip()[:8000])
            sources.setdefault(original[2], set()).add(original)
    identities = {hashlib.sha256(f'{session_id}:{position}'.encode()).hexdigest()
                  for position in range(footprint)}
    return identities, sources


def restore_task_inputs(store, tasks, grouped, metadata, *, session_id='', journal_size=0):
    from .repositories.presentation import _is_public_user_message
    archived = None
    replay_sources = None
    known_inputs = set()
    def archive():
        nonlocal archived
        if archived is None:
            archived = (store._read_json_blob(str(metadata['epoch_archive_ref']))
                        if metadata.get('epoch_archive_ref') else metadata.get('epoch_archive') or [])
        return archived
    def native_identity(message):
        try:
            uuid.UUID(str(message.get('message_id') or ''))
            return True
        except ValueError:
            return False
    for task in tasks:
        turn = str(task['task_id'])
        reference = str(task['contract_ref'] or '')
        if not reference:
            continue
        try:
            contract = store._read_json_blob(reference)
            inputs = contract.get('conversation_inputs')
            if inputs is not None:
                selected = inputs
                if session_id and any(len(str(m.get('message_id') or '')) == 64 for m in inputs) and any(native_identity(m) for m in inputs):
                    if replay_sources is None:
                        replay_sources = _legacy_boundary_sources(archive(), session_id, journal_size)
                    selected = [m for m in inputs if not any(
                        source in known_inputs and source[1] != turn and source[2] == m.get('content')
                        for source in (replay_sources[1].get(m.get('content'), ())
                                       if m.get('message_id') in replay_sources[0] else ()))]
                # This list is the task's durable public-input authority. Live
                # messages may have moved, gained Provider metadata or vanished.
                grouped[turn] = [m for m in grouped.get(turn, []) if m.get('role') != 'user']
                grouped[turn] = [
                    {**m, 'turn_id': turn, 'role': 'user', 'final': True,
                     'kind': 'conversation', 'visibility': 'public', 'sequence': i}
                    for i, m in enumerate(selected) if _is_public_user_message(m)
                ] + grouped[turn]
                known_inputs.update((m['message_id'], turn, str(m['content']).strip()[:8000])
                                    for m in selected if m.get('message_id') and _is_public_user_message(m))
                continue
            if any(m.get('role') == 'user' for m in grouped.get(turn, [])):
                continue
            description = str(contract.get('task_description') or '')
            original = next((m for epoch in archive() for m in epoch.get('messages', [])
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
