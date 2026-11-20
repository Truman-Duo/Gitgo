"""Bounded startup under allocator contention; no administrator writes.

Control-directory ownership is substituted in these unit fixtures. Flock,
atomic recovery records and diagnostic files are real. Frozen launchd/SIP
acceptance remains a separate VM check.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX startup recovery', allow_module_level=True)

import fcntl

from backend.core import macos_boot_recovery as recovery
from backend.core import macos_broker_protocol as wire
from backend.core import macos_broker_service as broker
from backend.core import macos_helper_install as install
from backend.core import macos_helper_lifecycle as life
from backend.core import macos_identity as identity
from backend.core import sandbox_macos as mac

BOOT='11111111-0000-0000-0000-000000000002'
OLD_BOOT='ffffffff-0000-0000-0000-000000000001'
HELPER=str(install.BASE/('a'*64)/'runtime/gitgo-host')


class Clock:
    def __init__(self): self.now=100.;self.on_sleep=lambda:None
    def monotonic(self): return self.now
    def monotonic_ns(self): return int(self.now*1_000_000_000)
    def sleep(self,seconds):
        assert seconds>=0
        self.now+=seconds;self.on_sleep()


def local_os(monkeypatch,module,**changes):
    monkeypatch.setattr(module,'os',SimpleNamespace(**{
        **{name:getattr(os,name) for name in dir(os)},**changes}))


def root_stat(fd):
    value=os.fstat(fd)
    return SimpleNamespace(**{**{name:getattr(value,name) for name in dir(value)
        if name.startswith('st_')},'st_uid':0})


@pytest.fixture
def store(tmp_path_factory,monkeypatch):
    root=tmp_path_factory/'leases';root.mkdir(mode=0o700)
    lock=tmp_path_factory/'allocator.lock';lock.touch(mode=0o600)
    monkeypatch.setattr(mac,'DURABLE_STORE',root)
    monkeypatch.setattr(mac,'LOCK',str(lock))
    monkeypatch.setattr(mac,'private_directory',lambda path:None)
    monkeypatch.setattr(install,'trusted_directory',lambda path:None)
    monkeypatch.setattr(identity,'require_no_extended_acl',lambda fd:None)
    monkeypatch.setattr(identity,'bounded_read',lambda path,*args:Path(path).read_bytes())
    monkeypatch.setattr(identity,'boot_id',lambda:BOOT)
    monkeypatch.setattr(identity,'owned_rows',lambda uid:[])
    local_os(monkeypatch,recovery,getuid=lambda:0,geteuid=lambda:0,fstat=root_stat)
    clock=Clock();monkeypatch.setattr(recovery,'time',clock)
    return root,lock,clock


def test_real_allocator_contention_waits_then_recovers_same_call(store):
    root,lock,clock=store
    holder=os.open(lock,os.O_RDWR);fcntl.flock(holder,fcntl.LOCK_EX|fcntl.LOCK_NB)
    opened=[holder];notices=[]
    def release():
        if clock.now>=100.3 and opened: os.close(opened.pop())
    clock.on_sleep=release
    try: assert recovery.recover_store(progress=lambda value:notices.append(dict(value)))==[]
    finally:
        if opened: os.close(opened.pop())
    assert 100.3<=clock.now<101
    assert notices[0]['phase']=='recover_busy' and notices[0]['lock_waits']>0
    assert notices[-1]['phase']=='recovering' and notices[-1]['lock_waits']>=3
    # The recovered call released the exact allocator lock, not a replacement.
    fd=os.open(lock,os.O_RDWR)
    try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    finally: os.close(fd)


def test_persistent_allocator_contention_has_fixed_deadline(store):
    root,lock,clock=store
    holder=os.open(lock,os.O_RDWR);fcntl.flock(holder,fcntl.LOCK_EX|fcntl.LOCK_NB)
    notices=[]
    try:
        with pytest.raises(recovery.RecoveryBusy):
            recovery.recover_store(lock_seconds=1,progress=notices.append)
    finally: os.close(holder)
    assert clock.now==pytest.approx(101.) and list(root.iterdir())==[]
    assert notices and all(value['phase']=='recover_busy' for value in notices)


def test_allocator_failure_other_than_contention_is_not_retried(store,monkeypatch):
    root,lock,clock=store
    def denied(*args): raise PermissionError('fixture allocator denied')
    monkeypatch.setattr(recovery,'fcntl',SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=denied))
    with pytest.raises(PermissionError): recovery.recover_store()
    assert clock.now==100.


@pytest.mark.parametrize('kwargs',[{'seconds':True},{'seconds':121},{'seconds':float('nan')},
    {'lock_seconds':0},{'lock_seconds':31},{'lock_seconds':float('inf')}])
def test_recovery_cannot_select_unbounded_or_ambiguous_budget(store,kwargs):
    with pytest.raises(ValueError,match='bounded'): recovery.recover_store(**kwargs)


def test_recovery_budget_preserves_atomic_commits_and_rechecks_on_retry(store):
    root,lock,clock=store
    for number in range(3):
        lease=root/f'{number:032x}';lease.mkdir()
        identity.atomic_json(lease/'state.json',dict(schema=1,uid=identity.UID_MIN+number,
            boot_id=OLD_BOOT,phase='running',launch_count=1,invocation_id='b'*32,generation=1))
    with pytest.raises(recovery.RecoveryTimeout): recovery.recover_store(seconds=.2)
    states=[json.loads(path.read_bytes()) for path in root.glob('*/state.json')]
    assert sum(state['phase']=='clean' for state in states)==2
    changed=recovery.recover_store()
    assert len(changed)==1
    assert all(json.loads(path.read_bytes())['phase']=='clean' for path in root.glob('*/state.json'))
    assert len(list(root.glob('*/before-reboot-*.json')))==3


@pytest.fixture
def startup(tmp_path_factory,monkeypatch):
    root=tmp_path_factory/'service';(root/'private').mkdir(parents=True)
    monkeypatch.setattr(life,'trusted_directory',lambda path:None)
    monkeypatch.setattr(identity,'bounded_read',lambda path,*args:Path(path).read_bytes())
    monkeypatch.setattr(identity,'boot_id',lambda:BOOT)
    clock=Clock();monkeypatch.setattr(life,'time',clock)
    attempt=life.prepare_startup(root,HELPER)
    return root,attempt,clock


def test_progress_preserves_attempt_and_busy_count_until_listener_ready(startup):
    root,prepared,clock=startup
    attempt=life.begin_startup(root,HELPER)
    assert attempt['nonce']==prepared['nonce'] and attempt['starts']==1 and attempt['pid']==os.getpid()
    busy=life.startup_progress(root,attempt,phase='recover_busy',lock_waits=1,error='RECOVERY_LOCK_BUSY')
    clock.sleep(.1)
    ready=life.startup_progress(root,attempt,phase='ready')
    assert ready['lock_waits']==1 and ready['error'] is None
    assert all(ready[key]==busy[key] for key in ('nonce','boot','helper','pid','starts'))
    assert ready['updated_ns']>busy['updated_ns']
    again=life.begin_startup(root,HELPER)
    assert again['nonce']==attempt['nonce'] and again['starts']==2
    life.prepare_startup(root,HELPER)
    with pytest.raises(RuntimeError,match='changed'):
        life.startup_progress(root,attempt,phase='ready')


def test_new_boot_creates_new_attempt_without_renewing_any_grant(startup,monkeypatch):
    root,prepared,clock=startup
    grant=root/'private/grant.json';grant.write_bytes(b'unchanged authority')
    monkeypatch.setattr(identity,'boot_id',lambda:OLD_BOOT)
    current=life.begin_startup(root,HELPER)
    assert current['boot']==OLD_BOOT and current['nonce']!=prepared['nonce'] and current['starts']==1
    assert grant.read_bytes()==b'unchanged authority'


@pytest.mark.parametrize('field,value',[('schema',True),('nonce','bad'),('boot','bad'),('helper','/tmp/other'),
    ('pid',True),('starts',-1),('phase','unknown'),('recovered',1),('error','arbitrary message')])
def test_diagnostic_rejects_malformed_fields(startup,field,value):
    root,prepared,clock=startup
    identity.atomic_json(root/'private/startup.json',{**prepared,field:value})
    with pytest.raises(ValueError): life.startup_status(root)


def test_diagnostic_rejects_duplicate_fields(startup):
    root,prepared,clock=startup
    path=root/'private/startup.json'
    path.write_text('{"schema":1,'+json.dumps(prepared)[1:])
    with pytest.raises(ValueError,match='Duplicate'): life.startup_status(root)


class UnavailableSocket:
    def settimeout(self,value): assert 0<value<=.5
    def connect(self,address): raise ConnectionRefusedError('fixture no listener')
    def close(self): pass


@pytest.mark.parametrize('kind,deadline',[('missing',40),('stale',40),('future',40),('old_snapshot',40),
                                       ('wrong_nonce',40),('wrong_boot',40),('wrong_helper',40),('progress',180)])
def test_ready_diagnostic_cannot_authorize_or_remove_absolute_deadline(startup,monkeypatch,kind,deadline):
    root,prepared,clock=startup
    current=life.begin_startup(root,HELPER)
    monkeypatch.setattr(life.socket,'socket',lambda *args:UnavailableSocket())
    monkeypatch.setattr(life,'ctl',lambda *args:pytest.fail('Blocking inspection after deadline'))
    def status(path):
        value={**current,'updated_ns':clock.monotonic_ns()}
        if kind=='missing': return None
        if kind in ('stale','old_snapshot'): value['updated_ns']=current['updated_ns']-(60_000_000_000 if kind=='stale' else 0)
        if kind=='future': value['updated_ns']+=10_000_000_000
        if kind=='wrong_nonce': value['nonce']='f'*32
        if kind=='wrong_boot': value['boot']=OLD_BOOT
        if kind=='wrong_helper': value['helper']=str(install.BASE/('b'*64)/'runtime/gitgo-host')
        return value
    monkeypatch.setattr(life,'startup_status',status)
    with pytest.raises(TimeoutError): life.ready(root,expected=prepared)
    assert clock.now==pytest.approx(100+deadline)


def test_ready_explicit_current_failure_ends_early(startup,monkeypatch):
    root,prepared,clock=startup;current=life.begin_startup(root,HELPER)
    life.startup_progress(root,current,phase='failed',error='RECOVERY_TIMEOUT')
    monkeypatch.setattr(life.socket,'socket',lambda *args:pytest.fail('Failed startup cannot be ready'))
    with pytest.raises(RuntimeError,match='RECOVERY_TIMEOUT'): life.ready(root,expected=prepared)
    assert clock.now==100


@pytest.mark.parametrize('peer,event,result',[(0,'denied','pass'),(501,'denied','peer'),(0,'ready','response')])
def test_ready_still_requires_root_socket_refusal(startup,monkeypatch,peer,event,result):
    root,prepared,clock=startup
    class Connected(UnavailableSocket):
        def connect(self,address): pass
    monkeypatch.setattr(life.socket,'socket',lambda *args:Connected())
    monkeypatch.setattr(wire,'peer_uid',lambda connection:peer)
    monkeypatch.setattr(wire,'receive_until',lambda connection,deadline:{'event':event})
    if result=='pass': assert life.ready(root,expected=prepared) is None
    else:
        with pytest.raises(RuntimeError,match=result): life.ready(root,expected=prepared)


@pytest.mark.parametrize('failure,code',[(recovery.RecoveryBusy,'RECOVERY_LOCK_BUSY'),
    (recovery.RecoveryTimeout,'RECOVERY_TIMEOUT'),(ValueError,'RECOVERY_FAILED')])
def test_broker_never_opens_socket_after_recovery_failure(startup,monkeypatch,failure,code):
    root,prepared,clock=startup
    local_os(monkeypatch,broker,getuid=lambda:0,geteuid=lambda:0,fstat=root_stat)
    monkeypatch.setattr(identity,'require_no_extended_acl',lambda fd:None)
    monkeypatch.setattr(life,'managed',lambda path:True)
    monkeypatch.setattr(life,'validate_service_entry',lambda path:None)
    monkeypatch.setattr(broker,'sys',SimpleNamespace(executable=HELPER))
    @contextmanager
    def lock(*args): yield
    monkeypatch.setattr(life,'locked',lock)
    def recover(**kwargs): raise failure('fixture failure')
    monkeypatch.setattr(recovery,'recover_store',recover)
    monkeypatch.setattr(broker,'listen',lambda *args,**kwargs:pytest.fail('Unrecovered socket published'))
    with pytest.raises(failure): broker.serve(root)
    status=life.startup_status(root)
    assert status['nonce']==prepared['nonce'] and status['starts']==1
    assert status['phase']=='failed' and status['error']==code


def test_broker_reports_ready_only_after_full_recovery_and_listener_callback(startup,monkeypatch):
    root,prepared,clock=startup
    local_os(monkeypatch,broker,getuid=lambda:0,geteuid=lambda:0,fstat=root_stat)
    monkeypatch.setattr(identity,'require_no_extended_acl',lambda fd:None)
    monkeypatch.setattr(life,'managed',lambda path:True)
    monkeypatch.setattr(life,'validate_service_entry',lambda path:None)
    monkeypatch.setattr(broker,'sys',SimpleNamespace(executable=HELPER))
    @contextmanager
    def lock(*args): yield
    monkeypatch.setattr(life,'locked',lock)
    def recover(*,progress):
        progress(dict(phase='recover_busy',inspected=0,recovered=0,lock_waits=1))
        busy=life.startup_status(root)
        assert busy['phase']=='recover_busy' and busy['error']=='RECOVERY_LOCK_BUSY'
        progress(dict(phase='recovering',inspected=5,recovered=2,lock_waits=1))
    monkeypatch.setattr(recovery,'recover_store',recover)
    def listen(path,*,persistent,on_ready):
        assert persistent and life.startup_status(path)['phase']=='recovering'
        on_ready()
        status=life.startup_status(path)
        assert status['phase']=='ready' and status['error'] is None and status['lock_waits']==1
        assert status['inspected']==5 and status['recovered']==2 and status['starts']==1
        assert status['nonce']==prepared['nonce'] and status['pid']==os.getpid()
        return 'listening'
    monkeypatch.setattr(broker,'listen',listen)
    assert broker.serve(root)=='listening'


def test_launch_prepares_fresh_binding_before_bootstrap(startup,monkeypatch):
    root,prior,clock=startup;events=[]
    monkeypatch.setattr(life,'verify_bundle_path',lambda path:events.append('verify'))
    monkeypatch.setattr(life,'write_job',lambda *args:events.append('job'))
    def ctl(*args):
        status=life.startup_status(root)
        assert status['nonce']!=prior['nonce'] and status['phase']=='launching'
        events.append(args[0]);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(life,'ctl',ctl)
    def ready(path,*,expected):
        assert expected==life.startup_status(root);events.append('ready')
    monkeypatch.setattr(life,'ready',ready)
    life.launch(root,HELPER)
    assert events==['verify','job','enable','bootstrap','ready']
