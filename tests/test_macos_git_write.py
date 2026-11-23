"""Real fixed Git plumbing and exclusive Host import; no admin isolation claim."""
import hashlib
import os
import subprocess
import sys
import threading
import zlib
import pytest

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_git_write as write,macos_git_read as read
from backend.core.sandbox_read import ReadRoot
from backend.core.loop.git_execution import GitBoundaryError
from tests.test_macos_git_read import repository,capture

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Darwin exclusive publication')
AUTHOR={'name':'Gitgo Test','email':'test@local.invalid','date':'1700000000 +0000'}


def blob(data):
    content=b'blob '+str(len(data)).encode()+b'\0'+data
    oid=hashlib.sha1(content).hexdigest()
    return oid,(oid[:2]+'/'+oid[2:],zlib.compress(content))


def run(repo,tmp,query,data=b'',author=None):
    from backend.core.sandbox_macos import task_environment
    before=capture(repo);view=tmp/'object-view';before.materialize(view)
    command,env=write.command(view,query,data,author)
    env=task_environment(command,tmp,{'PATH':'/usr/bin:/bin',**env,
        'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':'/tmp/forbidden',
        'GIT_OBJECT_DIRECTORY':'/tmp/forbidden','GIT_CONFIG_GLOBAL':'/tmp/forbidden'})
    assert 'GIT_CONFIG_COUNT' not in env and 'GIT_OBJECT_DIRECTORY' not in env
    result=subprocess.run(command,input=data,env=env,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    with ReadRoot(view/'objects') as root:after=read.capture_objects(root,view/'objects')
    produced=write.verify_result(before,after,query,data,author,result.stdout.decode())
    with ReadRoot(repo/'.git/objects') as root:
        value=write.append_objects(repo,root,produced,guard=lambda:None)
    return value.strip()


def test_real_blob_tree_commit_chain_without_refs_or_worktree_changes(repository,tmp_path_factory):
    repo,_,_,raw=repository
    old_head=(repo/'.git/HEAD').read_bytes();old_config=(repo/'.git/config').read_bytes()
    payload=b'\0\xffbinary\r\n'
    a=tmp_path_factory/'a';a.mkdir();oid=run(repo,a,['hash-object','-w','--stdin'],payload)
    assert oid==blob(payload)[0]
    b=tmp_path_factory/'b';b.mkdir();tree=run(repo,b,['mktree','-z'],b'100644 blob '+oid.encode()+b'\tdata.bin\0')
    c=tmp_path_factory/'c';c.mkdir();commit=run(repo,c,['commit-tree',tree,'-m','protected commit'],author=AUTHOR)
    assert raw('cat-file','-t',commit)=='commit'
    assert raw('rev-parse',commit+'^{tree}')==tree
    content=raw('cat-file','-p',commit)
    assert 'author Gitgo Test <test@local.invalid> 1700000000 +0000\n' in content
    assert 'committer Gitgo Test <test@local.invalid> 1700000000 +0000\n' in content
    assert (repo/'.git/HEAD').read_bytes()==old_head and (repo/'.git/config').read_bytes()==old_config
    assert not (repo/'.git/index').exists() and not (repo/'data.bin').exists()


@pytest.mark.parametrize('query,data,author',[
 (['update-ref','refs/heads/x','a'*40],b'',None),
 (['hash-object','-w','--stdin','--path=evil'],b'x',None),
 (['hash-object','-w','--stdin'],b'x',AUTHOR),
 (['mktree','-z'],b'120000 blob '+b'a'*40+b'\tlink\0',None),
 (['mktree','-z'],b'100644 blob '+b'a'*40+b'\t.git\0',None),
 (['mktree','-z'],b'100644 blob '+b'a'*40+b'\ta/b\0',None),
 (['commit-tree','HEAD','-m','x'],b'',AUTHOR),
 (['commit-tree','a'*40,'-m','x'],b'',None),
 (['commit-tree','a'*40,'-m','x'],b'x',AUTHOR),
])
def test_producer_cannot_expand_authority(query,data,author):
    with pytest.raises((GitBoundaryError,ValueError)):write.validate(query,data,author)


def test_output_hash_bomb_mutation_and_deletion_rejected(repository):
    repo,_,_,_=repository;before=capture(repo);oid,item=blob(b'new')
    after=read.ObjectView(tuple(sorted((*before.files,item))),read.object_digest(sorted((*before.files,item))))
    assert write.verify_result(before,after,['hash-object','-w','--stdin'],b'new',None,oid+'\n').oid==oid
    for name,raw in [(item[0],b'broken'),(item[0],item[1]+b'trailing'),('aa/'+'b'*38,item[1]),(item[0],zlib.compress(b'blob 10000000\0'+b'a'*(write.MAX_EXPANDED+1)))]:
        with pytest.raises(GitBoundaryError):write.loose_oid(name,raw)
    empty=read.ObjectView((),read.object_digest(()))
    with pytest.raises(GitBoundaryError):write.verify_result(before,empty,['hash-object','-w','--stdin'],b'new',None,oid+'\n')
    with pytest.raises(GitBoundaryError):write.verify_result(before,after,['hash-object','-w','--stdin'],b'wrong',None,oid+'\n')


def test_import_never_overwrites_existing_symlink_or_bytes(repository,tmp_path_factory):
    repo,_,_,_=repository;oid,item=blob(b'new');directory=repo/'.git/objects';bucket=directory/oid[:2];bucket.mkdir(exist_ok=True)
    target=bucket/oid[2:];outside=tmp_path_factory/'outside';outside.write_bytes(b'outside')
    produced=write.ProducedObjects(oid,(item,),'a'*64)
    with ReadRoot(directory) as root:
        target.symlink_to(outside)
        with pytest.raises((GitBoundaryError,OSError,ValueError)):write.append_objects(repo,root,produced,guard=lambda:None)
        assert outside.read_bytes()==b'outside' and target.is_symlink()
        target.unlink();target.write_bytes(b'corrupt')
        with pytest.raises(GitBoundaryError):write.append_objects(repo,root,produced,guard=lambda:None)
        assert target.read_bytes()==b'corrupt'
    assert not list(bucket.glob('tmp_gitgo_*'))


def test_cancel_before_publish_or_short_write_leaves_no_final_object(repository,monkeypatch):
    repo,_,_,_=repository;oid,item=blob(b'new');directory=repo/'.git/objects';produced=write.ProducedObjects(oid,(item,),'a'*64)
    calls=[]
    def guard():
        calls.append(1)
        if len(calls)==3:raise RuntimeError('cancelled')
    with ReadRoot(directory) as root:
        with pytest.raises(RuntimeError):write.append_objects(repo,root,produced,guard=guard)
        assert not (directory/item[0]).exists()
        monkeypatch.setattr(write.os,'write',lambda *a:0)
        with pytest.raises(OSError):write.append_objects(repo,root,produced,guard=lambda:None)
        assert not (directory/item[0]).exists()
    assert not list(directory.glob('*/tmp_gitgo_*'))


def test_binary_communication_including_empty_stdin():
    from backend.core.sandbox_io import BoundedCommunication
    for data in (None,b'',b'\xff\0binary'):
        proc=subprocess.Popen(['/usr/bin/python3','-c','import sys; print(sys.stdin.buffer.read().hex())'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        out,err=BoundedCommunication(proc).communicate(input=data,timeout=5)
        assert out.strip()==(data or b'').hex() and err==''


@pytest.mark.parametrize('fault',['success','disconnect','cancel','incomplete','digest','object','journal'])
def test_broker_writer_never_imports_unconfirmed_objects(repository,monkeypatch,fault):
    import socket,json,copy,base64
    from backend.core import macos_broker_client as client,macos_broker_protocol as wire,macos_identity
    from backend.core.storage import get_storage
    from backend.core.storage.native_invocations import NativeInvocationRecord,pending_native_invocations
    repo,_,_,_=repository;data=b'write through broker';oid,item=blob(data)
    fd=wire.open_directory(repo);binding=dict(project=str(repo),identity=wire.directory_identity(fd),token='key',grant_id='a'*32,socket='fixture');os.close(fd)
    left,right=socket.socketpair();errors=[];stop=threading.Event()
    class Connection:
        def connect(self,address):assert address=='fixture'
        def __getattr__(self,name):return getattr(left,name)
    def peer():
        fd=None
        try:
            fd=wire.receive_directory(right);request=wire.receive(right)
            from backend.core import macos_broker_capabilities as capabilities
            wire.send(right,capabilities.reply(request,{**binding,'owner_uid':os.getuid()},boot='fixture-boot'))
            request=wire.receive(right)
            assert request['op']=='git_objects' and base64.b64decode(request['input'])==data
            ident=request['invocation_id'];wire.send(right,dict(event='started',invocation_id=ident,pid=os.getpid()))
            if fault=='disconnect':return
            if fault=='cancel':stop.set();assert wire.receive(right)=={'op':'cancel'}
            objects=copy.deepcopy(request['objects']);objects['files'].append([item[0],base64.b64encode(b'bad' if fault=='object' else item[1]).decode(),0o644])
            wire.send(right,dict(event='finished',invocation_id=ident,cleanup=dict(complete=fault!='incomplete',mode='macos_uid',invocation_id=ident,issues=[]),
                exit_code=0,reason='complete',stdout=oid+'\n',stderr='',objects=objects,view_sha256='0'*64 if fault=='digest' else request['view_sha256']))
        except Exception as exc:errors.append(exc)
        finally:
            if fd is not None:os.close(fd)
            right.close()
    if fault=='journal':
        original=NativeInvocationRecord._write
        def fail(self,**kwargs):
            if kwargs.get('terminal'):raise OSError('no journal')
            return original(self,**kwargs)
        monkeypatch.setattr(NativeInvocationRecord,'_write',fail)
    monkeypatch.setattr(client.socket,'socket',lambda *a,**k:Connection());monkeypatch.setattr(wire,'peer_uid',lambda _:0)
    monkeypatch.setattr(macos_identity,'boot_id',lambda:'fixture-boot')
    store=get_storage(repo);token=client._SESSION.set(binding);thread=threading.Thread(target=peer);thread.start()
    try:
        with ReadRoot(repo/'.git/objects') as root:
            if fault=='success':assert client.produce_git_objects(repo,root,['hash-object','-w','--stdin'],data,cancellation=stop).stdout==oid+'\n'
            else:
                with pytest.raises((GitBoundaryError,RuntimeError,EOFError,ValueError)):client.produce_git_objects(repo,root,['hash-object','-w','--stdin'],data,cancellation=stop)
    finally:client._SESSION.reset(token);left.close();thread.join(3)
    assert not errors and not thread.is_alive()
    assert (repo/'.git/objects'/item[0]).exists()==(fault=='success')
    if fault in ('disconnect','incomplete','journal'):assert pending_native_invocations(store,include_active=True)


def test_root_validates_producer_protocol_before_launch(repository,monkeypatch):
    import base64
    from backend.core import macos_broker_protocol as wire,macos_broker_service as server
    from tests.test_macos_broker import request_fixture
    repo,_,_,_=repository;fd,grant,request=request_fixture(repo);view=capture(repo)
    request.pop('request');request.pop('image');request.update(op='git_objects',timeout=10,query=['hash-object','-w','--stdin'],input=base64.b64encode(b'x').decode(),author=None,objects=read.view_to_wire(view),view_sha256=view.sha256)
    monkeypatch.setattr(wire,'peer_uid',lambda _:os.getuid())
    try:
        assert server.authorize(None,fd,request,grant)=='a'*32
        for bad in ({'query':['update-ref','refs/heads/x','a'*40]},{'input':'@@'},{'source':'/private/etc'},{'view_sha256':'0'*64}):
            with pytest.raises((GitBoundaryError,ValueError)):server.authorize(None,fd,{**request,**bad},grant)
    finally:os.close(fd)


@pytest.mark.parametrize('change',['executable','directory','option','command','missing','mismatch','extra','bad_date'])
def test_commit_identity_cannot_widen_task_environment(tmp_path_factory,change):
    from backend.core.sandbox_macos import task_environment
    workspace=tmp_path_factory
    argv,env=write.command(workspace/'object-view',['commit-tree','a'*40,'-m','message'],b'',AUTHOR)
    if change=='executable':argv[0]='/bin/sh'
    elif change=='directory':argv=[a.replace(str(workspace/'object-view'),str(workspace/'other')) for a in argv]
    elif change=='option':argv.insert(1,'--no-replace-objects')
    elif change=='command':argv[-4:]=['hash-object','-w','--stdin']
    elif change=='missing':env.pop('GIT_AUTHOR_DATE')
    elif change=='mismatch':env['GIT_COMMITTER_EMAIL']='different@local.invalid'
    elif change=='extra':env['GIT_AUTHOR_UNKNOWN']='value'
    elif change=='bad_date':env['GIT_AUTHOR_DATE']='now'
    with pytest.raises(GitBoundaryError):task_environment(argv,workspace,env)
