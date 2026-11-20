"""Per-connection compatibility faults without root or native execution.

Server tests keep real descriptor/path authorization. Client tests keep the
framed socket/FD protocol and native invocation journal, but inject a peer.
These are protocol regressions, not administrator isolation acceptance.
"""
from copy import deepcopy
import json
import os
import socket
import struct
import sys
import threading
import time

import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX broker transport', allow_module_level=True)

from backend.core import macos_broker_capabilities as capabilities
from backend.core import macos_broker_client as client
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as server
from backend.core import macos_identity as identity
from backend.core.storage.native_invocations import (
    NAMESPACE, NativeInvocationRecord, pending_native_invocations,
)
from tests.test_macos_broker import request_fixture
from tests.test_native_invocation_recovery import bound_store, invocation


BOOT = '6e130791-3c47-4d94-9149-758b1659e0ee'


@pytest.fixture
def negotiation(tmp_path_factory, monkeypatch):
    project = tmp_path_factory / 'project'
    project.mkdir()
    root = tmp_path_factory / 'broker'
    (root / 'private').mkdir(parents=True)
    ledger = root / 'private' / 'seen.json'
    ledger.write_text(json.dumps(['f' * 32]))
    before = (ledger.read_bytes(), ledger.stat().st_ino, ledger.stat().st_mtime_ns)
    fd, approved, action = request_fixture(project)
    approved.update(boot=BOOT, expires=200, revoked=False)
    probe = {key: value for key, value in action.items()
             if key in capabilities.CAPABILITY_REQUEST_FIELDS}
    probe['op'] = capabilities.CAPABILITY_OPERATION
    frames = []
    calls = []
    monkeypatch.setattr(wire, 'peer_uid', lambda _: os.getuid())
    monkeypatch.setattr(identity, 'boot_id', lambda: BOOT)
    monkeypatch.setattr(server, 'raw_time', lambda: 100)
    monkeypatch.setattr(server, 'private_directory', lambda path: None)

    def read_grant(path, *args, **kwargs):
        assert path == root / 'private' / 'grant.json'
        calls.append('read_live_grant')
        return json.dumps(approved)

    def forbid_write(*args, **kwargs):
        pytest.fail('Capability negotiation must not reserve an invocation or write state')

    monkeypatch.setattr(identity, 'bounded_read', read_grant)
    monkeypatch.setattr(identity, 'atomic_json', forbid_write)
    monkeypatch.setattr(wire, 'send', lambda connection, value: frames.append(deepcopy(value)))
    monkeypatch.setattr(wire, 'receive_until', lambda connection, deadline: deepcopy(action))
    try:
        yield dict(root=root, project=project, fd=fd, approved=approved,
                   action=action, probe=probe, frames=frames, calls=calls)
    finally:
        os.close(fd)
        assert (ledger.read_bytes(), ledger.stat().st_ino, ledger.stat().st_mtime_ns) == before
        assert sorted(path.name for path in (root / 'private').iterdir()) == ['seen.json']


def test_negotiation_keeps_original_authority_and_does_not_reserve(negotiation):
    item = negotiation
    identifier = server.authorize(None, item['fd'], item['probe'], item['approved'])
    action, approved = server.negotiate(None, item['fd'], item['probe'], item['root'], item['approved'])
    assert identifier == action['invocation_id'] == item['probe']['invocation_id']
    assert action == item['action'] and approved == item['approved']
    assert item['calls'] == ['read_live_grant']
    assert len(item['frames']) == 1
    capabilities.require_operation(item['frames'][0], 'run', binding=item['approved'],
                                   invocation_id=identifier,
                                   execution_basis=item['probe']['execution_basis'],
                                   owner_uid=os.getuid(), boot=BOOT)


@pytest.mark.parametrize('change', [
    {'invocation_id': 'c' * 32}, {'execution_basis': 'd' * 64},
    {'token': 'different'}, {'grant_id': 'new-grant'},
    {'op': 'capabilities'}, {'op': 'status'}, {'op': 'cancel'}, {'op': 'unknown'},
    {'invocation_id': True}, {'execution_basis': None},
])
def test_post_handshake_request_cannot_change_original_binding(negotiation, change):
    item = negotiation
    item['action'].update(change)
    server.authorize(None, item['fd'], item['probe'], item['approved'])
    with pytest.raises((ValueError, PermissionError)):
        server.negotiate(None, item['fd'], item['probe'], item['root'], item['approved'])
    assert len(item['frames']) == 1
    assert item['calls'] == []


