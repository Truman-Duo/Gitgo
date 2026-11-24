"""Real Git commit images and hostile result validation; no admin claim."""
import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import threading

import pytest


if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core import macos_git_checkout as checkout, macos_git_read as read
from backend.core.loop.git_execution import GitBoundaryError
from tests.test_macos_git_read import repository, capture

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS checkout bridge')


def commit_fixture(repository, files=None):
    repo, _, env, raw = repository
    files = [('a.bin', b'\xff\0\r\n', 0o644), ('nested/run.sh', b'#!/bin/sh\n', 0o755),
             ('.gitattributes', b'*.bin text eol=lf filter=hostile\n', 0o644)] if files is None else files
    for name, data, mode in files:
        path=repo/name;path.parent.mkdir(exist_ok=True,parents=True);path.write_bytes(data);path.chmod(mode)
        oid=raw('hash-object','-w','--stdin',data=data)
        raw('update-index','--add','--cacheinfo',f'{"100755" if mode==0o755 else "100644"},{oid},{name}')
    tree=raw('write-tree')
    commit=raw('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',tree,'-m','fixture')
    commit_bytes=subprocess.run(['/usr/bin/git','cat-file','commit',commit],cwd=repo,env=env,
        capture_output=True,check=True).stdout
    dirs=sorted({'/'.join(name.split('/')[:i]) for name,_,_ in files for i in range(1,len(name.split('/')))})
    image=dict(files=[[n,base64.b64encode(data).decode(),mode] for n,data,mode in files],directories=dirs)
    return commit,image,json.dumps(dict(commit=base64.b64encode(commit_bytes).decode()))


