"""Lifecycle ordering and failure recovery. launchd acceptance is separate."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace
import pytest
if sys.platform=='win32': pytest.skip('Unix lifecycle',allow_module_level=True)
from backend.core import macos_helper_lifecycle as life
from backend.core import macos_broker_service as broker
from backend.core.macos_helper_install import BASE

OLD=str(BASE/('a'*64)/'runtime/gitgo-host')
NEW=str(BASE/('b'*64)/'runtime/gitgo-host')


@pytest.fixture
def rig(tmp_path_factory,monkeypatch):
    root=tmp_path_factory/'demo';(root/'private').mkdir(parents=True)
    jobs=tmp_path_factory/'jobs';jobs.mkdir()
    monkeypatch.setattr(life,'REGISTRY',tmp_path_factory)
    monkeypatch.setattr(life,'JOBS',jobs)
    monkeypatch.setattr(life,'require_admin',lambda:None)
    monkeypatch.setattr(life,'trusted_directory',lambda p:None)
    monkeypatch.setattr(life.identity,'bounded_read',lambda p,*a:Path(p).read_bytes())
    monkeypatch.setattr(life,'verify_bundle_path',lambda p:Path(p).parent.parent)
    monkeypatch.setattr(life,'require_transition_trust',lambda previous,target:None)
    monkeypatch.setattr(life,'assert_idle',lambda p:None)
    @contextmanager
    def no_lock(*args): yield
    monkeypatch.setattr(life,'locked',no_lock)
    state=dict(schema=1,name='demo',phase='active',helper=OLD,previous=None,candidate=None)
    life.save(root,state)
    events=[]
    monkeypatch.setattr(life,'unload',lambda r:events.append('unload'))
    def launch(r,h):
        # New tasks must remain blocked throughout readiness checking.
        assert life.configuration(r)['phase']=='switching'
        events.append(str(h));life.write_job(r,h)
    monkeypatch.setattr(life,'launch',launch)
    return root,state,events


@pytest.mark.parametrize('name',['../escape','x/y','Upper','', 'a'*33,'.','-bad'])
def test_names_cannot_escape_registry(name):
    with pytest.raises(ValueError): life.service_root(name)


def test_successful_switch_keeps_previous_and_journals_gate(rig):
    root,state,events=rig
    result=life.switch(root,state,NEW)
    assert events==['unload',NEW]
    assert result==life.configuration(root)
    assert result['phase']=='active' and result['helper']==NEW and result['previous']==OLD
    revisions=[json.loads(p.read_bytes())['registration'] for p in (root/'private').glob('lifecycle-*.json')]
    assert any(s['phase']=='switching' and s['candidate']==NEW for s in revisions)


def test_trust_downgrade_is_rejected_before_maintenance_or_stop(rig,monkeypatch):
    root,state,events=rig
    before={p.name:p.read_bytes() for p in (root/'private').iterdir()}
    def refuse(previous,target):
        assert (previous,target)==(OLD,NEW)
        raise ValueError('unpinned candidate')
    monkeypatch.setattr(life,'require_transition_trust',refuse)
    with pytest.raises(ValueError,match='unpinned'): life.switch(root,state,NEW)
    assert events==[] and life.configuration(root)==state
    assert {p.name:p.read_bytes() for p in (root/'private').iterdir()}==before


@pytest.mark.parametrize('damaged',[OLD,NEW])
def test_invalid_candidate_or_rollback_preserves_running_service(rig,monkeypatch,damaged):
    root,state,events=rig;life.write_job(root,OLD)
    before={path.name:path.read_bytes() for path in (root/'private').iterdir()}
    plist=life.job_path(root).read_bytes();checked=[]
    def verify(path):
        checked.append(str(path))
        if str(path)==damaged: raise ValueError('damaged immutable image')
    monkeypatch.setattr(life,'verify_bundle_path',verify)
    with pytest.raises(ValueError,match='damaged'): life.switch(root,state,NEW)
    assert not events and life.configuration(root)==state
    assert {path.name:path.read_bytes() for path in (root/'private').iterdir()}==before
    assert life.job_path(root).read_bytes()==plist
    assert checked[0]==NEW


@pytest.mark.parametrize('phase',['active','switching'])
def test_recovery_validates_target_before_stopping_current_service(rig,monkeypatch,phase):
    root,state,events=rig
    state={**state,'phase':phase,'helper':NEW,'previous':OLD,
           'candidate':NEW if phase=='switching' else None}
    life.save(root,state);life.write_job(root,NEW)
    before={path.name:path.read_bytes() for path in (root/'private').iterdir()}
    plist=life.job_path(root).read_bytes();checked=[]
    def verify(path):
        checked.append(str(path));raise ValueError('damaged recovery image')
    monkeypatch.setattr(life,'verify_bundle_path',verify)
    with pytest.raises(ValueError,match='damaged'): life.recover('demo')
    assert checked==[OLD if phase=='switching' else NEW]
    assert not events and life.configuration(root)==state
    assert life.job_path(root).read_bytes()==plist
    assert {path.name:path.read_bytes() for path in (root/'private').iterdir()}==before


def test_failed_candidate_restores_previous_service(rig,monkeypatch):
    root,state,events=rig
    launch=life.launch
    def fail(r,h):
        launch(r,h)
        if h==NEW: raise TimeoutError('candidate not ready')
    monkeypatch.setattr(life,'launch',fail)
    with pytest.raises(RuntimeError,match='prior service restored'): life.switch(root,state,NEW)
    assert events==['unload',NEW,'unload',OLD]
    assert life.configuration(root)['helper']==OLD
    assert life.configuration(root)['phase']=='active'
    assert plistlib.loads(life.job_path(root).read_bytes())==life.job(root,OLD)


def test_failed_rollback_remains_closed_and_admin_recovery_is_repeatable(rig,monkeypatch):
    root,state,events=rig
    launch=life.launch
    def fail(r,h):
        launch(r,h);raise TimeoutError('not ready')
    monkeypatch.setattr(life,'launch',fail)
    with pytest.raises(RuntimeError,match='recovery still required'): life.switch(root,state,NEW)
    assert life.configuration(root)['phase']=='switching'
    monkeypatch.setattr(sys,'executable',OLD)
    with pytest.raises(ValueError): life.admission_allowed(root)
    monkeypatch.setattr(life,'launch',launch)
    assert life.recover('demo')['helper']==OLD
    assert life.recover('demo')['helper']==OLD


def test_pending_cleanup_prevents_stopping_service(rig,monkeypatch):
    root,state,events=rig
    def busy(r): raise RuntimeError('UID cleanup is pending')
    monkeypatch.setattr(life,'assert_idle',busy)
    with pytest.raises(RuntimeError,match='pending'): life.switch(root,state,NEW)
    with pytest.raises(RuntimeError,match='pending'): life.uninstall('demo')
    assert not events and life.configuration(root)==state


def test_admission_lock_blocks_upgrade_and_uninstall(rig,monkeypatch):
    root,state,events=rig
    monkeypatch.setattr(life,'install',lambda *a,**kw:NEW)
    @contextmanager
    def busy(r,name):
        if name=='admission.lock': raise RuntimeError('Service is busy')
        yield
    monkeypatch.setattr(life,'locked',busy)
    with pytest.raises(RuntimeError,match='busy'): life.upgrade('demo','bundle')
    with pytest.raises(RuntimeError,match='busy'): life.uninstall('demo')
    assert not events and life.configuration(root)==state


def test_uninstall_removes_entry_and_credentials_but_retains_evidence(rig):
    root,state,events=rig
    life.write_job(root,OLD)
    (root/'client.json').write_text('secret');(root/'broker.sock').write_text('socket fixture')
    (root/'private/evidence').write_text('retain')
    assert life.uninstall('demo')['phase']=='removed'
    assert life.uninstall('demo')['phase']=='removed'
    assert events==['unload']
    assert not life.job_path(root).exists() and not (root/'client.json').exists() and not (root/'broker.sock').exists()
    assert (root/'private/evidence').read_text()=='retain'


def test_unrelated_launchd_definition_is_never_replaced(rig):
    root,state,events=rig
    life.job_path(root).write_bytes(plistlib.dumps({'Label':'unrelated'}))
    with pytest.raises(ValueError,match='unrelated'): life.switch(root,state,NEW)
    assert not events


def test_superseded_runtime_and_maintenance_cannot_admit(rig,monkeypatch):
    root,state,events=rig
    monkeypatch.setattr(sys,'executable',NEW)
    with pytest.raises(ValueError): life.admission_allowed(root)
    monkeypatch.setattr(sys,'executable',OLD);life.admission_allowed(root)
    life.save(root,{**state,'phase':'switching','candidate':NEW})
    with pytest.raises(ValueError): life.validate_service_entry(root)
    monkeypatch.setattr(sys,'executable',NEW);life.validate_service_entry(root)
    with pytest.raises(ValueError): life.admission_allowed(root)


def test_postboot_residency_does_not_renew_authority(rig,monkeypatch):
    root,state,events=rig
    monkeypatch.setattr(broker,'private_directory',lambda p:None)
    monkeypatch.setattr(life.identity,'boot_id',lambda:'newboot')
    value=dict(boot='oldboot',expires=broker.raw_time()+100,revoked=False)
    (root/'private/grant.json').write_text(json.dumps(value))
    assert broker.grant(root,live=False)==value
    with pytest.raises(ValueError,match='expired'): broker.grant(root)
    monkeypatch.setattr(life,'ctl',lambda *a:SimpleNamespace(returncode=0,stdout='',stderr=''))
    assert life.status('demo')['authorization_live'] is False
    value['boot']='newboot';(root/'private/grant.json').write_text(json.dumps(value))
    # Administrator's Python path must not be mistaken for the service path.
    assert life.status('demo')['authorization_live'] is True


def test_launchd_is_resident_and_uses_only_fixed_arguments():
    root=life.service_root('demo');job=life.job(root,OLD)
    assert job['RunAtLoad'] is True and job['KeepAlive'] is True
    assert job['ProgramArguments']==[OLD,'--gitgo-internal-role','macos-broker-service',str(root)]


@pytest.mark.parametrize('fault',['none','live_uid','pending','issues','not_empty','wrong_iid','wrong_uid','missing'])
def test_idle_requires_exact_clean_lease_and_fresh_empty_census(tmp_path_factory,monkeypatch,fault):
    from backend.core import sandbox_macos
    root=tmp_path_factory;private=root/'private';private.mkdir()
    binding=dict(lease_id='a'*32,uid=200000001,invocation_id='b'*32)
    (private/('invocation-'+'b'*32+'.json')).write_text(json.dumps(binding))
    state=dict(uid=200000001,invocation_id='b'*32,phase='clean',launch_count=1,
        cleanup=dict(observed_empty=True,issues=[]))
    if fault=='pending': state['phase']='cleanup_pending'
    if fault=='issues': state['cleanup']['issues']=['unknown']
    if fault=='not_empty': state['cleanup']['observed_empty']=False
    if fault=='wrong_iid': state['invocation_id']='c'*32
    if fault=='wrong_uid': state['uid']=200000002
    def load(path):
        if fault=='missing': raise FileNotFoundError('missing evidence')
        return state
    monkeypatch.setattr(sandbox_macos,'load_state',load)
    monkeypatch.setattr(life.identity,'bounded_read',lambda p,*a:Path(p).read_bytes())
    monkeypatch.setattr(life.identity,'owned_rows',lambda uid:[{'pid':123}] if fault=='live_uid' else [])
    if fault=='none': life.assert_idle(root)
    else:
        with pytest.raises((RuntimeError,ValueError,FileNotFoundError)): life.assert_idle(root)


def test_actual_flock_excludes_other_admission_handle(tmp_path_factory,monkeypatch):
    root=tmp_path_factory;(root/'private').mkdir()
    monkeypatch.setattr(life,'trusted_directory',lambda p:None)
    if sys.platform!='darwin':
        monkeypatch.setattr(life,'require_no_extended_acl',lambda fd:None)
    original=os.fstat
    # Only root ownership is substituted. Descriptor, modes, links and kernel
    # flock are real; ordinary test execution must not require sudo.
    def as_root(fd):
        info=original(fd)
        return SimpleNamespace(**{**{name:getattr(info,name) for name in dir(info)
            if name.startswith('st_')},'st_uid':0})
    with monkeypatch.context() as patch:
        patch.setattr(os,'fstat',as_root)
        with life.locked(root,'admission.lock'):
            with pytest.raises(RuntimeError,match='busy'):
                with life.locked(root,'admission.lock'): pytest.fail('second holder')
        with life.locked(root,'admission.lock'): pass


def test_unload_waits_for_socket_owner_after_launchd_label_disappears(tmp_path_factory,monkeypatch):
    calls=[];held=[True,True,False]
    monkeypatch.setattr(life,'ctl',lambda *a:SimpleNamespace(returncode=113))
    monkeypatch.setattr(life.time,'sleep',lambda s:None)
    @contextmanager
    def ownership(root,name):
        calls.append(name)
        if held.pop(0): raise RuntimeError('busy')
        yield
    monkeypatch.setattr(life,'locked',ownership)
    life.unload(tmp_path_factory)
    assert calls==['broker.lock']*3


@pytest.mark.parametrize('fault',['none','inspection_error','timeout','bootout_error'])
def test_unload_waits_for_async_removal_and_refuses_uncertain_status(tmp_path_factory,monkeypatch,fault):
    clock=SimpleNamespace(now=0.)
    monkeypatch.setattr(life.time,'monotonic',lambda:clock.now)
    monkeypatch.setattr(life.time,'sleep',lambda s:setattr(clock,'now',clock.now+s))
    calls=[];prints=[0,0,0,113];locks=[]
    def ctl(*args):
        calls.append(args)
        if args[0]=='bootout': return SimpleNamespace(returncode=1 if fault=='bootout_error' else 0,stderr='bootout rejected')
        rc=prints.pop(0) if prints else 113
        if len(calls)>2 and fault=='inspection_error': rc=1
        if fault=='timeout': rc=0
        return SimpleNamespace(returncode=rc,stderr='inspection error' if rc==1 else '')
    @contextmanager
    def lock(root,name):
        locks.append(name);yield
    monkeypatch.setattr(life,'ctl',ctl);monkeypatch.setattr(life,'locked',lock)
    if fault=='none':
        life.unload(tmp_path_factory)
        assert clock.now>=.2 and locks==['broker.lock']
    else:
        with pytest.raises(RuntimeError): life.unload(tmp_path_factory)
        assert not locks


def test_failed_switch_preserves_root_cause_diagnostic(rig,monkeypatch):
    root,state,events=rig;launch=life.launch
    def fail(r,h):
        if h==NEW: raise TimeoutError('candidate boot failed')
        launch(r,h)
    monkeypatch.setattr(life,'launch',fail)
    with pytest.raises(RuntimeError,match='launch_candidate: TimeoutError: candidate boot failed'):
        life.switch(root,state,NEW)
    saved=json.loads((root/'private/lifecycle-failure.json').read_text())
    assert saved['candidate']==NEW and saved['previous']==OLD and saved['stage']=='launch_candidate'
