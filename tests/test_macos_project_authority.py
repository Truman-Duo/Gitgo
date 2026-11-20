"""Authorization changes keep old credentials from admitting new execution."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import pytest
if sys.platform=='win32': pytest.skip('Unix helper',allow_module_level=True)
from backend.core import macos_project_authority as auth, macos_helper_lifecycle as life
from backend.core import macos_broker_service as broker, macos_broker_protocol as wire, macos_identity as identity
from tests.test_macos_helper_lifecycle import rig


@pytest.fixture
def scope(rig,monkeypatch):
    root,state,events=rig;project=root/'project';project.mkdir()
    fd=wire.open_directory(project)
    try: key=wire.directory_identity(fd)
    finally: os.close(fd)
    old=dict(grant_id='a'*32,token_hash=hashlib.sha256(b'old').hexdigest(),boot='boot',expires=1,
             revoked=False,owner_uid=os.getuid(),identity=key,project=str(project))
    identity.atomic_json(root/'private/grant.json',old)
    identity.atomic_json(root/'private/seen.json',['b'*32])
    monkeypatch.setattr(broker,'private_directory',lambda p:None)
    monkeypatch.setattr(identity,'boot_id',lambda:'boot')
    monkeypatch.setattr(auth,'supports_renewal',lambda p:None)
    monkeypatch.setattr(os,'fchown',lambda *args:None)
    return root,old


def test_renew_rotates_both_credentials_without_resetting_history(scope):
    root,old=scope
    result=auth.renew('demo',lifetime=3600)
    current=broker.grant(root,live=False);client=json.loads((root/'client.json').read_bytes())
    assert result['authorization_live'] and not auth.pending(root)
    assert current['grant_id']!=old['grant_id'] and current['token_hash']!=old['token_hash']
    assert current['grant_id']==client['grant_id'] and hashlib.sha256(client['token'].encode()).hexdigest()==current['token_hash']
    assert current['project']==old['project'] and current['identity']==old['identity'] and current['boot']==old['boot']
    assert json.loads((root/'private/seen.json').read_text())==['b'*32]
    assert 3590<current['expires']-broker.raw_time()<=3600


@pytest.mark.parametrize('fault',['boot','directory','busy','dirty_lease','old_helper','lifetime'])
def test_renew_refuses_changed_scope_or_unfinished_execution(scope,monkeypatch,fault):
    root,old=scope;before=(root/'private/grant.json').read_bytes()
    def fail(*a): raise RuntimeError('refused')
    if fault=='boot': monkeypatch.setattr(identity,'boot_id',lambda:'new-boot')
    if fault=='directory':
        Path(old['project']).rename(root/'held');Path(old['project']).mkdir()
    if fault=='busy':
        @contextmanager
        def lock(r,name):
            if name=='admission.lock': raise RuntimeError('busy')
            yield
        monkeypatch.setattr(life,'locked',lock)
    if fault=='dirty_lease': monkeypatch.setattr(life,'assert_idle',fail)
    if fault=='old_helper': monkeypatch.setattr(auth,'supports_renewal',fail)
    with pytest.raises((ValueError,RuntimeError)): auth.renew('demo',lifetime=86401 if fault=='lifetime' else 3600)
    assert (root/'private/grant.json').read_bytes()==before and not auth.pending(root)


@pytest.mark.parametrize('point',['client','activation','clear'])
def test_interrupted_renewal_stays_closed_until_explicit_retry(scope,monkeypatch,point):
    root,old=scope;publish=auth.publish_client;atomic=identity.atomic_json;clear=auth.clear_pending
    def fail(*a): raise OSError('injected write failure')
    if point=='client': monkeypatch.setattr(auth,'publish_client',fail)
    elif point=='clear': monkeypatch.setattr(auth,'clear_pending',fail)
    else:
        def fail_activation(path,value):
            if path.name=='grant.json' and value.get('revoked') is False: fail()
            atomic(path,value)
        monkeypatch.setattr(identity,'atomic_json',fail_activation)
    with pytest.raises(OSError): auth.renew('demo')
    assert auth.pending(root)
    with pytest.raises(ValueError): broker.grant(root)
    with pytest.raises(ValueError,match='pending authorization'): life.switch(root,life.configuration(root),'unused')
    monkeypatch.setattr(auth,'publish_client',publish);monkeypatch.setattr(identity,'atomic_json',atomic);monkeypatch.setattr(auth,'clear_pending',clear)
    assert auth.renew('demo')['authorization_live'] and not auth.pending(root)


def test_revoke_is_durable_before_waiting_for_running_task(scope,monkeypatch):
    root,old=scope
    @contextmanager
    def lock(r,name):
        if name=='admission.lock': assert broker.grant(root,live=False)['revoked'] is True
        yield
    monkeypatch.setattr(life,'locked',lock)
    assert auth.revoke('demo')['cleanup_confirmed']
    assert auth.revoke('demo')['cleanup_confirmed']
    assert broker.grant(root,live=False)['revoked']


def test_revoke_never_reports_cleanup_when_uid_observation_fails(scope,monkeypatch):
    root,old=scope
    def unresolved(r): raise RuntimeError('unknown process census')
    monkeypatch.setattr(life,'assert_idle',unresolved)
    with pytest.raises(RuntimeError,match='cleanup remains unconfirmed'): auth.revoke('demo',cleanup_timeout=0)
    assert broker.grant(root,live=False)['revoked']


def test_new_boot_needs_explicit_reauthorization_and_clean_old_tasks(scope,monkeypatch):
    from backend.core import macos_project_identity as objects
    root,old=scope
    old['project_object']={'fixture':'persistent-object'}
    identity.atomic_json(root/'private/grant.json',old)
    monkeypatch.setattr(objects,'persistent_identity',lambda fd:old['project_object'])
    monkeypatch.setattr(identity,'boot_id',lambda:'new-boot')
    with pytest.raises(ValueError,match='Cross-boot'):auth.renew('demo')
    def dirty(root):raise RuntimeError('old lease not recovered')
    monkeypatch.setattr(life,'assert_idle',dirty)
    with pytest.raises(RuntimeError,match='not recovered'):auth.renew('demo',after_reboot=True)
    assert broker.grant(root,live=False)==old
    monkeypatch.setattr(life,'assert_idle',lambda root:None)
    assert auth.renew('demo',after_reboot=True)['authorization_live']
    assert broker.grant(root,live=False)['boot']=='new-boot'


def test_real_broker_rechecks_credentials_after_admission_lock(tmp_path_factory,monkeypatch):
    root=tmp_path_factory;(root/'private').mkdir();project=root/'project';project.mkdir()
    fd=wire.open_directory(project);key=wire.directory_identity(fd)
    old=dict(owner_uid=os.getuid(),identity=key,project=str(project),grant_id='a'*32,token_hash=hashlib.sha256(b'old').hexdigest())
    current=dict(old)
    request=dict(op='run',token='old',grant_id='a'*32,invocation_id='b'*32,execution_basis='c'*64,timeout=5,
       request=dict(tool_name='exec_command',args={'_workspace':'/role'},_native_sandbox_workspace='/role'),image=dict(files=[],directories=[]))
    client,server=socket.socketpair();wire.send_directory(client,fd);wire.send(client,request)
    class Stop(BaseException): pass
    class Listener:
        used=False
        def bind(self,address): Path(address).touch()
        def listen(self,n): pass
        def settimeout(self,n): pass
        def close(self): pass
        def accept(self):
            if self.used: raise Stop()
            self.used=True;return server,None
    monkeypatch.setattr(broker.socket,'socket',lambda *a:Listener())
    monkeypatch.setattr(broker,'grant',lambda *a,**k:dict(current))
    monkeypatch.setattr(identity,'bounded_read',lambda p,*a:Path(p).read_bytes())
    monkeypatch.setattr(os,'chown',lambda *a:None)
    monkeypatch.setattr(wire,'peer_uid',lambda c:os.getuid())
    monkeypatch.setattr(broker,'execute',lambda *a:pytest.fail('stale token executed'))
    @contextmanager
    def rotated(r,name):
        current.update(grant_id='d'*32,token_hash=hashlib.sha256(b'new').hexdigest());yield
    monkeypatch.setattr(life,'locked',rotated)
    try:
        with pytest.raises(Stop): broker.listen(root,persistent=True)
        assert wire.receive(client)['event']=='denied'
        assert json.loads((root/'private/seen.json').read_bytes())==[]
    finally:client.close();server.close();os.close(fd)


def test_new_grant_can_only_read_prior_status_for_same_project_owner_and_boot(tmp_path_factory,monkeypatch):
    from backend.core import sandbox_macos
    request=dict(invocation_id='b'*32,execution_basis='c'*64,original_grant_id='a'*32)
    approved=dict(grant_id='d'*32,owner_uid=501,identity=[1,2,501],boot='boot')
    mapping=dict(invocation_id='b'*32,execution_basis='c'*64,grant_id='a'*32,owner_uid=501,project_identity=[1,2,501],boot='boot',uid=identity.UID_MIN,lease_id='e'*32)
    state=dict(invocation_id='b'*32,uid=identity.UID_MIN,boot_id='boot',phase='clean',launch_count=1,cleanup=dict(observed_empty=True,issues=[]))
    monkeypatch.setattr(identity,'bounded_read',lambda *a:json.dumps(mapping))
    monkeypatch.setattr(sandbox_macos,'load_state',lambda *a:state);monkeypatch.setattr(identity,'owned_rows',lambda u:[])
    result=broker.recovery_status(tmp_path_factory,request,approved)
    assert result['grant_id']=='a'*32 and result['outcome']=='unknown' and result['cleanup']['complete']
    for field,value in [('owner_uid',502),('project_identity',[1,3,501]),('boot','another-boot')]:
        before=mapping[field];mapping[field]=value
        with pytest.raises(ValueError): broker.recovery_status(tmp_path_factory,request,approved)
        mapping[field]=before


@pytest.mark.parametrize('cross_boot',[False,True])
def test_new_client_authenticates_old_grant_cleanup_via_real_protocol(tmp_path_factory,monkeypatch,cross_boot):
    import threading
    from backend.core import macos_broker_client as client_module,sandbox_macos
    project=tmp_path_factory/'project';project.mkdir();fd=wire.open_directory(project)
    key=wire.directory_identity(fd);os.close(fd)
    binding=dict(project=str(project),identity=key,grant_id='d'*32,token='new',socket='fixture')
    old='a'*32;iid='b'*32;basis='c'*64
    item=dict(invocation_id=iid,execution_basis=basis,macos_broker_binding=dict(grant_id=old,project_identity=key))
    if cross_boot:item['macos_broker_binding']['boot']=[100,0]
    mapping=dict(invocation_id=iid,execution_basis=basis,grant_id=old,owner_uid=os.getuid(),project_identity=key,boot='boot',uid=identity.UID_MIN,lease_id='e'*32)
    state=dict(invocation_id=iid,uid=identity.UID_MIN,boot_id='boot',phase='clean',launch_count=1,cleanup=dict(observed_empty=True,issues=[]))
    approved=dict(owner_uid=os.getuid(),identity=key,project=str(project),grant_id=binding['grant_id'],boot='boot',token_hash=hashlib.sha256(b'new').hexdigest())
    a,b=socket.socketpair();errors=[]
    class Connection:
        def connect(self,address):assert address=='fixture'
        def __getattr__(self,key):return getattr(a,key)
    connection=Connection()
    monkeypatch.setattr(client_module.socket,'socket',lambda *a,**kw:connection)
    monkeypatch.setattr(wire,'peer_uid',lambda con:0 if con is connection else os.getuid())
    monkeypatch.setattr(identity,'boot_id',lambda:'boot')
    monkeypatch.setattr(identity,'bounded_read',lambda *a:json.dumps(mapping))
    monkeypatch.setattr(identity,'owned_rows',lambda uid:[])
    monkeypatch.setattr(sandbox_macos,'load_state',lambda *a:state)
    def serve():
        descriptor=None
        try:
            descriptor=wire.receive_directory(b);request=wire.receive(b)
            assert request['op']=='status' and request['grant_id']=='d'*32 and request['original_grant_id']==old
            broker.authorize(b,descriptor,request,approved)
            reply=broker.recovery_status(tmp_path_factory,request,approved)
            if cross_boot:reply['origin_boot']=[100,0]
            wire.send(b,reply)
        except Exception as exc:errors.append(exc)
        finally:
            if descriptor is not None:os.close(descriptor)
            b.close()
    token=client_module._SESSION.set(binding);worker=threading.Thread(target=serve);worker.start()
    try:
        result=client_module.recovered_cleanup(item)
        assert result['grant_id']==old and result['cleanup']['complete'] and result['outcome']=='unknown'
    finally:client_module._SESSION.reset(token);a.close();worker.join(3)
    assert not errors and not worker.is_alive()