@pytest.mark.parametrize('fault', ['expired', 'revoked', 'new_boot', 'renewed_grant', 'renewed_token', 'project_replaced'])
def test_live_authorization_is_rechecked_after_capability_receipt(negotiation, monkeypatch, fault):
    item = negotiation
    original = deepcopy(item['approved'])
    server.authorize(None, item['fd'], item['probe'], original)

    def changed_authority(connection, deadline):
        assert len(item['frames']) == 1
        if fault == 'expired':
            item['approved']['expires'] = 100
        elif fault == 'revoked':
            item['approved']['revoked'] = True
        elif fault == 'new_boot':
            item['approved']['boot'] = '7e130791-3c47-4d94-9149-758b1659e0ee'
        elif fault == 'renewed_grant':
            item['approved']['grant_id'] = 'new-grant'
        elif fault == 'renewed_token':
            item['approved']['token_hash'] = 'a' * 64
        elif fault == 'project_replaced':
            item['project'].rename(item['project'].with_name('old-project'))
            item['project'].mkdir()
        return deepcopy(item['action'])

    monkeypatch.setattr(wire, 'receive_until', changed_authority)
    with pytest.raises((ValueError, PermissionError)):
        server.negotiate(None, item['fd'], item['probe'], item['root'], original)
    assert item['calls'] == ['read_live_grant']


def test_existing_functional_request_does_not_require_new_client_protocol(negotiation):
    item = negotiation
    server.authorize(None, item['fd'], item['action'], item['approved'])
    action, approved = server.negotiate(None, item['fd'], item['action'], item['root'], item['approved'])
    assert action is item['action'] and approved is item['approved']
    assert item['frames'] == [] and item['calls'] == []


def test_service_limits_negotiated_action_wait_without_reserving(negotiation, monkeypatch):
    item = negotiation
    began = time.monotonic()

    def deadline_exceeded(connection, deadline):
        assert began + 5 <= deadline <= time.monotonic() + 5
        raise wire.ReceiveDeadlineExceeded('metadata peer stalled')

    monkeypatch.setattr(wire, 'receive_until', deadline_exceeded)
    with pytest.raises(wire.ReceiveDeadlineExceeded):
        server.negotiate(None, item['fd'], item['probe'], item['root'], item['approved'])
    assert len(item['frames']) == 1 and item['calls'] == []


@pytest.mark.parametrize('phase', ['header', 'body'])
def test_drip_bytes_cannot_restart_the_absolute_handshake_deadline(phase):
    left, right = socket.socketpair()
    left.settimeout(3)
    stop = threading.Event()
    failures = []
    raw = json.dumps({'payload': 'a' * 40}).encode()

    def drip():
        try:
            if phase == 'body':
                right.sendall(struct.pack('!I', len(raw)))
                fragments = raw
            else:
                fragments = struct.pack('!I', len(raw))
            for byte in fragments:
                right.sendall(bytes([byte]))
                if stop.wait(.06):
                    break
        except OSError:
            if not stop.is_set():
                failures.append('peer failed before cancellation')
        finally:
            right.close()

    worker = threading.Thread(target=drip)
    began = time.monotonic()
    worker.start()
    try:
        with pytest.raises(wire.ReceiveDeadlineExceeded):
            wire.receive_until(left, began + .14)
        elapsed = time.monotonic() - began
        assert .1 <= elapsed < .75
        assert left.gettimeout() == 3
    finally:
        stop.set()
        left.close()
        worker.join(1)
    assert not worker.is_alive() and not failures


def test_complete_fragmented_frame_keeps_existing_socket_configuration():
    left, right = socket.socketpair()
    left.settimeout(.3)
    value = {'event': 'capabilities', 'protocol_version': 1}
    raw = json.dumps(value).encode()
    frame = struct.pack('!I', len(raw)) + raw

    def send():
        try:
            for offset in range(0, len(frame), 3):
                right.sendall(frame[offset:offset + 3])
        finally:
            right.close()

    worker = threading.Thread(target=send)
    worker.start()
    try:
        assert wire.receive_until(left, time.monotonic() + 1) == value
        assert left.gettimeout() == .3 and left.getblocking()
    finally:
        left.close()
        worker.join(1)
    assert not worker.is_alive()


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'[]'])
def test_deadline_receive_preserves_strict_json_contract(raw):
    left, right = socket.socketpair()
    try:
        right.sendall(struct.pack('!I', len(raw)) + raw)
        with pytest.raises(ValueError):
            wire.receive_until(left, time.monotonic() + 1)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize('size', [0, wire.MAX_FRAME + 1])
