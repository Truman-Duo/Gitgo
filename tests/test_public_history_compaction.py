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


def test_completed_boundary_keeps_input_identity_without_republishing_the_tail(tmp_path_factory):
    workspace = tmp_path_factory / 'project'; workspace.mkdir()
    session = AgentSession()
    with StorageRuntime(workspace, state_home=tmp_path_factory / 'state') as store:
        for task, text in [('t1', 'hello'), ('t2', 'which model'), ('t3', 'what changed')]:
            session.append_user(text, message_type='conversation', turn_id=task)
            session.append_assistant('answer:' + task)
            save(store, task, session.messages)
        originals = {m['message_id'] for m in session.messages if m['role'] == 'user'}
        session.messages.insert(0, {'role': 'system', 'content': 'x' * 10000,
                                    'message_type': 'compiled_system_prompt'})
        assert session.compact_completed_task_boundary(checkpoint={'status': 'completed'}, token_threshold=1024)
        retained = [m for m in session.messages if m.get('context_purpose') == 'retained_dialogue' and m['role'] == 'user']
        assert {m['message_id'] for m in retained} == originals
        session.append_user('hello', message_type='conversation', turn_id='t4')
        save(store, 't4', session.messages, metadata={'epoch_archive': session.epoch_archive})
        session.messages.insert(0, {'role': 'system', 'content': 'updated prefix'})
        save(store, 't4', session.messages, metadata={'epoch_archive': session.epoch_archive})
        public = store._public_session_messages('shared')
        assert [(m['turn_id'], m['content']) for m in public if m['role'] == 'user'] == [
            ('t1', 'hello'), ('t2', 'which model'), ('t3', 'what changed'), ('t4', 'hello')]


def test_old_boundary_copies_are_reconciled_by_archive_identity_without_rewriting_records(tmp_path_factory):
    import copy
    workspace = tmp_path_factory / 'project'; workspace.mkdir()
    session = AgentSession()
    with StorageRuntime(workspace, state_home=tmp_path_factory / 'state') as store:
        for task, text in [('t1', 'hello'), ('t2', 'which model'), ('t3', 'what changed')]:
            session.append_user(text, message_type='conversation', turn_id=task)
            session.append_assistant('answer:' + task)
            save(store, task, session.messages)
        archive = [{'reason': 'completed_task_boundary', 'messages': copy.deepcopy(session.messages)}]
        # The actual old format discarded IDs and then shifted journal slots
        # when the compiled prefix was installed. Both checkpoints are retained.
        session.messages = [{k: m[k] for k in ('role', 'content')} | {'message_type': 'conversation'}
                            for m in session.messages]
        session.messages[:0] = [{'role': 'system', 'content': 'prefix'}, {'role': 'system', 'content': 'task'}]
        save(store, 't4', session.messages, metadata={'epoch_archive': archive})
        session.append_user('hello', message_type='conversation', turn_id='t4')
        session.messages.insert(0, {'role': 'system', 'content': 'runtime seed'})
        save(store, 't4', session.messages, metadata={'epoch_archive': archive})
        row = store._state.execute("SELECT contract_ref FROM tasks WHERE task_id='t4'").fetchone()
        raw = store._read_json_blob(row['contract_ref'])
        assert len(raw['conversation_inputs']) == 7
        for _ in range(3):
            public = store._public_session_messages('shared')
            assert [(m['turn_id'], m['content']) for m in public if m['role'] == 'user'] == [
                ('t1', 'hello'), ('t2', 'which model'), ('t3', 'what changed'), ('t4', 'hello')]
        assert store._read_json_blob(row['contract_ref']) == raw
        # Without archived provenance, even identical text must remain visible.
        from backend.core.storage.conversation_history import restore_task_inputs
        grouped = {}
        restore_task_inputs(store, [{'task_id': 't4', 'contract_ref': row['contract_ref']}], grouped, {}, session_id='shared')
        assert len(grouped['t4']) == 7
