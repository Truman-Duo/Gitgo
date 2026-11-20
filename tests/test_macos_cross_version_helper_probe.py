"""Fault harness plumbing with a socket peer; real privileged evidence is VM-only."""
from contextlib import contextmanager
import os
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from scripts import macos_cross_version_helper_probe as probe


@pytest.mark.skipif(sys.platform != 'darwin', reason='Actual macOS ToolPipeline dispatch')
def test_probe_loses_only_started_frame_and_retains_unknown_outcome_after_recovery(tmp_path_factory, monkeypatch):
    from backend.core import macos_broker_client as client, macos_broker_protocol as wire
    from backend.core import macos_broker_capabilities as capabilities, macos_identity as identity
    from backend.core.storage import get_storage
    from tests.test_workspace_candidate_service import make

    project = tmp_path_factory / 'project'
    project.mkdir()
    (project / 'fixture.txt').write_bytes(b'approved fixture\n')
    descriptor = wire.open_directory(project)
    binding = dict(project=str(project), identity=wire.directory_identity(descriptor),
                   token='fixture', grant_id='fixture', socket='fixture')
    os.close(descriptor)
    left, right = socket.socketpair()
    right.settimeout(5)
    boot = '7e130791-3c47-4d94-9149-758b1659e0ee'
    actions, errors = [], []

    class Connection:
        def connect(self, address):
            assert address == 'fixture'

        def __getattr__(self, name):
            return getattr(left, name)

    @contextmanager
    def session(service, root):
        assert service == 'fixture' and root == project
        token = client._SESSION.set(binding)
        try:
            yield
        finally:
            client._SESSION.reset(token)

    def context(args):
        ctx, call = make(project)
        store = get_storage(project)
        ctx.storage = store
        return dict(version='fixture'), project, store, ctx.process, lambda index: call({
            'argv': ['/usr/bin/printf', 'gitgo-ack-loss-fixture'], 'timeout': 5,
        }, index)

    def peer():
        fd = None
        try:
            fd = wire.receive_directory(right)
            request = wire.receive(right)
            wire.send(right, capabilities.reply(request, {**binding, 'owner_uid': os.getuid()}, boot=boot))
            action = wire.receive(right)
            actions.append(action['invocation_id'])
            assert action['op'] == 'run'
            wire.send(right, dict(event='started', invocation_id=action['invocation_id'], pid=os.getpid()))
            assert right.recv(1) == b''
        except BaseException as exc:
            errors.append(exc)
        finally:
            if fd is not None:
                os.close(fd)
            right.close()

    monkeypatch.setattr(client.socket, 'socket', lambda *a, **k: Connection())
    monkeypatch.setattr(wire, 'peer_uid', lambda connection: 0)
    monkeypatch.setattr(identity, 'boot_id', lambda: boot)
    monkeypatch.setattr(client, 'resident_project_session', session)
    monkeypatch.setattr(probe, 'exec_context', context)
    worker = threading.Thread(target=peer)
    worker.start()
    args = SimpleNamespace(service='fixture', workspace=str(project), expected_version='fixture')
    try:
        lost = probe.exec_ack_loss(args)
        assert lost['passed'] and lost['original_record']['launch_state'] == 'unknown'
        assert lost['lost_started']['invocation_id'] == actions[0]
        assert len(lost['functional_requests']) == 1 and lost['expected_outcome_debt']
        identifier = lost['invocation_id']
        # Substitute only a read-only helper cleanup receipt. The reconciler,
        # immutable original record, pending debt and Pipeline retries are real.
        monkeypatch.setattr(client, 'recovered_cleanup', lambda item: dict(
            cleanup=dict(complete=True, mode='macos_uid', invocation_id=identifier, issues=[]), outcome='unknown'))
        args.invocation_id, args.original_ref = identifier, lost['original_ref']
        recovered = probe.exec_recover(args)
        assert recovered['passed'] and recovered['original_retained'] and recovered['history_not_invented']
        assert recovered['pending'][0]['code'] == 'NATIVE_OUTCOME_UNCONFIRMED'
        assert recovered['no_replay'] and actions == [identifier]
        assert (project / 'fixture.txt').read_bytes() == b'approved fixture\n'
    finally:
        left.close()
        worker.join(5)
    assert not worker.is_alive() and not errors, errors
