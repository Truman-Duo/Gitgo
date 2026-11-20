"""Broker authority/transport tests, not substitutes for administrator acceptance."""
import array
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
from contextlib import contextmanager
from unittest.mock import Mock
import sys
import pytest
if sys.platform=='win32': pytest.skip('POSIX broker',allow_module_level=True)
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as server


@pytest.mark.parametrize('scenario', ['match', 'wrong-project', 'replaced', 'wrong-socket', 'trial'])
def test_resident_session_checks_project_and_clears_binding(tmp_path_factory, monkeypatch, scenario):
    from backend.core import macos_broker_client as client
    from backend.core import macos_helper_install as installer
    from backend.core import macos_helper_lifecycle as lifecycle
    from backend.core import macos_project_identity as project_identity

    project = tmp_path_factory / 'project'
    project.mkdir()
    service = tmp_path_factory / 'registry' / 'test'
    with_project_object = {'volume_uuid': 'fixture'}
    fd = wire.open_directory(project)
    try:
        key = wire.directory_identity(fd)
    finally:
        os.close(fd)
    binding = {
        'project': str(project), 'identity': key, 'project_object': with_project_object,
        'grant_id': 'test-grant', 'socket': str(service / 'broker.sock'),
    }
    if scenario == 'wrong-project':
        binding['project'] = str(project.with_name('other'))
    if scenario == 'replaced':
        project.rename(project.with_name('previous'))
        project.mkdir()
    if scenario == 'wrong-socket':
        binding['socket'] = str(service / 'other.sock')
    if scenario == 'trial':
        del binding['project_object']
    monkeypatch.setattr(lifecycle, 'REGISTRY', service.parent)
    trusted = Mock()
    monkeypatch.setattr(installer, 'trusted_directory', trusted)
    monkeypatch.setattr(project_identity, 'persistent_identity', lambda _: with_project_object)

    @contextmanager
    def credential(path):
        assert path == service / 'client.json'
        token = client._SESSION.set(binding)
        try:
            yield binding
        finally:
            client._SESSION.reset(token)

    monkeypatch.setattr(client, 'broker_session', credential)
    if scenario == 'match':
        with client.resident_project_session('test', project):
            assert client.project_binding(project) == {'grant_id': 'test-grant', 'identity': key}
    else:
        with pytest.raises(ValueError):
            with client.resident_project_session('test', project):
                pytest.fail('Invalid resident binding entered the Host scope')
    trusted.assert_called_once_with(service)
    assert not client.active()


@pytest.mark.skipif(sys.platform!='darwin',reason='Darwin kernel credentials')
def test_kernel_peer_identity_and_fd_transfer(tmp_path_factory):
    left,right=socket.socketpair()
    fd=wire.open_directory(tmp_path_factory)
    try:
        assert wire.peer_uid(left)==os.geteuid()
        wire.send_directory(left,fd)
        received=wire.receive_directory(right)
        try:
            assert not os.get_inheritable(received)
            assert wire.directory_identity(received)==wire.directory_identity(fd)
        finally: os.close(received)
    finally: left.close();right.close();os.close(fd)


@pytest.mark.parametrize('raw',[b'{"x":1,"x":2}',b'{"x":NaN}',b'[]'])
def test_ambiguous_protocol_objects_rejected(raw):
    a,b=socket.socketpair()
    try:
        a.sendall(struct.pack('!I',len(raw))+raw)
        with pytest.raises(ValueError): wire.receive(b)
    finally: a.close();b.close()


def test_oversize_frame_refused_without_reading_body():
    a,b=socket.socketpair()
    try:
        a.sendall(struct.pack('!I',wire.MAX_FRAME+1))
        with pytest.raises(ValueError): wire.receive(b)
    finally: a.close();b.close()


@pytest.mark.parametrize('name',['../escape','/etc/hosts','a/../../escape','.git/config','A/.GITGO/state','a\\b','a//b'])
def test_snapshot_paths_cannot_escape_or_publish_metadata(name):
    with pytest.raises(ValueError): wire.validate_image({'files':[[name,'eA==',0o644]],'directories':[]})


@pytest.mark.parametrize('mode',[0o4755,0o777,True,0o666])
def test_snapshot_modes_cannot_add_privileged_permissions(mode):
    with pytest.raises(ValueError): wire.validate_image({'files':[['file','eA==',mode]],'directories':[]})


def test_case_collision_and_file_as_parent_refused():
    for members in ([['a','',0o644],['A','',0o644]],[['a','',0o644],['a/b','',0o644]]):
        with pytest.raises(ValueError): wire.validate_image({'files':members,'directories':[]})


