"""Host failures preserve actionable metadata without forwarding private state."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest

from backend.core.application import OperationError
from backend.core.daemon.client import DaemonClient, DaemonCommandError
from backend.core.native_host import NativeHost


def failure():
    return {
        'event': 'command_result', 'cmd': 'task',
        'code': 'CONTINUATION_ADOPTION_HANDOFF_REQUIRED',
        'error': 'CONTINUATION_ADOPTION_HANDOFF_REQUIRED: The saved change remains pending.',
        'owner_process_id': 'owner-A',
        'next_actions': [{'action': 'list_agents', 'process_id': 'owner-A',
                          'reason': 'Inspect the retained responsibility.', 'private_state': 'PRIVATE'},
                         {'action': 'delegate_task', 'patch': {'continue_process_id': 'worker-B',
                          'continuation_mode': 'remaining', 'required_for_parent_completion': True,
                          'private_state': 'PRIVATE'}},
                         {'action': {'private_state': 'PRIVATE'}}, 'PRIVATE'],
        'private_state': 'PRIVATE',
    }


def reply(host, stdout, request):
    host._handle_request(request)
    return next(json.loads(row) for row in stdout.getvalue().splitlines()
                if json.loads(row).get('type') == 'response')


def test_real_client_and_host_keep_explicit_code_and_allowed_recovery_metadata(monkeypatch):
    result = failure()
    before = deepcopy(result)
    client = DaemonClient('error-context-fixture')

    def deliver(command):
        client._cmd_results[command['request_id']] = result
        client._cmd_events[command['request_id']].set()

    stdout = io.StringIO()
    host = NativeHost(stdout=stdout)
    monkeypatch.setattr(client, '_write_cmd', deliver)
    monkeypatch.setattr(host, '_dispatch', lambda request: client.send_task({'cmd': 'task', 'task_id': 'task-B'}))
    try:
        value = reply(host, stdout, {'request_id': 'r', 'operation': 'runtime.feedback', 'arguments': {}})
        assert value['ok'] is False
        error = value['error']
        assert error['code'] == result['code']
        assert error['message'] == result['error']
        assert error['details']['owner_process_id'] == 'owner-A'
        assert error['details']['next_actions'] == [
            {'action': 'list_agents', 'process_id': 'owner-A', 'reason': 'Inspect the retained responsibility.'},
            {'action': 'delegate_task', 'patch': {'continue_process_id': 'worker-B',
             'continuation_mode': 'remaining', 'required_for_parent_completion': True}}]
        assert 'PRIVATE' not in json.dumps(value)
        assert result == before
        assert client._agent_events == {} and client._agent_data == {}
    finally:
        host.close()


@pytest.mark.parametrize('code', [None, '', {'private': 'value'}])
def test_legacy_error_identity_is_not_inferred_from_prose(code):
    error = DaemonCommandError('task', {'code': code, 'error': 'Legacy failure: keep this text',
        'error_info': {'catalog_id': 'GITGO-E7406', 'details': {'ref': 'sha256:record'}}})
    assert error.code == 'Legacy failure: keep this text'
    assert error.message == 'Legacy failure: keep this text'
    assert error.details['ref'] == 'sha256:record'
    assert error.error_info['catalog_id'] == 'GITGO-E7406'


@pytest.mark.parametrize('stage', ['stop', 'continue'])
@pytest.mark.parametrize('error_kind', ['daemon', 'operation', 'unconfirmed'])
def test_saved_btw_adoption_survives_later_failure_without_repeating_it(monkeypatch, stage, error_kind):
    stdout = io.StringIO()
    host = NativeHost(stdout=stdout)
    saved = {'status': 'pending', 'authority_granted': False, 'private_state': 'PRIVATE',
             'change': {'id': 'saved-change', 'target_process_id': 'worker-B', 'target_task_id': 'task-B',
                        'source_ref': 'sha256:discussion', 'content': 'PRIVATE', 'status': 'pending'}}
    calls = []

    def send(command, **kwargs):
        calls.append(('save', command))
        return deepcopy(saved)

    rejection = (DaemonCommandError('task', failure()) if error_kind == 'daemon' else
                 OperationError('BTW_CANCELLATION_PENDING', 'The adopted change is saved; cleanup is pending.',
                                details={'closeout_state': 'pending'}))
    if error_kind == 'unconfirmed':
        rejection = DaemonCommandError('task', {'code': 'OBSERVATION_UNAVAILABLE',
                                               'error': 'The runtime outcome could not be confirmed.'})

    def followup(name):
        def action(*args, **kwargs):
            calls.append((name, kwargs))
            if stage == name:
                raise rejection
            return {'tree_terminal': True}
        return action

    monkeypatch.setattr(host, '_get_daemon', lambda *args, **kwargs: SimpleNamespace(send_command=send))
    monkeypatch.setattr(host, '_runtime_stop', followup('stop'))
    monkeypatch.setattr(host, '_runtime_feedback', followup('continue'))
    try:
        value = reply(host, stdout, {'request_id': 'adopt', 'operation': 'runtime.btw.adopt', 'arguments': {
            'project': 'fixture', 'process_id': 'worker-B', 'task_id': 'task-B', 'mode': 'interrupt',
            'sidecar_id': 'sidecar', 'discussion_ref': 'sha256:discussion', 'expected_revision': 0}})
        assert value['ok'] is False
        assert value['error']['code'] == rejection.code
        assert value['error']['details']['adoption'] == {
            'saved': True, 'status': 'pending', 'change_id': 'saved-change',
            'process_id': 'worker-B', 'task_id': 'task-B', 'source_ref': 'sha256:discussion',
            'followup_stage': stage, 'continuation_status': 'not_started'
            if stage == 'continue' and error_kind != 'unconfirmed' else 'unconfirmed'}
        assert 'saved as pending' in value['error']['message']
        assert ('Continuation was not started.' in value['error']['message']) == (stage == 'continue' and error_kind != 'unconfirmed')
        if error_kind == 'daemon':
            assert value['error']['details']['owner_process_id'] == 'owner-A'
        elif error_kind == 'operation':
            assert value['error']['details']['closeout_state'] == 'pending'
        assert [item[0] for item in calls] == (['save', 'stop'] if stage == 'stop' else ['save', 'stop', 'continue'])
        assert 'PRIVATE' not in json.dumps(value)
        assert 'result' not in value
    finally:
        host.close()


def test_rejected_adoption_does_not_claim_it_was_saved(monkeypatch):
    stdout = io.StringIO()
    host = NativeHost(stdout=stdout)
    def reject(command, **kwargs):
        raise DaemonCommandError('task', {'code': 'ADOPTION_TARGET_CHANGED', 'error': 'ADOPTION_TARGET_CHANGED'})
    monkeypatch.setattr(host, '_get_daemon', lambda *args, **kwargs: SimpleNamespace(send_command=reject))
    monkeypatch.setattr(host, '_runtime_stop', lambda *args, **kwargs: pytest.fail('Must not stop after rejected adoption'))
    try:
        value = reply(host, stdout, {'request_id': 'rejected', 'operation': 'runtime.btw.adopt', 'arguments': {
            'project': 'fixture', 'process_id': 'worker-B', 'mode': 'interrupt'}})
        assert value['error']['code'] == 'ADOPTION_TARGET_CHANGED'
        assert 'adoption' not in value['error']['details']
    finally:
        host.close()


@pytest.mark.parametrize('failure_at', ['kill', 'loop_status', 'chat'])
@pytest.mark.parametrize('transport', ['timeout', 'disconnect', 'broken_pipe'])
def test_real_client_transport_failure_after_saved_adoption_is_unconfirmed(monkeypatch, failure_at, transport):
    stdout = io.StringIO()
    host = NativeHost(stdout=stdout)
    client = DaemonClient('transport-after-adoption')
    calls = []
    saved = {'status': 'pending', 'change': {'id': 'saved-change', 'target_process_id': 'worker-B',
             'target_task_id': 'task-B', 'source_ref': 'sha256:discussion'}}

    def deliver(command):
        action = command.get('action') or command['cmd']
        calls.append(action)
        request_id = command['request_id']
        if action == failure_at:
            if transport == 'broken_pipe':
                raise BrokenPipeError('fixture pipe closed')
            if transport == 'disconnect':
                client._cmd_events[request_id].set()  # Real client sees no result.
            return  # timeout: real client wait expires without any response.
        result = saved if action == 'btw_adopt' else {'tree_terminal': True}
        if action == 'loop_status':
            result = {'processes': {'worker-B': {'status': 'cancelled', 'active_task_id': 'task-B',
                       'actor_kind': 'worker', 'parent_id': 'owner-A'}}}
        client._cmd_results[request_id] = {'event': 'command_result', 'result': deepcopy(result)}
        client._cmd_events[request_id].set()

    real_send = client.send_command
    monkeypatch.setattr(client, 'send_command', lambda command, **kwargs: real_send(command, timeout=.005))
    monkeypatch.setattr(client, '_write_cmd', deliver)
    monkeypatch.setattr(client, 'is_running', lambda: True)  # No reconnect/replay.
    monkeypatch.setattr(host, '_get_daemon', lambda *args, **kwargs: client)
    monkeypatch.setattr(host.services, 'config_get', lambda: {})
    try:
        value = reply(host, stdout, {'request_id': 'transport', 'operation': 'runtime.btw.adopt', 'arguments': {
            'project': 'fixture', 'process_id': 'worker-B', 'task_id': 'task-B', 'mode': 'interrupt'}})
        assert value['error']['code'] == 'INTERNAL_ERROR'
        adoption = value['error']['details']['adoption']
        assert adoption['saved'] is True and adoption['change_id'] == 'saved-change'
        assert adoption['continuation_status'] == 'unconfirmed'
        assert adoption['followup_stage'] == ('stop' if failure_at == 'kill' else 'continue')
        assert 'saved as pending' in value['error']['message']
        assert 'Continuation was not started.' not in value['error']['message']
        expected = ['btw_adopt', 'kill', 'loop_status', 'chat']
        assert calls == expected[:expected.index(failure_at)+1]
        assert client._agent_events == {} and client._agent_data == {}
    finally:
        host.close()


def test_initial_adoption_transport_failure_has_no_confirmed_saved_receipt(monkeypatch):
    stdout = io.StringIO()
    host = NativeHost(stdout=stdout)
    def lost_ack(*args, **kwargs):
        raise RuntimeError('fixture disconnected before adoption reply')
    monkeypatch.setattr(host, '_get_daemon', lambda *args, **kwargs: SimpleNamespace(send_command=lost_ack))
    try:
        value = reply(host, stdout, {'request_id': 'unknown-save', 'operation': 'runtime.btw.adopt', 'arguments': {
            'project': 'fixture', 'process_id': 'worker-B', 'mode': 'interrupt'}})
        assert value['error']['code'] == 'INTERNAL_ERROR'
        assert 'adoption' not in value['error']['details']
    finally:
        host.close()


@pytest.mark.parametrize('kind', ['blocked', 'commit_unknown', 'unsupported', 'corruption'])
def test_saved_adoption_does_not_downgrade_special_storage_errors(monkeypatch, kind):
    from backend.core.storage import StorageBlocked, StorageCommitUnknown, StorageRuntimeUnsupported, StorageCorruptionDetected
    exceptions = {'blocked': StorageBlocked('blocked'), 'commit_unknown': StorageCommitUnknown('checkpoint'),
                  'unsupported': StorageRuntimeUnsupported('old-fixture'),
                  'corruption': StorageCorruptionDetected('fixture.sqlite3', 'fixture damage')}
    expected = {'blocked': 'STORAGE_BLOCKED', 'commit_unknown': 'STORAGE_COMMIT_UNKNOWN',
                'unsupported': 'STORAGE_UNSAFE_SQLITE_VERSION', 'corruption': 'STORAGE_CORRUPTION'}
    stdout = io.StringIO()
    host = NativeHost(stdout=stdout)
    saved = {'status': 'pending', 'change': {'id': 'saved-change', 'target_process_id': 'worker-B',
             'target_task_id': 'task-B', 'source_ref': 'sha256:discussion'}}
    monkeypatch.setattr(host, '_get_daemon', lambda *args, **kwargs: SimpleNamespace(send_command=lambda *a, **k: saved))
    def failed_stop(*args, **kwargs):
        raise exceptions[kind]
    monkeypatch.setattr(host, '_runtime_stop', failed_stop)
    try:
        value = reply(host, stdout, {'request_id': 'storage', 'operation': 'runtime.btw.adopt', 'arguments': {
            'project': 'fixture', 'process_id': 'worker-B', 'mode': 'interrupt'}})
        assert value['error']['code'] == expected[kind]
        assert value['error']['details']['automatic_replacement'] is False
        assert value['error']['error_info']['name'] == expected[kind]
    finally:
        host.close()