def test_deadline_receive_rejects_frame_size_before_body(size):
    left, right = socket.socketpair()
    try:
        right.sendall(struct.pack('!I', size))
        with pytest.raises(ValueError, match='bounds'):
            wire.receive_until(left, time.monotonic() + 1)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize('fault', [
    'old_helper', 'malformed', 'revision', 'grant', 'invocation', 'basis',
    'project', 'owner', 'boot', 'cancel',
])
def test_client_rejects_handshake_before_sending_action(bound_store, monkeypatch, fault):
    project, storage = bound_store
    fd = wire.open_directory(project)
    binding = dict(project=str(project), identity=wire.directory_identity(fd),
                   grant_id='c' * 32, token='fixture-secret', socket='fixture')
    record = NativeInvocationRecord(storage, project, invocation(project))
    record.prepare()
    observation = dict(native_record=record, launch_state='not_started')
    cancellation = threading.Event()
    left, right = socket.socketpair()
    right.settimeout(3)
    failures = []
    messages = []
    trailing = []

    class Connection:
        def connect(self, address):
            assert address == binding['socket']

        def __getattr__(self, name):
            return getattr(left, name)

    def peer():
        received = None
        try:
            received = wire.receive_directory(right)
            probe = wire.receive(right)
            messages.append(probe)
            assert set(probe) == capabilities.CAPABILITY_REQUEST_FIELDS
            assert probe['op'] == 'capabilities'
            assert wire.directory_identity(received) == binding['identity']
            approved = {**binding, 'owner_uid': os.getuid()}
            response = capabilities.reply(probe, approved, boot=BOOT)
            if fault == 'old_helper':
                response = dict(event='denied', invocation_id=probe['invocation_id'],
                                error='Unsupported broker operation or fields')
            elif fault == 'malformed':
                response = {'event': 'capabilities'}
            elif fault == 'revision':
                response['operation_contracts']['run'] = 2
            elif fault == 'grant':
                response['grant_id'] = 'd' * 32
            elif fault == 'invocation':
                response['invocation_id'] = 'd' * 32
            elif fault == 'basis':
                response['execution_basis'] = 'd' * 64
            elif fault == 'project':
                response['project_identity'] = [0, 0, os.getuid()]
            elif fault == 'owner':
                response['owner_uid'] = os.getuid() + 1
            elif fault == 'boot':
                response['boot'] = '7e130791-3c47-4d94-9149-758b1659e0ee'
            wire.send(right, response)
            # Any functional request (or speculative retry) is a regression.
            trailing.append(right.recv(1))
        except BaseException as error:
            failures.append(error)
        finally:
            if received is not None:
                os.close(received)
            right.close()

    if fault == 'cancel':
        require = capabilities.require_operation

        def cancel_after_receipt(*args, **kwargs):
            require(*args, **kwargs)
            cancellation.set()

        monkeypatch.setattr(capabilities, 'require_operation', cancel_after_receipt)
    monkeypatch.setattr(client.socket, 'socket', lambda *args, **kwargs: Connection())
    monkeypatch.setattr(wire, 'peer_uid', lambda connection: 0)
    monkeypatch.setattr(identity, 'boot_id', lambda: BOOT)
    worker = threading.Thread(target=peer)
    worker.start()
    result = None
    failure = None
    try:
        try:
            result = client._exchange(binding, fd, dict(op='run', timeout=5),
                                      observation, cancellation, 5)
        except capabilities.BrokerCompatibilityError as error:
            failure = error
    finally:
        left.close()
        worker.join(4)
        os.close(fd)
        record.finish(observation)
    assert not worker.is_alive() and not failures, failures
    assert len(messages) == 1 and trailing == [b'']
    assert observation['launch_state'] == 'not_started'
    assert not observation.get('started') and observation.get('cleanup') is None
    if fault == 'cancel':
        assert failure is None and result.success is False
        assert result.effect_state == 'not_committed' and 'cancel' in result.error
    elif failure is None:
        assert result.success is False and result.effect_state == 'not_committed'
        assert 'HELPER_UPGRADE_REQUIRED' in result.error
    assert not record.failed
    assert pending_native_invocations(storage, include_active=True) == []
    ref = storage.get_state_ref(NAMESPACE, 'history/' + record.record['invocation_id'])
    saved = json.loads(storage.read_blob(ref))
    assert saved['launch_state'] == 'not_started' and saved['phase'] == 'finished'