def request_fixture(root):
    fd=wire.open_directory(root)
    grant={'owner_uid':os.getuid(),'identity':wire.directory_identity(fd),'project':str(root),
           'grant_id':'grant','token_hash':hashlib.sha256(b'key').hexdigest()}
    request={'op':'run','token':'key','grant_id':'grant','invocation_id':'a'*32,'execution_basis':'b'*64,'timeout':10,
             'request':{'tool_name':'exec_command','args':{'_workspace':'/untrusted/never/open'},
                        '_native_sandbox_workspace':'/untrusted/never/open'},
             'image':{'files':[],'directories':[]}}
    return fd,grant,request


def test_authorization_uses_kernel_not_claimed_user(monkeypatch,tmp_path_factory):
    fd,grant,request=request_fixture(tmp_path_factory)
    monkeypatch.setattr(wire,'peer_uid',lambda _:os.getuid()+1)
    try:
        with pytest.raises(PermissionError,match='Peer'): server.authorize(None,fd,request,grant)
    finally: os.close(fd)


@pytest.mark.parametrize('change',[{'token':'wrong'},{'grant_id':'other'},{'profile':'allow all'},
                                   {'timeout':float('nan')},{'timeout':True},{'op':'chown'}])
def test_no_client_selected_root_policy(monkeypatch,tmp_path_factory,change):
    fd,grant,request=request_fixture(tmp_path_factory)
    monkeypatch.setattr(wire,'peer_uid',lambda _:os.getuid())
    try:
        with pytest.raises((ValueError,PermissionError)): server.authorize(None,fd,{**request,**change},grant)
    finally: os.close(fd)


def test_stale_project_path_and_wrong_directory_descriptor_denied(monkeypatch,tmp_path_factory):
    root=tmp_path_factory/'project';root.mkdir()
    fd,grant,request=request_fixture(root)
    monkeypatch.setattr(wire,'peer_uid',lambda _:os.getuid())
    try:
        assert server.authorize(None,fd,request,grant)=='a'*32
        root.rename(tmp_path_factory/'original');root.mkdir()
        with pytest.raises(PermissionError,match='replaced'): server.authorize(None,fd,request,grant)
        other=wire.open_directory(root)
        try:
            with pytest.raises(PermissionError,match='object'): server.authorize(None,other,request,grant)
        finally: os.close(other)
    finally: os.close(fd)


def test_directory_walk_rejects_symlink_ancestor(tmp_path_factory):
    root=tmp_path_factory/'real';root.mkdir();(root/'child').mkdir()
    (tmp_path_factory/'link').symlink_to(root,target_is_directory=True)
    with pytest.raises(OSError): wire.open_directory(tmp_path_factory/'link/child')


@pytest.mark.skipif(sys.platform!='darwin' or os.geteuid()==0,reason='Ordinary Darwin traversal permissions')
def test_project_binding_needs_search_not_parent_directory_listing(tmp_path_factory):
    parent=tmp_path_factory/'search-only';parent.mkdir()
    project=parent/'project';project.mkdir(mode=0o700)
    expected=project.stat()
    parent.chmod(0o111)
    try:
        with pytest.raises(PermissionError): os.listdir(parent)
        fd=wire.open_directory(project)
        try:
            assert wire.directory_identity(fd)==[expected.st_dev,expected.st_ino,expected.st_uid]
            left,right=socket.socketpair()
            try:
                wire.send_directory(left,fd);received=wire.receive_directory(right)
                try: assert wire.directory_identity(received)==wire.directory_identity(fd)
                finally: os.close(received)
            finally: left.close();right.close()
        finally: os.close(fd)
        assert parent.stat().st_mode & 0o777==0o111
    finally: parent.chmod(0o700)


def test_candidate_swap_preserves_project_and_detects_concurrent_edit(tmp_path_factory):
    from backend.core.macos_broker_client import replace_candidate
    from backend.core.tools.workspace_projection import capture
    stage=tmp_path_factory/'stage';stage.mkdir();(stage/'in.txt').write_text('before')
    before=capture(stage);st=stage.stat();key=(st.st_dev,st.st_ino)
    (stage/'in.txt').write_text('changed')
    with pytest.raises(RuntimeError,match='changed'): replace_candidate(stage,key,before,{'files':[],'directories':[]})
    assert (stage/'in.txt').read_text()=='changed'


