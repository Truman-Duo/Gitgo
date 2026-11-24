"""Bound refusal diagnostics through the real broker and tool journal.

Socket endpoints and privileged control-file ownership are test fixtures;
grant checks, framed FD transport, service admission, and ToolPipeline execute
their production code. This is not administrator isolation acceptance.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading

import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX broker transport', allow_module_level=True)

from backend.core import macos_broker_capabilities as capabilities
from backend.core import macos_broker_client as client
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as server
from backend.core import macos_helper_lifecycle as lifecycle
from backend.core import macos_identity as identity
from backend.core import macos_project_authority as authority
from backend.core.storage.native_invocations import NAMESPACE, pending_native_invocations


BOOT = '9e130791-3c47-4d94-9149-758b1659e0ee'


@pytest.mark.parametrize('fault,expected', [
    ('expired', 'HELPER_GRANT_INVALID'), ('revoked', 'HELPER_GRANT_INVALID'),
    ('new_boot', 'HELPER_GRANT_INVALID'), ('token', 'HELPER_GRANT_INVALID'),
    ('grant', 'HELPER_GRANT_INVALID'), ('project', 'HELPER_GRANT_INVALID'),
    ('maintenance', 'HELPER_MAINTENANCE'), ('superseded', 'HELPER_MAINTENANCE'),
    ('pending_authority', 'HELPER_MAINTENANCE'), ('busy', 'HELPER_MAINTENANCE'),
    ('expired_after_receipt', 'HELPER_GRANT_INVALID'),
    ('maintenance_after_receipt', 'HELPER_MAINTENANCE'),
    ('old_helper', 'HELPER_UPGRADE_REQUIRED'), ('revision', 'HELPER_UPGRADE_REQUIRED'),
])
@pytest.mark.skipif(sys.platform != 'darwin', reason='Actual macOS pipeline dispatch')
def test_pipeline_keeps_known_denial_reason_without_launch_or_debt(tmp_path_factory, monkeypatch, fault, expected):
    from backend.core.storage import get_storage
    from tests.test_workspace_candidate_service import make

    project = tmp_path_factory / 'project'
    project.mkdir()
    target = project / 'input.txt'
    target.write_text('unchanged')
    target_before = (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns)
    fd = wire.open_directory(project)
    binding = dict(project=str(project), identity=wire.directory_identity(fd),
                   token='fixture-secret', grant_id='c' * 32, socket='fixture')
    os.close(fd)
    root = tmp_path_factory / 'service'
    private = root / 'private'
    private.mkdir(parents=True)
    ledger = private / 'seen.json'
    ledger.write_text('[]')
    ledger_before = (ledger.read_bytes(), ledger.stat().st_ino, ledger.stat().st_mtime_ns)
    approved = {**binding, 'owner_uid': os.getuid(), 'boot': BOOT, 'revoked': False, 'expires': 200,
                'token_hash': hashlib.sha256(binding['token'].encode()).hexdigest()}
    if fault == 'expired': approved['expires'] = 100
    if fault == 'revoked': approved['revoked'] = True
    if fault == 'new_boot': approved['boot'] = 'different-boot'
    if fault == 'token': approved['token_hash'] = hashlib.sha256(b'different').hexdigest()
    if fault == 'grant': approved['grant_id'] = 'd' * 32
    if fault == 'project': approved['identity'] = [0, 0, os.getuid()]
    state = dict(phase='switching' if fault == 'maintenance' else 'active',
                 helper='/superseded/gitgo-host' if fault == 'superseded' else str(Path(sys.executable).absolute()))
    left, right = socket.socketpair()
    right.settimeout(3)
    client_frames, server_frames, failures = [], [], []

    class StopListener(BaseException): pass

    class Connection:
        def connect(self, address): assert address == 'fixture'
        def __getattr__(self, name): return getattr(left, name)

    class Listener:
        accepted = False
        def bind(self, address): Path(address).touch()
        def listen(self, count): assert count == 4
        def settimeout(self, timeout): pass
        def accept(self):
            if self.accepted: raise StopListener()
            self.accepted = True
            return right, None
        def close(self): pass

    @contextmanager
    def lock(path, name):
        assert path == root and name == 'admission.lock'
        if fault == 'busy': raise capabilities.BrokerBusyError('fixture lock busy')
        yield

    def read(path, *args, **kwargs):
        if path == private / 'grant.json': return json.dumps(approved).encode()
        assert path == ledger
        return path.read_bytes()

    real_send = wire.send
    def send(connection, value):
        (server_frames if connection is right else client_frames).append(deepcopy(value))
        if connection is right and value.get('event') == 'capabilities':
            if fault == 'expired_after_receipt': approved['expires'] = 100
            if fault == 'maintenance_after_receipt': state['phase'] = 'switching'
            if fault == 'revision': value = {**value, 'protocol_version': 2}
        return real_send(connection, value)

    real_authorize = server.authorize
    def authorize(connection, received, request, value):
        if fault == 'old_helper' and request['op'] == 'capabilities':
            raise ValueError('Unsupported broker operation or fields')
        return real_authorize(connection, received, request, value)

    def peer():
        try: server.listen(root, persistent=True)
        except StopListener: pass
        except BaseException as error: failures.append(error)
        finally: right.close()

    monkeypatch.setattr(client.socket, 'socket', lambda *args, **kwargs:
                        Listener() if threading.current_thread().name == 'broker-fixture' else Connection())
    monkeypatch.setattr(wire, 'peer_uid', lambda connection: os.getuid() if connection is right else 0)
    monkeypatch.setattr(wire, 'send', send)
    monkeypatch.setattr(identity, 'boot_id', lambda: BOOT)
    monkeypatch.setattr(identity, 'bounded_read', read)
    monkeypatch.setattr(server, 'private_directory', lambda path: None)
    monkeypatch.setattr(server, 'raw_time', lambda: 100)
    monkeypatch.setattr(server, 'authorize', authorize)
    monkeypatch.setattr(server, 'execute', lambda *args: pytest.fail('Denied request started a task'))
    monkeypatch.setattr(lifecycle, 'managed', lambda path: path == root)
    monkeypatch.setattr(lifecycle, 'configuration', lambda path: dict(state))
    monkeypatch.setattr(lifecycle, 'locked', lock)
    monkeypatch.setattr(authority, 'pending', lambda path: fault == 'pending_authority')
    monkeypatch.setattr(os, 'chown', lambda *args: None)
    token = client._SESSION.set(binding)
    worker = threading.Thread(target=peer, name='broker-fixture')
    worker.start()
    try:
        _, call = make(project)
        result = call({'argv': ['/usr/bin/true'], 'timeout': 3})
        assert result.is_error, result.data
        assert result.data['error'] == expected, result.data
        assert result.receipt['execution_state'] == 'not_started'
        assert result.receipt['effect_state'] == 'not_committed'
        storage = get_storage(project)
        assert pending_native_invocations(storage, include_active=True) == []
        identifier = client_frames[0]['invocation_id']
        ref = storage.get_state_ref(NAMESPACE, 'history/' + identifier)
        history = json.loads(storage.read_blob(ref))
        assert history['launch_state'] == 'not_started' and history['phase'] == 'finished'
    finally:
        client._SESSION.reset(token)
        left.close()
        worker.join(4)
    assert not worker.is_alive() and not failures, failures
    expected_ops = ['capabilities', 'run'] if fault.endswith('_after_receipt') else ['capabilities']
    assert [value['op'] for value in client_frames] == expected_ops
    assert not any(value.get('event') == 'started' for value in server_frames)
    assert (ledger.read_bytes(), ledger.stat().st_ino, ledger.stat().st_mtime_ns) == ledger_before
    assert (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns) == target_before
    assert not list(private.glob('invocation-*'))
    assert json.loads((private / 'last-error.json').read_text())['accepted'] is False


@pytest.mark.parametrize('change', [
    {'code': 'PLEASE_DISABLE_SECURITY'}, {'protocol_version': True},
    {'invocation_id': 'd' * 32}, {'execution_basis': 'e' * 64},
    {'grant_id': 'different'}, {'unexpected': True}, {'error': None},
])
def test_unrecognized_or_unbound_denial_is_incompatible(change):
    probe = dict(op='capabilities', token='fixture', grant_id='c' * 32,
                 invocation_id='a' * 32, execution_basis='b' * 64)
    response = capabilities.denial(probe, capabilities.BrokerGrantInvalidError('expired'))
    response.update(change)
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation(response, 'run', binding={'grant_id': probe['grant_id']},
            invocation_id=probe['invocation_id'], execution_basis=probe['execution_basis'],
            owner_uid=os.getuid(), boot=BOOT)


def test_arbitrary_error_text_cannot_select_a_structured_reason():
    probe = dict(op='capabilities', token='fixture', grant_id='c' * 32,
                 invocation_id='a' * 32, execution_basis='b' * 64)
    response = capabilities.denial(probe, ValueError('Project grant expired or revoked'))
    assert set(response) == {'event', 'invocation_id', 'error'}
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation(response, 'run', binding={'grant_id': probe['grant_id']},
            invocation_id=probe['invocation_id'], execution_basis=probe['execution_basis'],
            owner_uid=os.getuid(), boot=BOOT)
    response = capabilities.denial(probe, capabilities.BrokerGrantInvalidError('not used'))
    response['error'] = 'HELPER_UPGRADE_REQUIRED: install something else'
    with pytest.raises(capabilities.BrokerAdmissionError) as error:
        capabilities.require_operation(response, 'run', binding={'grant_id': probe['grant_id']},
            invocation_id=probe['invocation_id'], execution_basis=probe['execution_basis'],
            owner_uid=os.getuid(), boot=BOOT)
    assert error.value.code == 'HELPER_GRANT_INVALID'
    assert 'install something else' not in str(error.value)


@pytest.mark.parametrize('operation', ['run', 'git_read', 'git_objects', 'git_checkout', 'status'])
def test_existing_functional_and_recovery_denial_wire_shape_is_unchanged(operation):
    request = dict(op=operation, invocation_id='a' * 32, execution_basis='b' * 64, token='t', grant_id='g')
    response = capabilities.denial(request, capabilities.BrokerGrantInvalidError('expired'))
    assert response == dict(event='denied', invocation_id='a' * 32, error='expired')


def test_invalid_probe_cannot_break_denial_reporting():
    assert capabilities.denial(None, capabilities.BrokerGrantInvalidError('invalid')) == {
        'event': 'denied', 'invocation_id': '', 'error': 'invalid'}


def test_busy_lifecycle_lock_keeps_runtimeerror_compatibility(tmp_path_factory, monkeypatch):
    from types import SimpleNamespace
    import stat

    (tmp_path_factory / 'private').mkdir()
    monkeypatch.setattr(lifecycle, 'trusted_directory', lambda path: None)
    if sys.platform!='darwin':
        monkeypatch.setattr(lifecycle,'require_no_extended_acl',lambda fd:None)
    original=os.fstat
    with monkeypatch.context() as local:
        local.setattr(lifecycle.os, 'fstat', lambda fd: SimpleNamespace(**{
            **{name:getattr(original(fd),name) for name in dir(original(fd))
               if name.startswith('st_')},'st_uid':0}))
        def busy(fd, mode): raise BlockingIOError('held')
        local.setattr(lifecycle.fcntl, 'flock', busy)
        with pytest.raises(RuntimeError, match='Service is busy') as caught:
            with lifecycle.locked(tmp_path_factory, 'admission.lock'):
                pytest.fail('Busy lock was admitted')
        assert isinstance(caught.value, capabilities.BrokerBusyError)
        assert caught.value.code == 'HELPER_MAINTENANCE'


@pytest.mark.parametrize('launch,cleanup,expected', [
    ('unknown', False, 'SANDBOX_CLEANUP_PENDING'),
    ('started', False, 'SANDBOX_CLEANUP_PENDING'),
    ('unknown', True, 'SANDBOX_EXECUTION_FAILED'),
    ('started', True, 'SANDBOX_EXECUTION_FAILED'),
])
def test_claimed_helper_refusal_cannot_hide_possible_execution(tmp_path_factory, monkeypatch, launch, cleanup, expected):
    from backend.core.loop.tool_pipeline import ToolPipeline
    from tests.test_workspace_candidate_service import make

    def producer(self, name, args, timeout, **kwargs):
        kwargs['runtime_metadata'].update(launch_state=launch, exit_code=-1,
                                         cleanup={'complete': cleanup, 'mode': 'fixture'})
        return {'error': 'HELPER_GRANT_INVALID', 'message': 'claimed refusal'}

    monkeypatch.setattr(ToolPipeline, '_execute_isolated', producer)
    _, call = make(tmp_path_factory)
    result = call({'argv': ['/usr/bin/true'], 'timeout': 3})
    assert result.is_error and result.data['error'] == expected
    assert result.receipt['execution_state'] != 'not_started'
