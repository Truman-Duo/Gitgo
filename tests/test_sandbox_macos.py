"""Candidate integration contracts; mocks do not establish OS isolation."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import sys
if sys.platform == "win32":
    pytest.skip("POSIX candidate module", allow_module_level=True)
from backend.core.sandbox import SandboxDenied
from backend.core import sandbox_macos as mac
from backend.core import macos_identity as ident


@pytest.mark.skipif(sys.platform!='darwin',reason='Actual Seatbelt signal boundary')
def test_same_sandbox_child_signals_and_real_runner_timeout(tmp_path_factory):
    import os
    import subprocess
    from backend.core.sandbox import trusted_runtime_roots, sandbox_environment
    from backend.core.child_process import tool_runner_command, owned_child_cwd
    from backend.core.tools.runner import handler_bindings
    handler_bindings()
    work=tmp_path_factory
    policy=mac.profile(work,trusted_runtime_roots())
    outside=subprocess.Popen(['/bin/sleep','20'])
    try:
        os.kill(outside.pid,0)  # Positive control: same UID can normally signal it.
        probe='''import os,subprocess,sys
p=subprocess.Popen(['/bin/sleep','20'])
p.terminate()
assert p.wait(timeout=3)==-15
try: os.kill(int(sys.argv[1]),0)
except PermissionError: print('child-allowed-peer-denied')
else: raise AssertionError('Signal crossed sandbox boundary')
'''
        # Use the Python whose roots the Host selected above. Apple's
        # /usr/bin/python3 can dispatch into an unrelated Xcode installation;
        # admitting that tree would change the policy under test.
        r=subprocess.run(['/usr/bin/sandbox-exec','-p',policy,sys.executable,'-I','-c',probe,str(outside.pid)],
            cwd=work,capture_output=True,text=True,timeout=6)
        assert r.returncode==0 and r.stdout.strip()=='child-allowed-peer-denied',r.stderr
        source=Path(__file__).resolve().parents[1]
        payload={'tool_name':'exec_command','args':{'_workspace':str(work),
            'argv':['/bin/sleep','20'],'timeout':1},'_native_sandbox_workspace':str(work)}
        r=subprocess.run(['/usr/bin/sandbox-exec','-p',policy,*tool_runner_command()],
            cwd=owned_child_cwd(source),env=sandbox_environment({}),input=json.dumps(payload),
            capture_output=True,text=True,timeout=9)
        assert r.returncode==0,r.stderr
        data=json.loads(r.stdout)
        assert data.get('success') is True and data.get('data',{}).get('error')=='COMMAND_TIMEOUT',(data,r.stderr)
        assert 'PermissionError' not in r.stderr
    finally:
        outside.kill();outside.wait(timeout=3)


def test_ordinary_host_cannot_enable_with_environment(monkeypatch):
    monkeypatch.setenv('GITGO_MACOS_EXECUTION','1')
    with pytest.raises(SandboxDenied,match='not provisioned'):
        mac.popen(['/bin/echo'],SimpleNamespace(workspace=Path('/private/tmp/work')))


def test_unprivileged_trial_refused_before_any_filesystem_change(monkeypatch,tmp_path_factory):
    monkeypatch.setattr(mac.os,'getuid',lambda:501)
    with pytest.raises(SandboxDenied):
        with mac.execution_trial(tmp_path_factory): pass


@pytest.mark.parametrize('kwargs',[{'pass_fds':(99,)},{'preexec_fn':lambda:None},{'shell':True},{}])
def test_reject_descriptor_and_launch_overrides(kwargs):
    token=mac._TRIAL.set(dict(used=False,workspace=Path('/tmp/work')))
    try:
        with pytest.raises(SandboxDenied):
            mac.popen(['/bin/sh'],SimpleNamespace(workspace=Path('/tmp/work')),**kwargs)
    finally: mac._TRIAL.reset(token)


def process(root,**state):
    p=object.__new__(mac.DarwinTrialProcess)
    p.root=root; p.boot=[1,2]; p.uid=200000001; p.invocation_id='a'*32
    p.returncode=None; p.args=['test']; p.control=None; p.loaded=False
    p._gitgo_cleanup={'complete':False,'issues':['unobserved']}
    return p


@pytest.mark.parametrize('changed',[{'uid':200000002},{'invocation_id':'b'*32},{'boot_id':[3,4]}])
def test_receipt_must_match_lease(monkeypatch,tmp_path_factory,changed):
    s=dict(uid=200000001,invocation_id='a'*32,boot_id=[1,2],phase='clean',generation=2,cleanup={'observed_empty':True})
    s.update(changed)
    monkeypatch.setattr(mac,'load_state',lambda _:s)
    with pytest.raises(RuntimeError,match='identity changed'): process(tmp_path_factory).poll()


def test_unknown_exit_is_not_success(monkeypatch,tmp_path_factory):
    s=dict(uid=200000001,invocation_id='a'*32,boot_id=[1,2],phase='clean',generation=2,
           cleanup={'observed_empty':True,'issues':[]})
    monkeypatch.setattr(mac,'load_state',lambda _:s)
    p=process(tmp_path_factory)
    assert p.poll()==-1 and p._gitgo_cleanup['complete']


def test_cleanup_errors_do_not_retire_identity(monkeypatch,tmp_path_factory):
    s=dict(uid=200000001,invocation_id='a'*32,boot_id=[1,2],phase='clean',generation=2,
           cleanup={'observed_empty':True,'issues':['query_failed']},exit_code=0)
    monkeypatch.setattr(mac,'load_state',lambda _:s)
    p=process(tmp_path_factory)
    assert p.poll()==0 and not p._gitgo_cleanup['complete']


def test_native_cancel_delegates_to_uid_owner_not_pid(monkeypatch):
    from backend.core.process_control import terminate_tree
    scope=SimpleNamespace(close_sandbox=Mock())
    proc=SimpleNamespace(_gitgo_macos_scope=scope,kill=Mock())
    terminate_tree(proc)
    scope.close_sandbox.assert_called_once()
    proc.kill.assert_not_called()


def test_uid_signal_helper_drops_identity_and_inherits_no_task_environment(monkeypatch):
    child=Mock(returncode=0)
    child.communicate.return_value=(b'',b''); child.poll.return_value=0
    launch=Mock(return_value=child); monkeypatch.setattr(ident,'low_spawn',launch)
    ident.send_uid_signal(200000001,[{'pid':999}],ident.signal.SIGKILL)
    args=launch.call_args.args
    assert args[1:3]==(200000001,'/')
    assert args[3]=={'PATH':'/usr/bin:/bin','LC_ALL':'C'}


def test_profile_escapes_path_and_does_not_grant_network(tmp_path_factory):
    text=mac.profile(tmp_path_factory/'a"(allow default)',[])
    assert json.dumps(str(tmp_path_factory/'a"(allow default)')) in text
    assert '(deny default)' in text and '(allow network' not in text


@pytest.mark.parametrize('phase',['starting','running','cleanup_pending','clean'])
def test_service_restart_never_replays(phase):
    from backend.core.macos_execution_service import may_launch
    assert not may_launch(dict(phase=phase,launch_count=1,generation=2))


def test_prepared_lease_can_only_launch_on_first_incarnation():
    from backend.core.macos_execution_service import may_launch
    state=dict(phase='prepared',launch_count=0,generation=1)
    assert may_launch(state)
    assert not may_launch(state,recover_only=True)
    assert not may_launch({**state,'generation':2})


@pytest.mark.parametrize('field',['read_roots','metadata_roots'])
def test_unimplemented_read_scopes_are_refused_not_silently_ignored(field):
    policy=SimpleNamespace(workspace=Path('/tmp/work'),**{field:(Path('/private/selected'),)})
    with pytest.raises(SandboxDenied,match='read views are not implemented'):
        mac.popen(['/usr/bin/true'],policy)



def test_new_uid_cleaner_covers_birth_during_freeze(monkeypatch):
    one=[{'pid':41,'state':'S'}]
    two=one+[{'pid':42,'state':'S'}]
    stopped=[{'pid':41,'state':'T'},{'pid':42,'state':'T'}]
    monkeypatch.setattr(ident,'owned_rows',Mock(side_effect=[one,two,two,stopped,stopped,[],[],[]]))
    send=Mock();monkeypatch.setattr(ident,'send_uid_signal',send)
    monkeypatch.setattr(ident.time,'sleep',lambda _:None)
    result=ident.drain_uid(ident.UID_MIN)
    assert result['observed_empty'] and result['stop_rounds']==2
    assert send.call_args.args[1:]==(stopped,ident.signal.SIGKILL)


def test_new_uid_cleaner_never_claims_success_on_query_failure(monkeypatch):
    monkeypatch.setattr(ident,'owned_rows',Mock(side_effect=OSError('query failed')))
    with pytest.raises(OSError): ident.drain_uid(ident.UID_MIN)


@pytest.mark.parametrize('options,payload,raw',[
    ({'text':False},b'\xff\0binary\r\n',b'\xff\0binary\r\n'),
    ({},b'',b''),
    ({'text':True},'中文\n','中文\n'.encode()),
    ({'encoding':'latin-1'},'é\n',b'\xe9\n'),
    ({'text':False,'encoding':'utf-8'},'中文','中文'.encode()),
    ({'text':False,'commit_fixture':True},b'',b''),
])
def test_trial_adapter_preserves_real_pipe_bytes_and_text(monkeypatch,tmp_path_factory,options,payload,raw):
    """Real pipe ownership/IO through the adapter; launchd is mocked, not isolation."""
    import os
    from backend.core import child_process,macos_resource_guard
    options=dict(options);argv=['/usr/bin/git'];author_env={};states=[]
    if options.pop('commit_fixture',False):
        from backend.core.macos_git_write import command
        argv,author_env=command(tmp_path_factory/'object-view',['commit-tree','a'*40,'-m','message'],b'',
            {'name':'Test','email':'test@local.invalid','date':'1700000000 +0000'})
        options['env']={**author_env,'GIT_CONFIG_COUNT':'1'}
    peer=[]
    class Control:
        reply=iter(b'PID:123\n')
        def settimeout(self,value):pass
        def connect(self,path):pass
        def sendmsg(self,data,ancillary):
            peer.extend(os.dup(fd) for fd in ancillary[0][2])
        def recv(self,size):return bytes([next(self.reply)])
        def close(self):pass
    monkeypatch.setattr(child_process,'is_frozen_runtime',lambda:True)
    monkeypatch.setattr(mac.identity,'boot_id',lambda:[1,2])
    monkeypatch.setattr(mac.identity,'atomic_json',lambda path,value:states.append(value))
    monkeypatch.setattr(mac,'load_state',lambda _: {})
    monkeypatch.setattr(mac,'profile',lambda *a:'fixture')
    monkeypatch.setattr(macos_resource_guard,'validate',lambda _: {})
    monkeypatch.setattr(mac.subprocess,'run',lambda *a,**k:None)
    monkeypatch.setattr(mac.socket,'socket',lambda *a:Control())
    policy=SimpleNamespace(workspace=tmp_path_factory,memory_bytes=None,process_limit=None,cpu_seconds=10)
    proc=None
    try:
        proc=mac.DarwinTrialProcess(dict(root=tmp_path_factory,uid=200000001),argv,policy,
            {'cwd':str(tmp_path_factory),**options},[], 'a'*32)
        assert all(states[0]['env'][key]==value for key,value in author_env.items())
        assert 'GIT_CONFIG_COUNT' not in states[0]['env']
        proc.stdin.write(payload);proc.stdin.close()
        assert os.read(peer[0],4096)==raw
        for fd in peer[1:]:os.write(fd,raw);os.close(fd)
        peer[:]=peer[:1]
        assert proc.stdout.read()==payload and proc.stderr.read()==payload
    finally:
        for fd in peer:os.close(fd)
        if proc:
            for stream in (proc.stdin,proc.stdout,proc.stderr):stream.close()
            proc.control.close()
