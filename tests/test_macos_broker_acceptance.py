"""Service readiness must tolerate cold imports without relaxing task limits."""
from types import SimpleNamespace
import sys
import pytest
if sys.platform=='win32': pytest.skip('POSIX acceptance driver',allow_module_level=True)
from scripts import check_macos_user_broker as harness


@pytest.fixture
def readiness(monkeypatch):
    clock=SimpleNamespace(now=0.,ready_at=12.,closed=0,peer=0,reply='denied',eof=False,
        status='state = running\n')
    monkeypatch.setattr(harness.time,'monotonic',lambda:clock.now)
    monkeypatch.setattr(harness.time,'sleep',lambda duration:setattr(clock,'now',clock.now+duration))
    class Probe:
        def settimeout(self,value): pass
        def connect(self,address):
            if clock.now<clock.ready_at: raise FileNotFoundError('still loading')
        def close(self): clock.closed+=1
    monkeypatch.setattr(harness.socket,'socket',lambda *a:Probe())
    monkeypatch.setattr(harness.wire,'peer_uid',lambda _:clock.peer)
    def reply(_):
        if clock.eof: raise EOFError('stale endpoint')
        return dict(event=clock.reply)
    monkeypatch.setattr(harness.wire,'receive',reply)
    monkeypatch.setattr(harness.wire,'send',lambda *a:pytest.fail('readiness must not submit a command'))
    monkeypatch.setattr(harness,'launchctl',lambda *a:SimpleNamespace(returncode=0,stdout=clock.status,stderr=''))
    return clock


def test_cold_service_has_separate_startup_allowance(readiness,tmp_path_factory):
    elapsed=harness.wait_broker(tmp_path_factory,'fixture')
    assert 12<=elapsed<13 and readiness.closed>100


def test_early_exit_is_reported_without_waiting_entire_deadline(readiness,tmp_path_factory):
    readiness.status='state = not running\nlast exit code = 1\n'
    with pytest.raises(RuntimeError,match='exited before readiness'):
        harness.wait_broker(tmp_path_factory,'fixture')
    assert readiness.now<1 and readiness.closed==1


@pytest.mark.parametrize('fault',['wrong_peer','wrong_reply','stale_socket'])
def test_socket_existence_does_not_prove_readiness(readiness,tmp_path_factory,fault):
    readiness.ready_at=0
    if fault=='wrong_peer': readiness.peer=501
    elif fault=='wrong_reply': readiness.reply='started'
    else: readiness.eof=True
    with pytest.raises(TimeoutError if fault=='stale_socket' else RuntimeError):
        harness.wait_broker(tmp_path_factory,'fixture',seconds=2)
    assert readiness.closed>0
    if fault=='stale_socket': assert readiness.now>=2
