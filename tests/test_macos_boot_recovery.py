"""Boot transitions are simulated here; real restart acceptance is separate."""
import hashlib
import json
import os
from pathlib import Path
import sys
import pytest
if sys.platform!='darwin': pytest.skip('macOS boot recovery',allow_module_level=True)
from backend.core import macos_boot_recovery as recovery,macos_identity as identity,sandbox_macos as mac
from backend.core import macos_broker_service as broker


@pytest.fixture
def lease(tmp_path_factory,monkeypatch):
    root=tmp_path_factory/('a'*32)
    root.mkdir()
    state=dict(schema=1,uid=identity.UID_MIN,boot_id="ffffffff-0000-0000-0000-000000000001",phase='running',launch_count=1,
               invocation_id='b'*32,generation=1,workspace='/discarded/work',pid=999999)
    identity.atomic_json(root/'state.json',state)
    monkeypatch.setattr(mac,'private_directory',lambda p:None)
    monkeypatch.setattr(identity,'bounded_read',lambda p,*a:Path(p).read_bytes())
    monkeypatch.setattr(identity,'boot_id',lambda:"11111111-0000-0000-0000-000000000002")
    monkeypatch.setattr(identity,'owned_rows',lambda uid:[])
    monkeypatch.setattr(identity,'low_spawn',lambda *a,**k:pytest.fail('Recovery launched a child'))
    monkeypatch.setattr(identity,'send_uid_signal',lambda *a:pytest.fail('Recovery signalled a PID'))
    return root,state


def test_cross_boot_archives_exact_launch_facts_and_is_idempotent(lease):
    root,old=lease;raw=(root/'state.json').read_bytes()
    assert recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002")
    state=mac.load_state(root);proof=recovery.verify_recovery(root,state,"11111111-0000-0000-0000-000000000002")
    assert state['phase']=='clean' and state['boot_id']==old['boot_id'] and state['launch_count']==1
    assert (root/('before-reboot-'+hashlib.sha256(raw).hexdigest()+'.json')).read_bytes()==raw
    assert proof['outcome']=='unknown'
    current=(root/'state.json').read_bytes()
    assert not recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002") and (root/'state.json').read_bytes()==current


@pytest.mark.parametrize('fault',['same_boot','legacy_boot','bad_boot','live_uid','census_error','changed_boot'])
def test_no_retirement_on_insufficient_evidence(lease,monkeypatch,fault):
    root,state=lease
    if fault=='legacy_boot':state['boot_id']=[100,0]
    if fault=='bad_boot':state['boot_id']='old'
    identity.atomic_json(root/'state.json',state);raw=(root/'state.json').read_bytes()
    if fault=='live_uid':monkeypatch.setattr(identity,'owned_rows',lambda uid:[{'pid':99}])
    if fault=='census_error':
        def fail(uid):raise OSError('process observation unavailable')
        monkeypatch.setattr(identity,'owned_rows',fail)
    if fault=='changed_boot':monkeypatch.setattr(identity,'boot_id',lambda:"bbbbbbbb-0000-0000-0000-000000000004")
    if fault=='same_boot':assert not recovery.recover_lease(root,"ffffffff-0000-0000-0000-000000000001")
    else:
        with pytest.raises((ValueError,OSError)):recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002")
    assert (root/'state.json').read_bytes()==raw


@pytest.mark.parametrize('fault',['archive','uid','boot','count','proof','live_uid'])
def test_recovery_proof_is_bound_and_rechecks_current_uid(lease,monkeypatch,fault):
    root,old=lease;recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002");s=mac.load_state(root)
    if fault=='archive':next(root.glob('before-reboot-*')).write_text('{}')
    if fault=='uid':s['uid']+=1
    if fault=='boot':s['boot_id']="cccccccc-0000-0000-0000-000000000005"
    if fault=='count':s['launch_count']=0
    if fault=='proof':s['reboot_recovery']['observed_boot']="bbbbbbbb-0000-0000-0000-000000000004"
    if fault=='live_uid':monkeypatch.setattr(identity,'owned_rows',lambda uid:[{'pid':123}])
    with pytest.raises(ValueError):recovery.verify_recovery(root,s,"11111111-0000-0000-0000-000000000002")


def test_second_reboot_requires_new_observation_and_preserves_original_boot(lease,monkeypatch):
    root,old=lease;recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002");monkeypatch.setattr(identity,'boot_id',lambda:"aaaaaaaa-0000-0000-0000-000000000003")
    assert recovery.recover_lease(root,"aaaaaaaa-0000-0000-0000-000000000003");s=mac.load_state(root)
    assert s['boot_id']=="ffffffff-0000-0000-0000-000000000001" and len(list(root.glob('before-reboot-*')))==2
    recovery.verify_recovery(root,s,"aaaaaaaa-0000-0000-0000-000000000003")


def test_cross_boot_root_status_preserves_origin_and_never_supplies_result(lease,monkeypatch):
    root,old=lease;recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002");monkeypatch.setattr(mac,'DURABLE_STORE',root.parent)
    registration=root.parent/'service';(registration/'private').mkdir(parents=True)
    mapping=dict(invocation_id='b'*32,execution_basis='c'*64,grant_id='d'*32,owner_uid=501,
                 project_identity=[1,2,501],boot="ffffffff-0000-0000-0000-000000000001",uid=old['uid'],lease_id=root.name,lease_store='persistent-v1')
    identity.atomic_json(registration/'private'/('invocation-'+'b'*32+'.json'),mapping)
    request=dict(invocation_id='b'*32,execution_basis='c'*64,original_grant_id='d'*32)
    approved=dict(grant_id='e'*32,owner_uid=501,identity=[1,2,501],boot="11111111-0000-0000-0000-000000000002")
    receipt=broker.recovery_status(registration,request,approved)
    assert receipt['origin_boot']=="ffffffff-0000-0000-0000-000000000001" and receipt['boot']=="11111111-0000-0000-0000-000000000002"
    assert receipt['cleanup']['complete'] and receipt['outcome']=='unknown' and 'stdout' not in receipt


