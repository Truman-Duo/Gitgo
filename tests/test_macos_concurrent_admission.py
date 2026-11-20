"""Real flock ownership and owner death; no claim of root/Seatbelt acceptance."""
import sys
import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX identity and file operations', allow_module_level=True)

import fcntl
import os
import subprocess
from types import SimpleNamespace
from backend.core import sandbox_macos as mac

BOOT='1712b6c7-f85c-42cc-9780-851da992c246'

@pytest.fixture
def owner(monkeypatch,tmp_path_factory):
    root=tmp_path_factory
    path=root/'admission.lock';path.touch(mode=0o600)
    state=dict(uid=200000001,phase='running',boot_id=BOOT)
    monkeypatch.setattr(mac.identity,'boot_id',lambda:BOOT)
    monkeypatch.setattr(mac,'load_state',lambda _:dict(state))
    # Exercise actual file locks as the ordinary test user. Root file validation
    # is separately tested below; no root process is simulated by this fixture.
    monkeypatch.setattr(mac,'lease_owner_lock',lambda root:os.open(root/'admission.lock',os.O_RDWR))
    child=subprocess.Popen([sys.executable,'-I','-c',
        'import fcntl,sys,time;f=open(sys.argv[1],"r+");fcntl.flock(f,fcntl.LOCK_EX);print("held",flush=True);time.sleep(20)',str(path)],
        stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='held'
        yield root,state,child
    finally:
        if child.poll() is None:child.kill()
        child.wait(timeout=3);child.stdout.close()

def test_live_owner_permits_concurrent_identity_reservation(owner):
    root,state,child=owner
    assert mac.live_lease_owner(root,state)

def test_dead_owner_never_bypasses_unfinished_recovery(owner):
    root,state,child=owner;child.kill();child.wait(timeout=3)
    assert not mac.live_lease_owner(root,state)

@pytest.mark.parametrize('change',[{'phase':'cleanup_pending'},{'boot_id':'ffffffff-ffff-ffff-ffff-ffffffffffff'}])
def test_live_lock_cannot_override_recovery_or_boot(owner,change):
    root,state,child=owner;state.update(change)
    assert not mac.live_lease_owner(root,state)

def test_state_changed_during_owner_check_is_rejected(owner,monkeypatch):
    root,state,child=owner
    monkeypatch.setattr(mac,'load_state',lambda _:dict(state,uid=200000002))
    assert not mac.live_lease_owner(root,state)

def test_legacy_record_without_owner_lock_blocks(owner):
    root,state,child=owner;(root/'admission.lock').unlink()
    assert not mac.live_lease_owner(root,state)

@pytest.mark.parametrize('fault',['owner','mode','links','type'])
def test_unsafe_owner_lock_is_rejected(monkeypatch,tmp_path_factory,fault):
    path=tmp_path_factory/'admission.lock';path.touch(mode=0o600)
    info=dict(st_mode=0o100600,st_uid=0,st_nlink=1)
    info.update({'owner':{'st_uid':501},'mode':{'st_mode':0o100644},'links':{'st_nlink':2},'type':{'st_mode':0o40600}}[fault])
    with monkeypatch.context() as patch:
        patch.setattr(mac.os,'fstat',lambda fd:SimpleNamespace(**info))
        with pytest.raises(RuntimeError,match='Unsafe lease owner lock'):mac.lease_owner_lock(tmp_path_factory)

def test_owner_lock_symlink_is_rejected(tmp_path_factory):
    target=tmp_path_factory/'target';target.touch()
    (tmp_path_factory/'admission.lock').symlink_to(target)
    with pytest.raises(OSError):mac.lease_owner_lock(tmp_path_factory)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Actual Darwin execution-trial admission')
def test_allocation_lock_released_while_lease_owner_remains_held(monkeypatch,tmp_path_factory):
    import json
    import tempfile
    from pathlib import Path
    from contextlib import ExitStack
    store=tmp_path_factory/'leases';durable=tmp_path_factory/'durable';lock=tmp_path_factory/'allocator.lock'
    monkeypatch.setattr(mac,'STORE',store);monkeypatch.setattr(mac,'DURABLE_STORE',durable);monkeypatch.setattr(mac,'LOCK',str(lock))
    monkeypatch.setattr(mac,'private_directory',lambda _:None)
    if sys.platform!='darwin':
        monkeypatch.setattr(mac,'require_no_extended_acl',lambda fd:None)
    monkeypatch.setattr(mac.identity,'boot_id',lambda:BOOT)
    monkeypatch.setattr(mac.identity,'choose_uid',lambda used:next(n for n in range(200000001,200000010) if n not in used))
    monkeypatch.setattr(mac,'lease_owner_lock',lambda root,create=False:os.open(root/'admission.lock',os.O_RDWR|(os.O_CREAT|os.O_EXCL if create else 0),0o600))
    original_glob=Path.glob
    monkeypatch.setattr(Path,'glob',lambda p,pattern:iter(()) if p==Path('/private/tmp') and pattern=='gitgo-uid-recovery-*' else original_glob(p,pattern))
    monkeypatch.setattr(mac.identity,'bounded_read',lambda p,*args:p.read_bytes())
    original_stat=os.fstat
    with monkeypatch.context() as patch,ExitStack() as stack:
        patch.setattr(mac.os,'getuid',lambda:0);patch.setattr(mac.os,'geteuid',lambda:0)
        patch.setattr(mac.os,'chown',lambda *a:None)
        def root_stat(fd):
            s=original_stat(fd)
            return SimpleNamespace(**{**{name:getattr(s,name) for name in dir(s)
                if name.startswith('st_')},'st_uid':0})
        patch.setattr(mac.os,'fstat',root_stat)
        one=Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='gitgo-macos-chain-',dir='/private/tmp')))/'work';one.mkdir(mode=0o700)
        two=Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='gitgo-macos-chain-',dir='/private/tmp')))/'work';two.mkdir(mode=0o700)
        with mac.execution_trial(one) as first:
            allocation=os.open(lock,os.O_RDWR)
            try:fcntl.flock(allocation,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(allocation)
            with mac.execution_trial(two) as second:
                assert first['uid']!=second['uid']
                assert mac.live_lease_owner(first['root'],mac.load_state(first['root']))
        assert all(json.loads(p.read_text())['phase']=='clean' for p in store.glob('*/state.json'))
        # Restore stat before TemporaryDirectory cleanup in ExitStack.
        patch.undo()
