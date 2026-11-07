"""Durable user inputs survive Provider GC and preserve repeated requests."""
from backend.core.loop.session import AgentSession
from backend.core.storage import StorageRuntime


def save(store, task, messages, *, description='', metadata=None):
    store.save_agent_checkpoint(dict(process_id=task, session_id='shared', task_id=task,
        status='completed', messages=messages, task_description=description,
        session_metadata=metadata or {},
        result={'status': 'completed', 'response': 'answer:' + task}))


def test_compaction_and_reopen_keep_each_identical_user_turn(tmp_path_factory):
    workspace = tmp_path_factory / 'project'; workspace.mkdir()
    state = tmp_path_factory / 'state'
    session = AgentSession()
    with StorageRuntime(workspace, state_home=state) as store:
        for task in ('t1', 't2', 't3'):
            session.append_user('same request', message_type='conversation', turn_id=task)
            save(store, task, session.messages)
            session.begin_context_epoch(summary='working summary', retained_messages=[], reason='test')
            save(store, task, session.messages)
    with StorageRuntime(workspace, state_home=state) as store:
        public = store._public_session_messages('shared')
    assert [(m['role'], m['content'], m['turn_id']) for m in public] == [
        pair for task in ('t1', 't2', 't3') for pair in
        [('user', 'same request', task), ('assistant', 'answer:' + task, task)]]
    assert len({m['message_id'] for m in public}) == 6


def test_legacy_archive_restores_actual_user_text_without_using_summary(tmp_path_factory):
    workspace = tmp_path_factory / 'project'; workspace.mkdir()
    with StorageRuntime(workspace) as store:
        save(store, 't1', [{'role': 'user', 'content': 'original request'}], description='original request')
        # Legacy contract shape, before durable inputs were added. Use product
        # CAS publication; only the compatibility fixture removes the new field.
        row = store._state.execute("SELECT contract_ref FROM tasks WHERE task_id='t1'").fetchone()
        contract = store._read_json_blob(row['contract_ref']); contract.pop('conversation_inputs')
        ref = store.put_blob(__import__('json').dumps(contract).encode(), media_type='application/json')
        store._state.execute("UPDATE tasks SET contract_ref=? WHERE task_id='t1'", (ref,))
        save(store, 't2', [{'role': 'user', 'content': 'new request'}], metadata={
            'epoch_archive': [{'epoch': 0, 'messages': [
                {'role': 'user', 'content': 'original request'},
                {'role': 'user', 'content': '[HOST steering]', 'message_type': 'host_steering_delta'}]}]})
        public = store._public_session_messages('shared')
    assert [m['content'] for m in public] == ['original request', 'answer:t1', 'new request', 'answer:t2']


def test_internal_guidance_never_becomes_public_input(tmp_path_factory):
    workspace = tmp_path_factory / 'project'; workspace.mkdir()
    with StorageRuntime(workspace) as store:
        save(store, 't1', [{'role': 'user', 'content': 'actual request'},
                          {'role': 'user', 'content': '[HOST update]', 'message_type': 'host_steering_delta'}])
        save(store, 't1', [])
        assert [m['content'] for m in store._public_session_messages('shared')] == ['actual request', 'answer:t1']
