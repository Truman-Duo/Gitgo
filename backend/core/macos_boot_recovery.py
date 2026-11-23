"""Cross-boot retirement of durable task leases; never execute or signal.

Original launch facts remain archived. A changed boot alone is insufficient:
require a validated lease, repeated empty UID observations and stable boot.
Project grants are not changed by this module.
"""
from __future__ import annotations
import hashlib
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import fcntl

import re
import stat
import time

from backend.core import macos_identity as identity


class RecoveryBusy(RuntimeError):
    """A validated allocation lock stayed busy within the bounded startup wait."""


class RecoveryTimeout(RuntimeError):
    """Recovery did not finish its complete evidence scan within its budget."""


def _within(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise RecoveryTimeout('Boot recovery exceeded its bounded startup budget')


def valid_boot(value):
    return identity.valid_boot_id(value)


def verify_recovery(root,state,current,*,deadline=None):
    _within(deadline)
    proof=state.get('reboot_recovery') or {}
    if (not valid_boot(current) or not valid_boot(state.get('boot_id')) or state['boot_id']==current
            or proof.get('observed_boot')!=current or proof.get('origin_boot')!=state['boot_id']
            or proof.get('outcome')!='unknown' or proof.get('empty_observations')!=3
            or state['phase']!='clean' or state.get('cleanup')!={'observed_empty':True,'issues':[], 'mode':'reboot'}) :
        raise ValueError('Cross-boot cleanup is not verified')
    digest=proof.get('prior_sha256')
    if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest):
        raise ValueError('Invalid prior lease digest')
    raw=identity.bounded_read(root/('before-reboot-'+digest+'.json'),0,True)
    if hashlib.sha256(raw).hexdigest()!=digest: raise ValueError('Prior lease evidence changed')
    prior=json.loads(raw)
    for key in ('uid','invocation_id','boot_id','launch_count','generation'):
        if prior.get(key)!=state.get(key): raise ValueError('Prior lease identity changed')
    if identity.boot_id()!=current or identity.owned_rows(state['uid']):
        raise ValueError('Cross-boot UID is not empty or boot changed')
    _within(deadline)
    return proof


def recover_lease(root,current,*,deadline=None):
    _within(deadline)
    from backend.core.sandbox_macos import load_state
    state=load_state(root)
    if not valid_boot(current) or not valid_boot(state.get('boot_id')):
        raise ValueError('Unverifiable boot identity')
    if state['boot_id']==current: return False
    # UUIDs identify boot sessions, not chronological order. A different UUID
    # plus the archived root-owned lease and empty UID observations is required.
    if state.get('reboot_recovery',{}).get('observed_boot')==current:
        verify_recovery(root,state,current,deadline=deadline);return False
    if state['launch_count'] and not re.fullmatch('[0-9a-f]{32}',state.get('invocation_id','')):
        raise ValueError('Missing original invocation identity')
    # Never kill an identity observed after reboot: its ownership is unknown.
    for _ in range(3):
        _within(deadline)
        if identity.boot_id()!=current or identity.owned_rows(state['uid']):
            raise ValueError('Cross-boot UID observation is not empty')
        _within(deadline)
        time.sleep(.03)
    _within(deadline)
    raw=identity.bounded_read(root/'state.json',0,True)
    if json.loads(raw)!=state: raise ValueError('Lease changed during boot recovery')
    digest=hashlib.sha256(raw).hexdigest();archive=root/('before-reboot-'+digest+'.json')
    if archive.exists():
        if identity.bounded_read(archive,0,True)!=raw: raise ValueError('Prior archive differs')
    else:
        # atomic_json canonicalizes identically to the writer of state.json.
        identity.atomic_json(archive,state)
        if identity.bounded_read(archive,0,True)!=raw: raise ValueError('Prior archive encoding differs')
    if identity.boot_id()!=current: raise ValueError('Boot changed before recovery commit')
    _within(deadline)
    proof=dict(observed_boot=current,origin_boot=state['boot_id'],prior_sha256=digest,
               empty_observations=3,outcome='unknown')
    identity.atomic_json(root/'state.json',{**state,'phase':'clean',
        'cleanup':dict(observed_empty=True,issues=[],mode='reboot'),'reboot_recovery':proof})
    return True


def recover_store(*,seconds=120,lock_seconds=30,progress=None):
    """Bound startup recovery without treating contention as corrupt evidence.

    Every lease remains checked. Completed per-lease atomic commits survive a
    later timeout; no cursor or status file substitutes for recovery evidence.
    Existing bounded kernel-observation calls finish before deadline checks;
    the manager additionally imposes an independent hard readiness deadline.
    """
    from backend.core.sandbox_macos import DURABLE_STORE, LOCK, private_directory
    from backend.core.macos_helper_install import trusted_directory
    if os.getuid()!=0 or os.geteuid()!=0: raise PermissionError('Boot recovery requires root')
    if (type(seconds) not in (int,float) or not 0<seconds<=120
            or type(lock_seconds) not in (int,float) or not 0<lock_seconds<=30):
        raise ValueError('Expected bounded boot recovery and lock budgets')
    started=time.monotonic();deadline=started+seconds;lock_deadline=min(deadline,started+lock_seconds)
    inspected=lock_waits=0;changed=[];last_notice=-float('inf')
    def notice(phase,*,force=False):
        nonlocal last_notice
        now=time.monotonic()
        if progress is not None and (force or now-last_notice>=1):
            progress(dict(phase=phase,inspected=inspected,recovered=len(changed),lock_waits=lock_waits))
            last_notice=now
    trusted_directory(DURABLE_STORE.parent)
    DURABLE_STORE.mkdir(mode=0o700,exist_ok=True);private_directory(DURABLE_STORE)
    fd=os.open(LOCK,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
            raise ValueError('Unsafe allocation lock')
        identity.require_no_extended_acl(fd)
        while True:
            _within(deadline)
            if lock_waits and time.monotonic()>=lock_deadline:
                raise RecoveryBusy('Identity allocator remained busy during boot recovery')
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                lock_waits+=1;now=time.monotonic()
                notice('recover_busy')
                if now>=lock_deadline:
                    raise RecoveryBusy('Identity allocator remained busy during boot recovery') from None
                time.sleep(min(.1,lock_deadline-now))
        notice('recovering',force=True);_within(deadline)
        current=identity.boot_id();_within(deadline)
        for root in DURABLE_STORE.iterdir():
            _within(deadline)
            if not re.fullmatch('[0-9a-f]{32}',root.name): raise ValueError('Unknown durable lease directory')
            private_directory(root)
            # Historical timestamp-based leases cannot establish a boot
            # transition. Already-retired ones remain evidence only; an active
            # legacy lease requires explicit operator recovery, never inference.
            from backend.core.sandbox_macos import load_state
            state=load_state(root)
            if (not valid_boot(state.get('boot_id')) and state['phase']=='clean'
                    and state.get('cleanup',{}).get('observed_empty') is True
                    and state['cleanup'].get('issues')==[]):
                inspected+=1;notice('recovering');continue
            if recover_lease(root,current,deadline=deadline): changed.append(root.name)
            inspected+=1;notice('recovering');_within(deadline)
        notice('recovering',force=True);_within(deadline)
        return changed
    finally: os.close(fd)