@pytest.mark.parametrize('kind',['symlink','fifo','hardlink'])
def test_output_exporter_refuses_special_objects_without_following_them(tmp_path_factory,kind):
    import subprocess
    work=tmp_path_factory/'view';work.mkdir()
    outside=tmp_path_factory/'private';outside.write_text('private-canary')
    if kind=='symlink': (work/'output').symlink_to(outside)
    elif kind=='fifo': os.mkfifo(work/'output')
    else: os.link(outside,work/'output')
    result=subprocess.run([sys.executable,'-I','-c',server.EXPORT],cwd=work,capture_output=True,text=True,timeout=3)
    assert result.returncode!=0 and 'private-canary' not in result.stdout
    assert outside.read_text()=='private-canary'


@pytest.mark.skipif(sys.platform!='darwin',reason='Darwin integration selection')
@pytest.mark.parametrize('response_kind',['success','denied_after_start','unknown_cleanup','wrong_peer','project_replaced','timeout'])
def test_real_host_pipeline_with_simulated_broker_peer(tmp_path_factory,monkeypatch,response_kind):
    """Real candidate/file service; transport fixture is NOT privileged evidence."""
    import base64
    import threading
    from backend.core import macos_broker_client as client
    from backend.core import macos_identity
    monkeypatch.setattr(macos_identity,'boot_id',lambda:[100,0])
    from tests.test_workspace_candidate_service import make
    project=tmp_path_factory/'project';project.mkdir()
    (project/'input.txt').write_text('original')
    fd=wire.open_directory(project)
    binding={'project':str(project),'identity':wire.directory_identity(fd),
             'token':'fixture','grant_id':'fixture','socket':'fixture'}
    os.close(fd)
    a,b=socket.socketpair();failures=[]
    class Connection:
        def connect(self,address): assert address=='fixture'
        def __getattr__(self,name): return getattr(a,name)
    def peer():
        received=None
        try:
            if response_kind=='wrong_peer': return
            received=wire.receive_directory(b);request=wire.receive(b)
            from backend.core import macos_broker_capabilities as capabilities
            wire.send(b,capabilities.reply(request,{**binding,'owner_uid':os.getuid()},boot=[100,0]))
            request=wire.receive(b)
            assert (project/'input.txt').read_text()=='original'
            assert not (project/'output.txt').exists()
            identifier=request['invocation_id']
            wire.send(b,dict(event='started',invocation_id=identifier,pid=os.getpid()))
            if response_kind=='denied_after_start':
                wire.send(b,dict(event='denied',invocation_id=identifier,error='too late'));return
            image=request['image']
            image['files'].append(['output.txt',base64.b64encode(b'produced').decode(),0o644])
            if response_kind=='project_replaced':
                project.rename(project.with_name('held'));project.mkdir()
                (project/'input.txt').write_text('original')
            wire.send(b,dict(event='finished',invocation_id=identifier,exit_code=0,
                cleanup=dict(complete=response_kind!='unknown_cleanup',mode='macos_uid',invocation_id=identifier,issues=[]),
                stdout=json.dumps({'success':True,'data':{'error':'COMMAND_TIMEOUT','timeout':2} if response_kind=='timeout' else {'success':True,'stdout':'done','exit_code':0}}),
                stderr='',image=image))
        except Exception as exc: failures.append(exc)
        finally:
            if received is not None: os.close(received)
            b.close()
    monkeypatch.setattr(client.socket,'socket',lambda *a,**k:Connection())
    monkeypatch.setattr(wire,'peer_uid',lambda _:os.getuid() if response_kind=='wrong_peer' else 0)
    token=client._SESSION.set(binding);worker=threading.Thread(target=peer);worker.start()
    try:
        ctx,call=make(project)
        result=call({'argv':['/usr/bin/printf','fixture'],'timeout':5})
    finally:
        client._SESSION.reset(token);a.close();worker.join(3)
    assert not worker.is_alive() and not failures
    assert (project/'input.txt').read_text()=='original'
    if response_kind=='success':
        assert not result.is_error, result.formatted
        assert (project/'output.txt').read_text()=='produced'
        assert result.receipt['effect_state']=='committed'
        assert result.receipt['file_service_result']
        assert result.receipt['runtime_cleanup']['complete'] is True
    else:
        assert result.is_error and not (project/'output.txt').exists()
        assert result.receipt['effect_state']!='committed'
        if response_kind not in ('wrong_peer','project_replaced','timeout'):
            assert result.receipt['runtime_cleanup']['complete'] is False
        if response_kind=='project_replaced':
            assert result.receipt['runtime_cleanup']['complete'] is True
        if response_kind=='timeout':
            assert result.data['error']=='TOOL_TIMEOUT' and result.data['execution_state']=='timed_out'
            assert result.receipt['runtime_cleanup']['complete'] is True