def test_unknown_store_or_lease_path_cannot_redirect_recovery():
    for binding in (dict(lease_id='../bad'),dict(lease_id='a'*32,lease_store='/tmp/other')):
        with pytest.raises(ValueError):mac.lease_root(binding)


@pytest.mark.parametrize('failed_write',['archive','state'])
def test_interrupted_persistence_does_not_retire_lease_and_retry_preserves_evidence(lease,monkeypatch,failed_write):
    root,old=lease;before=(root/'state.json').read_bytes();write=identity.atomic_json
    def interrupted(path,value):
        if (failed_write=='archive' and path.name.startswith('before-reboot-') or
                failed_write=='state' and path.name=='state.json'):
            raise OSError('injected interrupted write')
        write(path,value)
    monkeypatch.setattr(identity,'atomic_json',interrupted)
    with pytest.raises(OSError):recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002")
    assert (root/'state.json').read_bytes()==before
    monkeypatch.setattr(identity,'atomic_json',write)
    assert recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002")
    recovery.verify_recovery(root,mac.load_state(root),"11111111-0000-0000-0000-000000000002")


@pytest.mark.parametrize('fault',['none','changed_volume','changed_inode','changed_owner','changed_client_object'])
def test_real_protocol_reconciles_original_device_tuple_after_remount(lease,monkeypatch,fault):
    import socket,threading
    from backend.core import macos_project_identity as objects,macos_broker_client as client,macos_broker_protocol as wire
    root,old=lease;recovery.recover_lease(root,"11111111-0000-0000-0000-000000000002");monkeypatch.setattr(mac,'DURABLE_STORE',root.parent)
    project=root.parent/'project';project.mkdir();fd=wire.open_directory(project)
    key=wire.directory_identity(fd);anchor=objects.persistent_identity(fd);os.close(fd)
    previous=[key[0]-2,*key[1:]]
    binding=dict(project=str(project),identity=key,project_object=anchor,grant_id='e'*32,token='new',socket='fixture')
    approved=dict(binding,owner_uid=os.getuid(),boot="11111111-0000-0000-0000-000000000002",token_hash=hashlib.sha256(b'new').hexdigest())
    mapping=dict(invocation_id='b'*32,execution_basis='c'*64,grant_id='d'*32,owner_uid=os.getuid(),
        project_identity=previous,project_object=anchor,boot="ffffffff-0000-0000-0000-000000000001",uid=old['uid'],lease_id=root.name,lease_store='persistent-v1')
    if fault=='changed_volume':mapping['project_object']={**anchor,'volume_uuid':'0'*32}
    if fault=='changed_inode':mapping['project_identity']=[previous[0],previous[1]+1,previous[2]]
    if fault=='changed_owner':mapping['owner_uid']+=1
    registration=root.parent/'service';(registration/'private').mkdir(parents=True)
    identity.atomic_json(registration/'private'/('invocation-'+'b'*32+'.json'),mapping)
    item=dict(invocation_id='b'*32,execution_basis='c'*64,macos_broker_binding=dict(
        grant_id='d'*32,project_identity=previous,project_object=anchor,boot="ffffffff-0000-0000-0000-000000000001"))
    if fault=='changed_client_object':item['macos_broker_binding']['project_object']={**anchor,'volume_uuid':'f'*32}
    if fault=='changed_client_object':
        token=client._SESSION.set(binding)
        try:assert client.recovered_cleanup(item) is None
        finally:client._SESSION.reset(token)
        return
    a,b=socket.socketpair();errors=[]
    class Connection:
        def connect(self,address):assert address=='fixture'
        def __getattr__(self,key):return getattr(a,key)
    connection=Connection();monkeypatch.setattr(client.socket,'socket',lambda *a,**kw:connection)
    monkeypatch.setattr(wire,'peer_uid',lambda con:0 if con is connection else os.getuid())
    def serve():
        descriptor=None
        try:
            descriptor=wire.receive_directory(b);request=wire.receive(b)
            broker.authorize(b,descriptor,request,approved)
            try:response=broker.recovery_status(registration,request,approved)
            except ValueError:
                if fault=='none':raise
                response={'event':'denied'}
            wire.send(b,response)
        except Exception as exc:errors.append(exc)
        finally:
            if descriptor is not None:os.close(descriptor)
            b.close()
    token=client._SESSION.set(binding);worker=threading.Thread(target=serve);worker.start()
    try:
        if fault=='none':
            receipt=client.recovered_cleanup(item)
            assert receipt['project_identity']==previous and receipt['project_object']==anchor
            assert receipt['origin_boot']=="ffffffff-0000-0000-0000-000000000001" and receipt['boot']=="11111111-0000-0000-0000-000000000002"
            assert receipt['cleanup']['complete'] and receipt['outcome']=='unknown'
        else:
            with pytest.raises(ValueError):client.recovered_cleanup(item)
    finally:client._SESSION.reset(token);a.close();worker.join(3)
    assert not errors and not worker.is_alive()
