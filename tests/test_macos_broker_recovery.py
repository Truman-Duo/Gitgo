"""Bound cleanup evidence must never become a command outcome or replay grant."""
import json
import os
import socket
import sys
import threading

import pytest
if sys.platform=='win32': pytest.skip('POSIX broker',allow_module_level=True)
from backend.core import macos_broker_client as client, macos_broker_service as server
from backend.core import macos_broker_protocol as wire, macos_identity as identity, sandbox_macos
from backend.core.storage.native_invocations import NAMESPACE, NativeInvocationRecord, pending_native_invocations, reconcile_native_invocations
from tests.test_native_invocation_recovery import bound_store, invocation


@pytest.mark.parametrize('fault',['none','basis','grant','project','lease_uid','lease_boot','running','live_uid','launch_count'])
def test_root_status_needs_exact_lease_and_fresh_empty_identity(monkeypatch,tmp_path_factory,fault):
    approved=dict(grant_id='g',owner_uid=501,identity=[1,2,501],boot='boot')
    request=dict(invocation_id='a'*32,execution_basis='b'*64)
    mapping=dict(**request,grant_id='g',owner_uid=501,project_identity=[1,2,501],boot='boot',uid=identity.UID_MIN,lease_id='c'*32)
    state=dict(invocation_id='a'*32,uid=identity.UID_MIN,boot_id='boot',phase='clean',launch_count=1,
               cleanup=dict(observed_empty=True,issues=[]))
    if fault=='basis': mapping['execution_basis']='d'*64
    if fault=='grant': mapping['grant_id']='another'
    if fault=='project': mapping['project_identity']=[1,3,501]
    if fault=='lease_uid': state['uid']+=1
    if fault=='lease_boot': state['boot_id']='old'
    if fault=='running': state['phase']='running'
    if fault=='launch_count': state['launch_count']=0
    monkeypatch.setattr(identity,'bounded_read',lambda *a:json.dumps(mapping))
    monkeypatch.setattr(sandbox_macos,'load_state',lambda p:state)
    monkeypatch.setattr(identity,'owned_rows',lambda u:[{'pid':42}] if fault=='live_uid' else [])
    if fault in ('basis','grant','project','lease_uid','lease_boot'):
        with pytest.raises(ValueError): server.recovery_status(tmp_path_factory,request,approved)
    else:
        result=server.recovery_status(tmp_path_factory,request,approved)
        assert result['cleanup']['complete'] is (fault=='none')
        assert result['outcome']=='unknown' and 'stdout' not in result


@pytest.mark.parametrize('live',[True,False])
def test_revoked_grant_only_remains_readable_for_status(monkeypatch,tmp_path_factory,live):
    monkeypatch.setattr(server,'private_directory',lambda _:None)
    monkeypatch.setattr(identity,'bounded_read',lambda *a:json.dumps(dict(boot='boot',revoked=True,expires=1)))
    monkeypatch.setattr(identity,'boot_id',lambda:'boot')
    if live:
        with pytest.raises(ValueError): server.grant(tmp_path_factory)
    else: assert server.grant(tmp_path_factory,live=False)['revoked']


@pytest.mark.parametrize('fault',['none','wrong_peer','basis','project','boot','uid','incomplete','extra'])
def test_status_client_authenticates_exact_reply(monkeypatch,tmp_path_factory,fault):
    project=tmp_path_factory/'project';project.mkdir()
    fd=wire.open_directory(project)
    binding=dict(project=str(project),identity=wire.directory_identity(fd),grant_id='g',token='secret',socket='fixture')
    os.close(fd)
    item=dict(invocation_id='a'*32,execution_basis='b'*64,
              macos_broker_binding=dict(grant_id='g',project_identity=binding['identity']))
    a,b=socket.socketpair();errors=[]
    class Connection:
        def connect(self,address): assert address=='fixture'
        def __getattr__(self,key): return getattr(a,key)
    def peer():
        received=None
        try:
            if fault=='wrong_peer': return
            received=wire.receive_directory(b);request=wire.receive(b)
            assert set(request)=={'op','token','grant_id','invocation_id','execution_basis'}
            assert request['op']=='status'
            response=dict(event='status',invocation_id='a'*32,execution_basis='b'*64,grant_id='g',
                project_identity=binding['identity'],owner_uid=os.getuid(),boot='boot',outcome='unknown',
                uid=identity.UID_MIN,cleanup=dict(complete=True,mode='macos_uid',invocation_id='a'*32,issues=[]))
            if fault=='basis': response['execution_basis']='c'*64
            if fault=='project': response['project_identity']=[0,0,0]
            if fault=='boot': response['boot']='prior'
            if fault=='uid': response['uid']=0
            if fault=='extra': response['output']='not authoritative'
            if fault=='incomplete': response['cleanup']['complete']=False
            wire.send(b,response)
        except Exception as exc: errors.append(exc)
        finally:
            if received is not None: os.close(received)
            b.close()
    monkeypatch.setattr(client.socket,'socket',lambda *a,**k:Connection())
    monkeypatch.setattr(wire,'peer_uid',lambda _:501 if fault=='wrong_peer' else 0)
    monkeypatch.setattr(identity,'boot_id',lambda:'boot')
    token=client._SESSION.set(binding);worker=threading.Thread(target=peer);worker.start()
    try:
        if fault in ('none','incomplete'):
            result=client.recovered_cleanup(item)
            assert (result is not None)==(fault=='none')
        else:
            with pytest.raises(ValueError): client.recovered_cleanup(item)
    finally:
        client._SESSION.reset(token);a.close();worker.join(3)
    assert not errors and not worker.is_alive()


