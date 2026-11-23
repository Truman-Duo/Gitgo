"""Temporary broker transport budget, independent of privileged execution."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import sys
import pytest
if sys.platform!='darwin':pytest.skip('Darwin peer credentials',allow_module_level=True)
from backend.core import macos_broker_service as server,macos_broker_protocol as wire
from scripts.check_macos_git_broker import expected_calls,worktree_fault_modes


def test_split_batches_preserve_worktree_cases_and_leave_transport_room():
    assert worktree_fault_modes(False)==('reserved','building','prepared','published','retiring','unrecorded')
    assert worktree_fault_modes(True)==()
    complete=expected_calls(True,True,True,True)
    results=expected_calls(True,True,True,True,False,True)
    assert complete==23 and results==57
    assert expected_calls(True,True,True,True,results=True,seal=True)==40
    assert expected_calls(True,True,True,True,results=True,seal=True,runtime_merge=True)==30
    assert expected_calls(True,True,True,True,compare=True)==29
    assert expected_calls(True,True,True,compare=True)==20
    # One readiness connection plus three negative probes also consume slots.
    assert max(complete,results)+1+3<=61
    assert expected_calls(True,True,True,True,True)==40


def test_real_listener_counts_denied_readiness_against_64_connections(monkeypatch):
    # Exercise the actual listen loop and kernel peer lookup as an ordinary
    # user. No root helper, execution admission, or process launch is used.
    with tempfile.TemporaryDirectory(prefix='gitgo-budget-',dir='/private/tmp') as directory:
        root=Path(directory);(root/'private').mkdir();errors=[];deadline=time.monotonic()+10
        ready=threading.Event()
        def grant(*a,**kw):
            if time.monotonic()>deadline:raise TimeoutError('test listener deadline')
            return {'owner_uid':os.getuid()+1}
        # chown to another identity is privileged and irrelevant to this test.
        monkeypatch.setattr(server,'grant',grant)
        monkeypatch.setattr(server.os,'chown',lambda *a,**k:None)
        def run():
            try:server.listen(root,on_ready=ready.set)
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=run,daemon=True);thread.start()
        address=root/'broker.sock'
        try:
            # bind() creates the socket path before listen() can accept. Use
            # the real readiness callback, without spending a connection slot.
            assert ready.wait(5),errors
            assert not errors,errors
            for _ in range(64):
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                    peer.settimeout(2);peer.connect(str(address))
                    assert wire.receive(peer)['event']=='denied'
            thread.join(2);assert not thread.is_alive() and not errors
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                peer.settimeout(1)
                with pytest.raises(OSError):peer.connect(str(address))
            assert json.loads((root/'private/seen.json').read_text())==[]
            assert json.loads((root/'private/last-error.json').read_text())['detail']=='Wrong peer user'
        finally:
            deadline=0;thread.join(2)


def test_finalization_faults_split_before_the_connection_budget():
    pending=expected_calls(True,True,True,True,results=True,seal=True,runtime_merge=True,finalize='pending')
    ack=expected_calls(True,True,True,True,results=True,seal=True,runtime_merge=True,finalize='ack')
    assert pending==53 and ack==42
    assert pending+4<64 and ack+4<64


def test_automatic_boundary_batch_keeps_transport_headroom():
    assert expected_calls(True,True,True,True,results=True,seal=True,runtime_merge=True,finalize='boundaries')==58