@pytest.mark.parametrize('packed',[False,True])
def test_real_checkout_exact_bytes_modes_and_host_index(repository,tmp_path_factory,packed):
    from backend.core.sandbox_macos import task_environment
    repo,_,_,raw=repository
    commit,expected,_=commit_fixture(repository)
    if packed:
        raw('update-ref','refs/heads/main',commit);raw('repack','-adn');raw('prune-packed')
    marker=tmp_path_factory/'forbidden'
    raw('config','filter.hostile.smudge','touch '+str(marker))
    raw('config','core.fsmonitor','touch '+str(marker))
    (repo/'.git/hooks/post-checkout').write_text('#!/bin/sh\ntouch '+str(marker))
    (repo/'.git/hooks/post-checkout').chmod(0o755)
    before=capture(repo)
    destination=tmp_path_factory/'object-view';before.materialize(destination)
    work=tmp_path_factory/'checkout';work.mkdir()
    command=checkout.command(destination,work,commit)
    environment=task_environment(command,tmp_path_factory,{'PATH':'/usr/bin:/bin',
        'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'filter.hostile.smudge',
        'GIT_CONFIG_VALUE_0':'touch '+str(marker),'GIT_INDEX_FILE':'/tmp/forbidden'})
    result=subprocess.run(command,env=environment,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    image=dict(files=[[str(p.relative_to(work)),base64.b64encode(p.read_bytes()).decode(),p.stat().st_mode&0o777]
                      for p in work.rglob('*') if p.is_file()],
               directories=[str(p.relative_to(work)) for p in work.rglob('*') if p.is_dir()])
    accepted=checkout.verify_result(commit,image,result.stdout)
    assert sorted(image['files'])==sorted(expected['files']) and not marker.exists()
    # Verify the Host-generated index with actual Git, without trusting a
    # task-authored binary index, cache-tree, split-index link or stat cache.
    (destination/'index').write_bytes(accepted.index)
    query=read.read_command(destination,['cat-file','-t',commit])[:-3]+['write-tree']
    actual=subprocess.run(query,env=environment,capture_output=True,text=True,check=True).stdout.strip()
    assert actual==raw('rev-parse',commit+'^{tree}')
    assert capture(repo)==before


@pytest.mark.parametrize('fault',['bytes','mode','missing','extra','directory','proof','commit','alias','extension'])
def test_host_rejects_changed_or_unbound_checkout(repository,fault):
    commit,image,proof=commit_fixture(repository)
    if fault=='bytes':image['files'][0][1]=base64.b64encode(b'wrong').decode()
    elif fault=='mode':image['files'][0][2]=0o755
    elif fault=='missing':image['files'].pop()
    elif fault=='extra':image['files'].append(['extra','',0o644])
    elif fault=='directory':image['directories'].append('extra')
    elif fault=='proof':proof='{"commit":"@@"}'
    elif fault=='commit':commit='a'*40
    elif fault=='alias':image['files'].append(['a.bin.','',0o644])
    elif fault=='extension':proof=json.dumps({**json.loads(proof),'index':'untrusted'})
    with pytest.raises((GitBoundaryError,ValueError)):checkout.verify_result(commit,image,proof)


@pytest.mark.parametrize('name',['.git/config','dir/.GITgo/file','a./b','a /b','a:b','dir/\u200c.git/x'])
def test_metadata_and_portable_alias_paths_refused(name):
    files=[(name,b'x',0o644)]
    dirs=['/'.join(name.split('/')[:i]) for i in range(1,len(name.split('/')))]
    with pytest.raises(GitBoundaryError):checkout.validate_paths(files,dirs)


def test_unicode_directory_alias_and_tree_order():
    with pytest.raises(GitBoundaryError):
        checkout.validate_paths([('é/a',b'',0o644),('e\u0301/b',b'',0o644)],['é','e\u0301'])
    # A tree sorts as if followed by '/', not as a plain directory name.
    files=[('foo/z',b'z',0o644),('foo.bar',b'b',0o644),('foo0',b'0',0o644)]
    oid=checkout.object_oid
    subtree=oid('tree',b'100644 z\0'+bytes.fromhex(oid('blob',b'z')))
    expected=oid('tree',b'100644 foo.bar\0'+bytes.fromhex(oid('blob',b'b'))+
        b'40000 foo\0'+bytes.fromhex(subtree)+b'100644 foo0\0'+bytes.fromhex(oid('blob',b'0')))
    assert checkout.tree_oid(files)==expected


def test_empty_commit_checkout(repository):
    commit,image,proof=commit_fixture(repository,[])
    result=checkout.verify_result(commit,image,proof)
    assert result.files==() and len(result.index)==32


@pytest.mark.parametrize('kind',['symlink','submodule'])
def test_producer_refuses_nonregular_tree_entries(repository,tmp_path_factory,kind):
    repo,blob,env,raw=repository
    if kind=='submodule':
        commit,_,_=commit_fixture(repository,[])
        row=f'160000 commit {commit}\tmodule\0'.encode()
    else:row=f'120000 blob {blob}\tlink\0'.encode()
    tree=raw('mktree','-z',data=row)
    commit=raw('-c','user.name=Test','-c','user.email=t@local.invalid','commit-tree',tree,'-m','bad')
    destination=tmp_path_factory/'object-view';capture(repo).materialize(destination)
    work=tmp_path_factory/'checkout';work.mkdir()
    result=subprocess.run(checkout.command(destination,work,commit),env=env,capture_output=True,timeout=10)
    assert result.returncode!=0 and list(work.iterdir())==[]


def test_broker_authorizes_only_fixed_checkout(repository,monkeypatch):
    from backend.core import macos_broker_protocol as wire,macos_broker_service as server
    from tests.test_macos_broker import request_fixture
    repo,_,_,_=repository;fd,grant,request=request_fixture(repo);view=capture(repo)
    request.pop('request');request.pop('image')
    request.update(op='git_checkout',timeout=10,commit='a'*40,objects=read.view_to_wire(view),view_sha256=view.sha256)
    monkeypatch.setattr(wire,'peer_uid',lambda _:os.getuid())
    try:
        assert server.authorize(None,fd,request,grant)=='a'*32
        for bad in ({'commit':'HEAD'},{'commit':'a'*40+'^{commit}'},{'target':'/private/etc'},
                    {'command':['/bin/sh']},{'view_sha256':'0'*64}):
            with pytest.raises((GitBoundaryError,ValueError)):server.authorize(None,fd,{**request,**bad},grant)
    finally:os.close(fd)


@pytest.mark.parametrize('limit',['files','bytes'])
def test_real_producer_refuses_output_budget_without_a_deliverable_image(repository,tmp_path_factory,limit):
    repo,_,env,raw=repository
    data=b'x'*(4*1024*1024+1) if limit=='bytes' else b'x'
    oid=raw('hash-object','-w','--stdin',data=data)
    listing=b''.join(f'100644 blob {oid}\tf{i:03d}\0'.encode() for i in range(129 if limit=='files' else 1))
    tree=raw('mktree','-z',data=listing)
    commit=raw('-c','user.name=Test','-c','user.email=t@local.invalid','commit-tree',tree,'-m','large')
    destination=tmp_path_factory/'object-view';capture(repo).materialize(destination)
    work=tmp_path_factory/'checkout';work.mkdir()
    result=subprocess.run(checkout.command(destination,work,commit),env=env,capture_output=True,timeout=10)
    assert result.returncode!=0 and result.stdout==b'' and list(work.iterdir())==[]


@pytest.mark.parametrize('fault',['success','disconnect','cancel','incomplete','digest','commit','image','journal'])
def test_checkout_client_withholds_unconfirmed_results(repository,monkeypatch,fault):
    import socket
    from backend.core import macos_broker_client as client,macos_broker_protocol as wire,macos_identity
    from backend.core.sandbox_read import ReadRoot
    from backend.core.storage import get_storage
    from backend.core.storage.native_invocations import NativeInvocationRecord,pending_native_invocations
    repo,_,_,_=repository;commit,image,proof=commit_fixture(repository);before=capture(repo)
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
            assert request['op']=='git_checkout' and request['commit']==commit
            ident=request['invocation_id'];wire.send(right,dict(event='started',invocation_id=ident,pid=os.getpid()))
            if fault=='disconnect':return
            if fault=='cancel':stop.set();assert wire.receive(right)=={'op':'cancel'}
            returned=copy.deepcopy(image)
            if fault=='image':returned['files'][0][1]=''
            wire.send(right,dict(event='finished',invocation_id=ident,
                cleanup=dict(complete=fault!='incomplete',mode='macos_uid',invocation_id=ident,issues=[]),
                exit_code=0,reason='complete',stdout=proof,stderr='',checkout=returned,
                commit='b'*40 if fault=='commit' else commit,view_sha256='0'*64 if fault=='digest' else request['view_sha256']))
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
    monkeypatch.setattr(client.socket,'socket',lambda *a,**k:Connection())
    monkeypatch.setattr(wire,'peer_uid',lambda _:0);monkeypatch.setattr(macos_identity,'boot_id',lambda:'fixture-boot')
    store=get_storage(repo);token=client._SESSION.set(binding);thread=threading.Thread(target=peer);thread.start()
    try:
        with ReadRoot(repo/'.git/objects') as root:
            if fault=='success':assert client.checkout_git_commit(repo,root,commit,cancellation=stop).commit==commit
            else:
                with pytest.raises((GitBoundaryError,RuntimeError,EOFError,ValueError)):
                    client.checkout_git_commit(repo,root,commit,cancellation=stop)
    finally:client._SESSION.reset(token);left.close();thread.join(3)
    assert not errors and not thread.is_alive() and capture(repo)==before
    assert not (repo/'.git/worktrees').exists()
    if fault in ('disconnect','incomplete','journal'):assert pending_native_invocations(store,include_active=True)