@pytest.mark.parametrize('reason', ['cancel', 'deadline'])
def test_silent_handshake_is_cancellable_and_bounded_without_sending_action(bound_store, monkeypatch, reason):
    project, storage = bound_store
    fd = wire.open_directory(project)
    binding = dict(project=str(project), identity=wire.directory_identity(fd),
                   grant_id='c' * 32, token='fixture-secret', socket='fixture')
    record = NativeInvocationRecord(storage, project, invocation(project))
    record.prepare()
    observation = dict(native_record=record, launch_state='not_started')
    cancellation = threading.Event()
    left, right = socket.socketpair()
    right.settimeout(1)
    frames = []
    trailing = []
    failures = []

    class Connection:
        def connect(self, address):
            assert address == 'fixture'

        def __getattr__(self, name):
            return getattr(left, name)

    def silent_peer():
        received = None
        try:
            received = wire.receive_directory(right)
            frames.append(wire.receive(right))
            if reason == 'cancel':
                cancellation.set()
            trailing.append(right.recv(1))
        except BaseException as error:
            failures.append(error)
        finally:
            if received is not None:
                os.close(received)
            right.close()

    monkeypatch.setattr(client.socket, 'socket', lambda *args, **kwargs: Connection())
    monkeypatch.setattr(wire, 'peer_uid', lambda connection: 0)
    monkeypatch.setattr(identity, 'boot_id', lambda: BOOT)
    worker = threading.Thread(target=silent_peer)
    worker.start()
    began = time.monotonic()
    try:
        result = client._exchange(binding, fd, dict(op='run', timeout=1), observation,
                                  cancellation, .15 if reason == 'deadline' else 5)
        assert time.monotonic() - began < .75
        assert result.success is False and result.effect_state == 'not_committed'
        assert result.timed_out is (reason == 'deadline')
        assert ('cancelled' if reason == 'cancel' else 'timed out') in result.error
        assert observation['launch_state'] == 'not_started' and not observation.get('started')
    finally:
        record.finish(observation)
        left.close()
        worker.join(2)
        os.close(fd)
    assert not worker.is_alive() and not failures, failures
    assert len(frames) == 1 and frames[0]['op'] == 'capabilities'
    assert trailing == [b'']
    assert not record.failed and pending_native_invocations(storage, include_active=True) == []
    ref = storage.get_state_ref(NAMESPACE, 'history/' + record.record['invocation_id'])
    assert json.loads(storage.read_blob(ref))['launch_state'] == 'not_started'


@pytest.mark.skipif(sys.platform != 'darwin', reason='Actual macOS pipeline dispatch')
@pytest.mark.parametrize('loss', ['before_started', 'after_started'])
def test_pipeline_keeps_remote_response_loss_pending_and_blocks_replay(tmp_path_factory, monkeypatch, loss):
    from backend.core.storage import get_storage
    from tests.test_workspace_candidate_service import make

    project = tmp_path_factory / 'project'
    project.mkdir()
    (project / 'input.txt').write_text('original')
    fd = wire.open_directory(project)
    binding = dict(project=str(project), identity=wire.directory_identity(fd),
                   token='fixture', grant_id='fixture', socket='fixture')
    os.close(fd)
    left, right = socket.socketpair()
    right.settimeout(3)
    actions = []
    connections = []
    failures = []

    class Connection:
        def connect(self, address):
            assert address == 'fixture'
            connections.append(address)

        def __getattr__(self, name):
            return getattr(left, name)

    def peer():
        received = None
        try:
            received = wire.receive_directory(right)
            probe = wire.receive(right)
            wire.send(right, capabilities.reply(probe, {**binding, 'owner_uid': os.getuid()}, boot=BOOT))
            action = wire.receive(right)
            actions.append(action)
            assert action['op'] == 'run'
            if loss == 'after_started':
                wire.send(right, dict(event='started', invocation_id=action['invocation_id'], pid=os.getpid()))
            # The service could already have reserved/launched this invocation.
            # Disconnecting does not prove either no launch or completed cleanup.
        except BaseException as error:
            failures.append(error)
        finally:
            if received is not None:
                os.close(received)
            right.close()

    monkeypatch.setattr(client.socket, 'socket', lambda *args, **kwargs: Connection())
    monkeypatch.setattr(wire, 'peer_uid', lambda connection: 0)
    monkeypatch.setattr(identity, 'boot_id', lambda: BOOT)
    token = client._SESSION.set(binding)
    worker = threading.Thread(target=peer)
    worker.start()
    try:
        _, call = make(project)
        first = call({'argv': ['/usr/bin/printf', 'fixture'], 'timeout': 5})
        storage = get_storage(project)
        debt = pending_native_invocations(storage, include_active=True)
        assert first.is_error and len(debt) == 1
        assert debt[0]['launch_state'] == ('started' if loss == 'after_started' else 'unknown')
        assert debt[0]['effect_state'] == 'ambiguous'
        assert first.receipt['runtime_cleanup']['complete'] is False
        identifier = actions[0]['invocation_id']
        assert debt[0]['invocation_id'] == identifier
        assert storage.get_state_ref(NAMESPACE, 'history/' + identifier) is None
        before = storage.get_state_ref(NAMESPACE, 'pending/' + identifier)
        second = call({'argv': ['/usr/bin/printf', 'fixture'], 'timeout': 5}, index=1)
        assert second.is_error
        assert storage.get_state_ref(NAMESPACE, 'pending/' + identifier) == before
        assert pending_native_invocations(storage, include_active=True) == debt
        assert len(actions) == 1 and connections == ['fixture']
        assert (project / 'input.txt').read_text() == 'original'
    finally:
        client._SESSION.reset(token)
        left.close()
        worker.join(4)
    assert not worker.is_alive() and not failures, failures
