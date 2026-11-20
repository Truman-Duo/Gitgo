"""Real Rust capture/journal with a simulated root transport; no UID claim."""
import copy
import os
import socket
import sys
import threading

import pytest

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_broker_client as client
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as server
from backend.core import macos_git_read as git
from backend.core.loop.git_execution import GitBoundaryError
from backend.core.sandbox_read import ReadRoot
from backend.core.storage import get_storage
from backend.core.storage.native_invocations import NAMESPACE, pending_native_invocations
from tests.test_macos_broker import request_fixture
from tests.test_macos_git_read import repository, capture

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS Git broker')


def test_transport_binds_exact_bytes_and_applies_existing_smaller_budget(repository, monkeypatch):
    repo, _, _, _ = repository
    view=capture(repo);value=git.view_to_wire(view)
    assert git.view_from_wire(value,view.sha256)==view
    bad=copy.deepcopy(value);bad['files'][0][1]='eA=='
    with pytest.raises(GitBoundaryError): git.view_from_wire(bad,view.sha256)
    monkeypatch.setattr(wire,'MAX_CONTENT',1)
    with pytest.raises(ValueError): git.view_to_wire(view)


@pytest.mark.parametrize('name',['../escape','info/alternates','config','hooks/update',
    'pack/pack-'+ 'a'*40+'.promisor','aa/'+'b'*62])
def test_untrusted_view_cannot_materialize_paths(name):
    with pytest.raises(GitBoundaryError):
        git.ObjectView(((name,b'x'),),git.object_digest(((name,b'x'),)))


@pytest.mark.parametrize('change',[{'query':['status']},{'query':['cat-file','-t','HEAD']},
    {'argv':['/bin/sh']},{'source':'/etc'},{'image':{'files':[],'directories':[]}},
    {'view_sha256':'0'*64},{'timeout':True}])
def test_root_rejects_expanded_git_authority(repository,monkeypatch,change):
    repo,oid,_,_=repository;fd,grant,request=request_fixture(repo)
    view=capture(repo)
    request.pop('request');request.pop('image')
    request.update(op='git_read',query=['cat-file','-t',oid],objects=git.view_to_wire(view),view_sha256=view.sha256)
    monkeypatch.setattr(wire,'peer_uid',lambda _:os.getuid())
    try:
        assert server.authorize(None,fd,request,grant)=='a'*32
        with pytest.raises((ValueError,GitBoundaryError)):
            server.authorize(None,fd,{**request,**change},grant)
    finally: os.close(fd)


@pytest.mark.parametrize('mode',['success','denied','disconnect','wrong_id','incomplete',
    'wrong_mode','wrong_digest','bad_output','image','project_replaced','cancel','journal_failure',
    'cancel_on_receipt','cancel_at_commit','ancestor_false','ancestor_error','ancestor_bad_output'])
