"""Do not confuse asynchronous job removal with a failed or unauthorized query."""
from types import SimpleNamespace
import pytest
from scripts import check_macos_user_broker as probe


def test_waits_for_disappearing_job(monkeypatch):
    replies=iter([(0,''),(0,''),(113,'Could not find service "test"')]);seen=[]
    def query(*args):
        seen.append(args);code,error=next(replies)
        return SimpleNamespace(returncode=code,stderr=error)
    monkeypatch.setattr(probe,'launchctl',query)
    assert probe.wait_unloaded('system/test',seconds=1)
    assert seen==[('print','system/test')]*3


def test_running_job_times_out(monkeypatch):
    monkeypatch.setattr(probe,'launchctl',lambda *a:SimpleNamespace(returncode=0,stderr=''))
    with pytest.raises(TimeoutError,match='not confirmed'):probe.wait_unloaded('system/test',seconds=.01)


@pytest.mark.parametrize('code,error',[(1,'Operation not permitted'),(113,'unknown error')])
def test_query_errors_are_not_absence(monkeypatch,code,error):
    monkeypatch.setattr(probe,'launchctl',lambda *a:SimpleNamespace(returncode=code,stderr=error))
    with pytest.raises(RuntimeError,match='Cannot verify'):probe.wait_unloaded('system/test')
