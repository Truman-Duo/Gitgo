"""Directory-service-free CF initialization must use the dedicated identity."""
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from backend.core import macos_identity as identity


@pytest.mark.parametrize('uid',[200000001,209999999])
@pytest.mark.parametrize('inherited',[None,'0x1F5:0:0','0x0:0:0'])
def test_launch_replaces_controller_encoding_hint_without_mutating_environment(monkeypatch,uid,inherited):
    monkeypatch.setattr(identity, 'os', SimpleNamespace(geteuid=lambda:0))
    popen=Mock();monkeypatch.setattr(identity.subprocess,'Popen',popen)
    env={'PATH':'/usr/bin:/bin'}
    if inherited is not None:env['__CF_USER_TEXT_ENCODING']=inherited
    original=dict(env)
    identity.low_spawn(['/bin/true'],uid,'/',env)
    args=popen.call_args.kwargs
    assert args['env']=={**original,'__CF_USER_TEXT_ENCODING':f'0x{uid:X}:0:0'}
    assert args['user']==args['group']==uid and args['extra_groups']==()
    assert env==original