def test_host_git_adapter_retains_original_journal_and_withholds_unconfirmed_results(repository,monkeypatch,mode):
    from backend.core import macos_identity
    from backend.core.storage.native_invocations import NativeInvocationRecord
    repo,oid,_,_=repository
    fd=wire.open_directory(repo)
    binding=dict(project=str(repo),identity=wire.directory_identity(fd),token='key',grant_id='a'*32,socket='fixture')
    os.close(fd)
    left,right=socket.socketpair();errors=[];requests=[];stop=threading.Event()
    class Connection:
        def connect(self,address): assert address=='fixture'
        def __getattr__(self,name): return getattr(left,name)
    def peer():
        received=None
        try:
            received=wire.receive_directory(right);request=wire.receive(right)
            from backend.core import macos_broker_capabilities as capabilities
            wire.send(right,capabilities.reply(request,{**binding,'owner_uid':os.getuid()},boot='fixture-boot'))
            request=wire.receive(right);requests.append(request)
            assert wire.directory_identity(received)==binding['identity']
            assert request['op']=='git_read' and 'request' not in request and 'source' not in request
            git.view_from_wire(request['objects'],request['view_sha256'])
            identifier=request['invocation_id']
            if mode=='denied':
                wire.send(right,dict(event='denied',invocation_id=identifier,error='fixture denial'));return
            wire.send(right,dict(event='started',invocation_id=identifier,pid=os.getpid()))
            if mode=='disconnect': return
            if mode=='project_replaced': repo.rename(repo.with_name('held'));repo.mkdir()
            if mode=='cancel':
                stop.set();assert wire.receive(right)=={'op':'cancel'}
            wire.send(right,dict(event='finished',invocation_id='0'*32 if mode=='wrong_id' else identifier,
                cleanup=dict(complete=mode!='incomplete',mode='unknown' if mode=='wrong_mode' else 'macos_uid',
                    invocation_id=identifier,issues=[]),exit_code=2 if mode=='ancestor_error' else 1 if mode.startswith('ancestor_') else 0,reason='complete',stderr='',
                view_sha256='0'*64 if mode=='wrong_digest' else request['view_sha256'],
                stdout='' if mode in ('ancestor_false','ancestor_error') else 'blob\nextra' if mode=='bad_output' else 'blob\n',
                **({'image':{}} if mode=='image' else {})))
        except Exception as exc: errors.append(exc)
        finally:
            if received is not None: os.close(received)
            right.close()
    if mode=='journal_failure':
        original=NativeInvocationRecord._write
        def fail_terminal(self,**kw):
            if kw.get('terminal'): raise OSError('journal unavailable')
            return original(self,**kw)
        monkeypatch.setattr(NativeInvocationRecord,'_write',fail_terminal)
    if mode=='cancel_on_receipt':
        original_receive=wire.receive
        def receive(connection):
            result=original_receive(connection)
            if result.get('event')=='finished':stop.set()
            return result
        monkeypatch.setattr(wire,'receive',receive)
    if mode=='cancel_at_commit':
        original_finish=NativeInvocationRecord.finish
        def finish(self,observation):
            original_finish(self,observation)
            stop.set()
        monkeypatch.setattr(NativeInvocationRecord,'finish',finish)
    monkeypatch.setattr(macos_identity,'boot_id',lambda:'fixture-boot')
    monkeypatch.setattr(client.socket,'socket',lambda *a,**k:Connection())
    monkeypatch.setattr(wire,'peer_uid',lambda _:0)
    store=get_storage(repo)
    token=client._SESSION.set(binding);worker=threading.Thread(target=peer);worker.start()
    try:
        with ReadRoot(repo/'.git/objects') as root:
            if mode.startswith('ancestor_'):
                query=['merge-base','--is-ancestor',oid,oid]
                if mode=='ancestor_false':
                    result=client.query_git_objects(repo,root,query,cancellation=stop)
                    assert result.returncode==1 and result.stdout==''
                else:
                    with pytest.raises(GitBoundaryError):client.query_git_objects(repo,root,query,cancellation=stop)
            elif mode=='success': assert client.read_git_objects(repo,root,['cat-file','-t',oid],cancellation=stop)=='blob\n'
            else:
                with pytest.raises((GitBoundaryError,RuntimeError,ValueError,EOFError)):
                    client.read_git_objects(repo,root,['cat-file','-t',oid],cancellation=stop)
    finally:
        client._SESSION.reset(token);left.close();worker.join(3)
    assert not worker.is_alive() and not errors
    assert len(requests)==1
    identifier=requests[0]['invocation_id']
    pending=pending_native_invocations(store,include_active=True)
    if mode in ('disconnect','wrong_id','incomplete','wrong_mode','journal_failure'):
        assert len(pending)==1 and pending[0]['invocation_id']==identifier
        assert store.get_state_ref(NAMESPACE,'history/'+identifier) is None
    else:
        assert not pending
        ref=store.get_state_ref(NAMESPACE,'history/'+identifier)
        import json
        record=json.loads(store.read_blob(ref,repair=False))
        assert record['tool_name']=='macos_git_read'
        assert record['execution_basis']==requests[0]['execution_basis']
        assert record['macos_broker_binding']['grant_id']==binding['grant_id']
        assert record['launch_state']==('not_started' if mode=='denied' else 'started')


def test_adapter_requires_explicit_project_capability_and_matching_native_root(repository,monkeypatch,tmp_path_factory):
    from backend.core.sandbox import SandboxDenied
    repo,oid,_,_=repository
    with ReadRoot(repo/'.git/objects') as root:
        with pytest.raises(SandboxDenied): client.read_git_objects(repo,root,['cat-file','-t',oid])
    fd=wire.open_directory(repo)
    binding=dict(project=str(repo),identity=wire.directory_identity(fd),token='key',grant_id='a'*32,socket='never')
    os.close(fd)
    token=client._SESSION.set(binding)
    try:
        get_storage(repo)
        with ReadRoot(tmp_path_factory) as root:
            with pytest.raises(GitBoundaryError,match='differ'):
                client.read_git_objects(repo,root,['cat-file','-t',oid])
        with ReadRoot(repo/'.git/objects') as root:
            stop=threading.Event();stop.set()
            with pytest.raises(GitBoundaryError,match='cancelled'):
                client.read_git_objects(repo,root,['cat-file','-t',oid],cancellation=stop)
        assert not pending_native_invocations(get_storage(repo),include_active=True)
    finally: client._SESSION.reset(token)