@pytest.mark.parametrize('fault',['none','unavailable','cas_race','active','unbound'])
def test_reconciliation_preserves_unknown_effects_and_original_record(bound_store,monkeypatch,fault):
    workspace,store=bound_store
    record=NativeInvocationRecord(store,workspace,invocation(workspace))
    record.prepare()
    if fault!='active': record.finish(dict(started=True,cleanup=dict(complete=False)))
    old=record.ref;identifier=record.record['invocation_id']
    monkeypatch.setattr(client,'active',lambda:fault!='unbound')
    calls=[]
    def status(item):
        calls.append(item)
        if fault=='unavailable': raise OSError('no root peer')
        if fault=='cas_race':
            newer={**item,'owner_note':'new evidence'}
            newref=store.put_blob(json.dumps(newer).encode())
            assert store.compare_state_ref(NAMESPACE,record.key,newref,expected_ref=old)
        return dict(cleanup=dict(complete=True,issues=[],mode='macos_uid',invocation_id=identifier),outcome='unknown')
    monkeypatch.setattr(client,'recovered_cleanup',status)
    try:
        changed=reconcile_native_invocations(store)
        ref=store.get_state_ref(NAMESPACE,record.key)
        saved=json.loads(store.read_blob(ref))
        if fault=='none':
            assert changed==[identifier]
            assert saved['recovered_outcome']=='unknown' and saved['runtime_cleanup']['complete']
            assert saved['phase']=='finished' and saved['exit_code'] is None
            assert saved['recovery_previous_ref']==old and json.loads(store.read_blob(old))['runtime_cleanup']['complete'] is False
            assert store.get_state_ref(NAMESPACE,'recovery/'+identifier)==ref
            findings=pending_native_invocations(store)
            assert findings[0]['code']=='NATIVE_OUTCOME_UNCONFIRMED' and findings[0]['effect_state']=='ambiguous'
            assert reconcile_native_invocations(store)==[] and len(calls)==1
            with pytest.raises(Exception,match='Unsettled'): NativeInvocationRecord(store,workspace,invocation(workspace)).prepare()
            from backend.core.loop.recovery import inspect_post_checkpoint_invocations
            assert inspect_post_checkpoint_invocations(workspace,'process','',storage=store,paths=store.paths)[0]['code']=='NATIVE_OUTCOME_UNCONFIRMED'
        else:
            assert not changed and not saved.get('recovered_outcome')
            assert store.get_state_ref(NAMESPACE,'recovery/'+identifier) is None
            if fault=='cas_race': assert saved['owner_note']=='new evidence'
            else: assert ref==old
        if fault in ('active','unbound'): assert not calls
    finally:
        if fault=='active': record.finish(dict(started=True,cleanup=dict(complete=False)))


def test_pipeline_reconciles_before_existing_recovery_guard_without_executing(bound_store,monkeypatch):
    from backend.core.loop.agent_tool import AgentTool
    from backend.core.loop.execution_contract import NATIVE_PROCESS
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.tool_pipeline import ToolPipeline
    workspace,store=bound_store
    record=NativeInvocationRecord(store,workspace,invocation(workspace));record.prepare()
    record.finish(dict(started=True,cleanup=dict(complete=False)))
    monkeypatch.setattr(client,'active',lambda:True)
    monkeypatch.setattr(client,'recovered_cleanup',lambda item:dict(cleanup=dict(complete=True,issues=[],
        mode='macos_uid',invocation_id=item['invocation_id']),outcome='unknown'))
    tool=AgentTool('native_fixture','fixture',{},lambda _:pytest.fail('must not replay'),execution_contract=NATIVE_PROCESS)
    process=AgentProcess('process','worker',RingLevel.RING_3,active_task_id='task',session=AgentSession(),
        worktree_path=str(workspace),tool_registry=ToolRegistry([tool.name]))
    process.recovery=dict(resume_forbidden=True,reasons=pending_native_invocations(store))
    ctx=ExecutionContext(process,process.session,str(workspace),storage=store)
    for _ in range(2):
        result=ToolPipeline().execute(dict(name=tool.name,args={}),tool,ctx,'after-crash')
        assert result.is_error and result.diagnostics['code']=='NATIVE_OUTCOME_UNCONFIRMED'
    assert process.recovery['resume_forbidden'] and process.recovery['requires_manual_verification']
    assert all(r['code']=='NATIVE_OUTCOME_UNCONFIRMED' for r in process.recovery['reasons'])