def test_unregistered_project_is_not_created_by_a_read(repository):
    from backend.core.sandbox import SandboxDenied
    repo,oid,_,_=repository
    fd=wire.open_directory(repo)
    binding=dict(project=str(repo),identity=wire.directory_identity(fd),token='key',grant_id='a'*32,socket='never')
    os.close(fd)
    token=client._SESSION.set(binding)
    try:
        with ReadRoot(repo/'.git/objects') as root:
            with pytest.raises(SandboxDenied,match='Register'):
                client.read_git_objects(repo,root,['cat-file','-t',oid])
        assert not (repo/'.gitgo').exists() and not (repo/'.git/gitgo').exists()
    finally: client._SESSION.reset(token)


def test_git_journal_cannot_attach_to_another_project_context(repository,tmp_path_factory):
    from types import SimpleNamespace
    from backend.core.loop.native_invocation_scope import native_invocation_scope
    from backend.core.sandbox import SandboxDenied
    repo,oid,_,_=repository;get_storage(repo)
    other=tmp_path_factory/'other';other.mkdir()
    fd=wire.open_directory(repo)
    binding=dict(project=str(repo),identity=wire.directory_identity(fd),token='key',grant_id='a'*32,socket='never')
    os.close(fd)
    token=client._SESSION.set(binding)
    try:
        with ReadRoot(repo/'.git/objects') as root, native_invocation_scope(SimpleNamespace(workspace_path=other),'test',0):
            with pytest.raises(SandboxDenied,match='different project'):
                client.read_git_objects(repo,root,['cat-file','-t',oid])
        assert not pending_native_invocations(get_storage(repo),include_active=True)
    finally: client._SESSION.reset(token)


def test_service_git_branch_has_no_source_path_runner_stdin_or_workspace_export(repository,monkeypatch):
    """Wiring test only; the administrator harness checks the actual UID path."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    import shutil
    repo,oid,_,_=repository;view=capture(repo)
    fd,approved,request=request_fixture(repo);os.close(fd)
    approved['boot']='fixture'
    request.pop('request');request.pop('image')
    request.update(op='git_read',query=['cat-file','-t',oid],objects=git.view_to_wire(view),view_sha256=view.sha256)
    launched=[];writes=[];sent=[];workspaces=[]
    @contextmanager
    def trial(work,**kw):
        workspaces.append(work)
        yield {'root':work.parent/('c'*32),'uid':200000001}
    proc=SimpleNamespace(pid=1234,returncode=0,resource_reason=None,
        _gitgo_cleanup=dict(complete=True,mode='macos_uid',invocation_id=request['invocation_id'],issues=[]),
        close_sandbox=lambda:None,stdin=None,stdout=None,stderr=None)
    def launch(command,policy,**kw):
        launched.append((command,policy,kw));return proc
    class Capture:
        def __init__(self,p,limit): assert p is proc and limit==4096
        def communicate(self,**kw): assert kw['input'] is None;return 'blob\n',''
    def forbidden(*a,**k): raise AssertionError('Git reads cannot export workspaces or run the tool runner')
    monkeypatch.setattr(server,'execution_trial',trial)
    monkeypatch.setattr(server,'sandbox_popen',launch)
    monkeypatch.setattr(server,'BoundedCommunication',Capture)
    monkeypatch.setattr(server,'tool_runner_command',forbidden)
    monkeypatch.setattr(server,'export_image',forbidden)
    monkeypatch.setattr(server,'grant',lambda _:approved)
    monkeypatch.setattr(server.select,'select',lambda *a:([],[],[]))
    monkeypatch.setattr(server.identity,'atomic_json',lambda path,value:writes.append((path,value)))
    monkeypatch.setattr(server.identity,'owned_rows',lambda uid:[])
    monkeypatch.setattr(wire,'send',lambda connection,value:sent.append(value))
    try:
        server.execute(None,request,repo,approved)
        command,policy,kw=launched[0]
        assert command[0]=='/usr/bin/git' and command[-3:]==['cat-file','-t',oid]
        assert str(repo) not in ' '.join(command) and kw['cwd']==str(policy.workspace)
        assert kw['env']=={} and kw['invocation_id']==request['invocation_id']
        assert all(p.stat().st_mode & 0o007==4 for p in (policy.workspace/'object-view').rglob('*') if p.is_file())
        assert sent[-1]['stdout']=='blob\n' and 'image' not in sent[-1]
        assert sent[-1]['view_sha256']==view.sha256 and sent[-1]['cleanup']['complete']
        mapping=writes[0][1]
        assert mapping['execution_basis']==request['execution_basis'] and mapping['grant_id']==approved['grant_id']
    finally:
        for work in workspaces: shutil.rmtree(work.parent)
