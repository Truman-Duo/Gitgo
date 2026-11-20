"""Administrator-only launchd lifecycle; the task socket cannot manage services.

Registrations and transaction records are root-owned. Admission is locked out
while replacing a helper. Unknown UID ownership blocks upgrade and removal.
Project authority stays bounded to its explicit boot/expiry; launchd residency
does not silently renew it. Publisher trust remains a separate release gate.
"""
from __future__ import annotations
from contextlib import contextmanager
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import fcntl

from pathlib import Path
import plistlib
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

from backend.core import macos_identity as identity
from backend.core.macos_helper_install import install, trusted_directory, verify_bundle_path, require_transition_trust
from backend.core.macos_file_authority import require_no_extended_acl

REGISTRY=Path('/private/var/db/gitgo-sandbox-v1')
JOBS=Path('/Library/LaunchDaemons')
PREFIX='org.gitgo.sandbox.'


def service_root(name):
    if type(name) is not str or not re.fullmatch('[a-z][a-z0-9-]{0,31}',name):
        raise ValueError('Invalid service name')
    return REGISTRY/name


def managed(root):
    root=Path(root)
    return root.parent==REGISTRY and root==service_root(root.name)


def require_admin():
    if sys.platform!='darwin' or os.getuid()!=0 or os.geteuid()!=0:
        raise PermissionError('Service management requires macOS administrator authority')


def configuration(root):
    trusted_directory(root/'private')
    state=json.loads(identity.bounded_read(root/'private/service.json',0,True))
    if (set(state)!={'schema','name','phase','helper','previous','candidate'} or state['schema']!=1
            or root!=service_root(state['name']) or state['phase'] not in ('active','switching','blocked','removing','removed')):
        raise ValueError('Invalid service registration; retain recovery records')
    for key in ('helper','previous','candidate'):
        if state[key] is not None:
            from backend.core.macos_helper_install import BASE
            p=Path(state[key])
            if (p.parent.parent.parent!=BASE or p.parent.name!='runtime' or p.name!='gitgo-host'
                    or not re.fullmatch('[0-9a-f]{64}',p.parent.parent.name)):
                raise ValueError('Invalid registered helper path')
    return state


def save(root,state):
    # Preserve transitions for diagnosis without discarding task/UID evidence.
    identity.atomic_json(root/'private'/('lifecycle-'+uuid.uuid4().hex+'.json'),
                         dict(recorded_ns=time.time_ns(),registration=state))
    identity.atomic_json(root/'private/service.json',state)


@contextmanager
def locked(root,name):
    trusted_directory(root/'private')
    fd=os.open(root/'private'/name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
            raise ValueError('Unsafe lifecycle lock')
        require_no_extended_acl(fd)
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            from backend.core.macos_broker_capabilities import BrokerBusyError
            raise BrokerBusyError('Service is busy; no lifecycle changes applied') from None
        yield
    finally: os.close(fd)


def admission_allowed(root):
    state=configuration(root)
    if state['phase']!='active' or state['helper']!=str(Path(sys.executable).absolute()):
        from backend.core.macos_broker_capabilities import BrokerMaintenanceError
        raise BrokerMaintenanceError('Service is in maintenance or this helper is superseded')


def validate_service_entry(root):
    state=configuration(root)
    expected=state['candidate'] if state['phase']=='switching' else state['helper']
    if state['phase'] not in ('active','switching') or expected!=str(Path(sys.executable).absolute()):
        raise ValueError('Service registration does not admit this runtime')


def assert_idle(root):
    """Read only. Never erase a lease or infer completion from a dead broker."""
    from backend.core.sandbox_macos import lease_root, load_state
    for path in (root/'private').glob('invocation-*.json'):
        binding=json.loads(identity.bounded_read(path,0,True))
        lease=binding.get('lease_id')
        if type(lease) is not str or not re.fullmatch('[0-9a-f]{32}',lease): raise ValueError('Invalid retained lease')
        directory=lease_root(binding);state=load_state(directory)
        if binding.get('lease_store')=='persistent-v1' and state['boot_id']!=identity.boot_id():
            from backend.core.macos_boot_recovery import verify_recovery
            verify_recovery(directory,state,identity.boot_id())
        if state['uid']!=binding['uid']: raise ValueError('Retained identity mismatch')
        if state['phase']!='clean' or identity.owned_rows(state['uid']):
            raise RuntimeError('UID cleanup is pending; lifecycle changes refused')
        if state['launch_count'] and (state['invocation_id']!=binding['invocation_id'] or
                state.get('cleanup',{}).get('observed_empty') is not True or state.get('cleanup',{}).get('issues')):
            raise RuntimeError('UID cleanup evidence is incomplete')


def job(root,helper):
    return dict(Label=PREFIX+root.name,ProgramArguments=[str(helper),'--gitgo-internal-role','macos-broker-service',str(root)],
        RunAtLoad=True,KeepAlive=True,ThrottleInterval=3,ExitTimeOut=5,Umask=0o077,
        StandardOutPath=str(root/'private/service.out'),StandardErrorPath=str(root/'private/service.err'))


def ctl(*args):
    return subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True,timeout=12)


def job_path(root): return JOBS/(PREFIX+root.name+'.plist')


def remove_job_file(root):
    path=job_path(root)
    if path.exists():
        path.unlink()
        directory=os.open(JOBS,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)


def check_job_owned(root,state):
    path=job_path(root)
    if not path.exists() and not path.is_symlink(): return
    raw=identity.bounded_read(path,0)
    current=plistlib.loads(raw)
    if not any(current==job(root,h) for h in (state['helper'],state['previous'],state['candidate']) if h):
        raise ValueError('Refusing to replace an unrelated launchd job')


def unload(root):
    target='system/'+PREFIX+root.name
    status=ctl('print',target)
    if status.returncode not in (0,113):
        raise RuntimeError('Could not inspect managed service: '+status.stderr[-1000:])
    if status.returncode==0:
        result=ctl('bootout',target)
        status=ctl('print',target)
        if result.returncode and status.returncode!=113:
            raise RuntimeError('Could not unload managed service: '+result.stderr[-1000:])
    # bootout acknowledges a request; the label can remain while launchd
    # terminates its process. Only service-not-found (113 from launchctl print), not an
    # arbitrary inspection error, proves the registration has disappeared.
    end=time.monotonic()+10
    while status.returncode!=113:
        if status.returncode!=0:
            raise RuntimeError('Could not confirm service removal: '+status.stderr[-1000:])
        if time.monotonic()>=end:
            raise RuntimeError('Managed service is still registered after bootout')
        time.sleep(.1);status=ctl('print',target)
    # A removed launchd label alone does not prove the process has exited.
    # Wait for lifetime socket ownership before accepting a new health reply.
    end=time.monotonic()+10
    while True:
        try:
            with locked(root,'broker.lock'): return
        except RuntimeError:
            if time.monotonic()>=end: raise RuntimeError('Previous broker still owns its socket') from None
            time.sleep(.1)


def write_job(root,helper):
    trusted_directory(JOBS)
    path=job_path(root);temp=JOBS/('.gitgo-'+uuid.uuid4().hex)
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(plistlib.dumps(job(root,helper)));stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        directory=os.open(JOBS,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temp.exists(): temp.unlink()


_STARTUP_FIELDS={'schema','nonce','boot','helper','pid','starts','phase','inspected',
                 'recovered','lock_waits','updated_ns','error'}
_STARTUP_PHASES={'launching','recover_busy','recovering','ready','failed'}
_STARTUP_ERRORS={None,'RECOVERY_LOCK_BUSY','RECOVERY_TIMEOUT','RECOVERY_FAILED','STARTUP_FAILED'}


def _startup_valid(value):
    from backend.core.macos_helper_install import BASE
    if (type(value) is not dict or set(value)!=_STARTUP_FIELDS
            or type(value['schema']) is not int or value['schema']!=1
            or type(value['nonce']) is not str or not re.fullmatch('[0-9a-f]{32}',value['nonce'])
            or not identity.valid_boot_id(value['boot']) or type(value['helper']) is not str
            or type(value['phase']) is not str or value['phase'] not in _STARTUP_PHASES
            or (value['error'] is not None and type(value['error']) is not str)
            or value['error'] not in _STARTUP_ERRORS):
        raise ValueError('Invalid startup diagnostic')
    helper=Path(value['helper'])
    if (helper.parent.parent.parent!=BASE or helper.parent.name!='runtime' or helper.name!='gitgo-host'
            or not re.fullmatch('[0-9a-f]{64}',helper.parent.parent.name)):
        raise ValueError('Invalid startup helper diagnostic')
    for key in ('pid','starts','inspected','recovered','lock_waits','updated_ns'):
        if type(value[key]) is not int or not 0<=value[key]<2**63:
            raise ValueError('Invalid startup progress diagnostic')
    if value['recovered']>value['inspected']:
        raise ValueError('Invalid startup recovery counters')
    return value


def startup_status(root):
    """Read bounded root evidence. This is never task or install authority."""
    from backend.core.macos_broker_protocol import unique_pairs
    trusted_directory(root/'private')
    try: raw=identity.bounded_read(root/'private/startup.json',0,True)
    except FileNotFoundError: return None  # Legacy helpers publish no progress.
    return _startup_valid(json.loads(raw,object_pairs_hook=unique_pairs))


def _startup_save(root,value):
    _startup_valid(value)
    trusted_directory(root/'private')
    identity.atomic_json(root/'private/startup.json',value)
    return value


def prepare_startup(root,helper):
    """Explicit management creates a new attempt before launchd bootstrap."""
    return _startup_save(root,dict(schema=1,nonce=uuid.uuid4().hex,boot=identity.boot_id(),
        helper=str(helper),pid=0,starts=0,phase='launching',inspected=0,recovered=0,
        lock_waits=0,updated_ns=time.monotonic_ns(),error=None))


def begin_startup(root,helper):
    """Called under broker.lock, after registration admits this exact runtime."""
    current=startup_status(root);boot=identity.boot_id()
    if current is None or current['boot']!=boot or current['helper']!=str(helper):
        current=prepare_startup(root,helper)
    return _startup_save(root,{**current,'pid':os.getpid(),'starts':current['starts']+1,
        'phase':'recovering','inspected':0,'recovered':0,'lock_waits':0,
        'updated_ns':time.monotonic_ns(),'error':None})


def startup_progress(root,attempt,*,phase,inspected=None,recovered=None,lock_waits=None,error=None):
    current=startup_status(root)
    # A stale process must not overwrite a new manager attempt's explanation.
    if current is None or any(current[k]!=attempt[k] for k in ('nonce','boot','helper','pid','starts')):
        raise RuntimeError('Startup attempt changed')
    value={**current,'phase':phase,'error':error,'updated_ns':time.monotonic_ns()}
    for key,item in (('inspected',inspected),('recovered',recovered),('lock_waits',lock_waits)):
        if item is not None:
            if type(item) is not int or item<current[key]: raise ValueError('Startup progress regressed')
            value[key]=item
    return _startup_save(root,value)


def ready(root,seconds=180,*,inactivity=40,expected=None):
    from backend.core import macos_broker_protocol as wire
    if (type(seconds) not in (int,float) or not 0<seconds<=180
            or type(inactivity) not in (int,float) or not 0<inactivity<=40):
        raise ValueError('Expected bounded service readiness budgets')
    end=time.monotonic()+seconds;idle=time.monotonic()+inactivity;updated=-1;detail='no current startup progress'
    while time.monotonic()<min(end,idle):
        if expected is not None:
            current=startup_status(root)
            if (current is not None and all(current[k]==expected[k] for k in ('nonce','boot','helper'))
                    and current['pid']>0 and current['starts']>0):
                detail=(f"phase={current['phase']} error={current['error']} "
                        f"inspected={current['inspected']} lock_waits={current['lock_waits']}")
                if current['phase']=='failed':
                    raise RuntimeError('Managed service startup failed: '+detail)
                now=time.monotonic_ns()
                if updated<current['updated_ns']<=now and current['phase'] in ('recover_busy','recovering','ready'):
                    # Timestamp age matters: repeatedly reading an old snapshot
                    # cannot extend inactivity, nor can any progress extend end.
                    idle=max(idle,current['updated_ns']/1_000_000_000+inactivity)
                    updated=current['updated_ns']
        remaining=min(end,idle)-time.monotonic()
        if remaining<=0: break
        peer=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);peer.settimeout(min(.5,remaining))
        try:
            peer.connect(str(root/'broker.sock'))
            if wire.peer_uid(peer)!=0: raise RuntimeError('Service peer is not root')
            if wire.receive_until(peer,min(end,idle,time.monotonic()+.5)).get('event')=='denied': return
            raise RuntimeError('Unexpected readiness response')
        except (OSError,EOFError): pass
        finally: peer.close()
        remaining=min(end,idle)-time.monotonic()
        if remaining>0: time.sleep(min(.1,remaining))
    # Do not add a blocking launchctl invocation after the hard deadline.
    raise TimeoutError('Managed service did not become ready: '+detail)


def launch(root,helper):
    verify_bundle_path(helper)
    attempt=prepare_startup(root,helper)
    write_job(root,helper)
    # Deliberate install/start also clears an administrator's prior disable.
    result=ctl('enable','system/'+PREFIX+root.name)
    if result.returncode: raise RuntimeError(result.stderr)
    result=ctl('bootstrap','system',str(job_path(root)))
    if result.returncode: raise RuntimeError('Service registration failed: '+result.stderr)
    ready(root,expected=attempt)


def switch(root,state,target):
    """Called with admin + admission locks. Roll back failed health checks."""
    from backend.core.macos_project_authority import pending
    if pending(root): raise ValueError('Finish or revoke the pending authorization before switching helpers')
    previous=state['helper'] if state['phase']=='active' else None
    check_job_owned(root,state);assert_idle(root)
    # Reject damaged candidates or unusable rollback images before journaling
    # maintenance or stopping the healthy service. launch() checks again at use.
    verify_bundle_path(target)
    if previous and str(previous)!=str(target): verify_bundle_path(previous)
    require_transition_trust(previous,target)
    transaction={**state,'phase':'switching','previous':previous,'candidate':str(target)}
    save(root,transaction)  # Durable maintenance gate precedes stopping old code.
    stage='stop_previous'
    try:
        unload(root);stage='launch_candidate';launch(root,target)
        stage='commit_candidate'
        result={**transaction,'phase':'active','helper':str(target),'candidate':None}
        save(root,result)
        return result
    except Exception as failure:
        detail=type(failure).__name__+': '+str(failure)
        try:
            identity.atomic_json(root/'private/lifecycle-failure.json',
                dict(stage=stage,candidate=str(target),previous=previous,error=detail[-8000:]))
        except OSError:
            pass  # Diagnostic failure must not prevent restoring the service.
        try:
            unload(root)
            if previous:
                restoring={**transaction,'candidate':previous}
                save(root,restoring);launch(root,previous)
                save(root,{**restoring,'phase':'active','helper':previous,'candidate':None})
            else:
                remove_job_file(root)
                save(root,{**transaction,'phase':'blocked','candidate':None})
        except Exception as recovery_failure:
            raise RuntimeError('Service change failed; recovery still required: '+str(recovery_failure)) from failure
        message='Service change failed; prior service restored' if previous else 'Install failed; service admission remains disabled'
        raise RuntimeError(message+'; '+stage+': '+detail) from failure


def register(name,bundle,project,uid,*,lifetime=180,allow_local_adhoc=False,expected_bundle_sha256=None):
    require_admin();root=service_root(name)
    trusted_directory(REGISTRY.parent)
    REGISTRY.mkdir(mode=0o711,exist_ok=True);trusted_directory(REGISTRY)
    if root.exists(): raise ValueError('Service name already has retained state; use status/upgrade/recover')
    if job_path(root).exists() or ctl('print','system/'+PREFIX+name).returncode==0:
        raise ValueError('Service label already exists')
    pin={} if expected_bundle_sha256 is None else dict(expected_bundle_sha256=expected_bundle_sha256)
    helper=install(bundle,allow_local_adhoc=allow_local_adhoc,**pin)
    from backend.core.macos_broker_service import provision
    provision(project,uid,lifetime=lifetime,root=root)
    initial=dict(schema=1,name=name,phase='blocked',helper=None,previous=None,candidate=None)
    save(root,initial)
    with locked(root,'admin.lock'),locked(root,'admission.lock'):
        return switch(root,initial,helper)


def upgrade(name,bundle,*,allow_local_adhoc=False,expected_bundle_sha256=None):
    require_admin();root=service_root(name)
    pin={} if expected_bundle_sha256 is None else dict(expected_bundle_sha256=expected_bundle_sha256)
    helper=install(bundle,allow_local_adhoc=allow_local_adhoc,**pin)
    with locked(root,'admin.lock'),locked(root,'admission.lock'):
        state=configuration(root)
        if state['phase']!='active': raise ValueError('Recover the pending lifecycle operation first')
        if state['helper']==str(helper):
            ready(root);return state
        return switch(root,state,helper)


def recover(name):
    """Idempotent admin retry after interrupted lifecycle management."""
    require_admin();root=service_root(name)
    with locked(root,'admin.lock'),locked(root,'admission.lock'):
        state=configuration(root);check_job_owned(root,state);assert_idle(root)
        if state['phase']=='removed': return state
        if state['phase']=='removing': return remove_locked(root,state)
        target=state['previous'] if state['phase']=='switching' else state['helper']
        # Recovery may be inspecting a live candidate. A damaged rollback image
        # must not cause us to stop that process before discovering the damage.
        if target is not None: verify_bundle_path(target)
        unload(root)
        if target is None:
            remove_job_file(root)
            result={**state,'phase':'blocked','candidate':None};save(root,result);return result
        save(root,{**state,'phase':'switching','candidate':target})
        launch(root,target)
        result={**state,'phase':'active','helper':target,'candidate':None}
        save(root,result);return result


def remove_locked(root,state):
    check_job_owned(root,state);assert_idle(root)
    save(root,{**state,'phase':'removing'})
    unload(root)
    remove_job_file(root)
    # Keep root evidence and immutable version images for shared users/rollback.
    for path in (root/'client.json',root/'broker.sock'):
        if path.exists() or path.is_symlink(): path.unlink()
    result={**state,'phase':'removed','candidate':None};save(root,result);return result


def uninstall(name):
    require_admin();root=service_root(name)
    with locked(root,'admin.lock'),locked(root,'admission.lock'):
        state=configuration(root)
        if state['phase']=='removed': return state
        return remove_locked(root,state)


def status(name):
    require_admin();root=service_root(name);state=configuration(root)
    result=ctl('print','system/'+PREFIX+name)
    from backend.core.macos_broker_service import grant,raw_time
    from backend.core.macos_project_authority import pending
    try:
        value=grant(root,live=False)
        authorized=(state['phase']=='active' and value['boot']==identity.boot_id()
                    and not value.get('revoked') and raw_time()<value['expires'] and not pending(root))
    except (ValueError,OSError): authorized=False
    return dict(registration=state,loaded=result.returncode==0,authorization_live=authorized,authorization_pending=pending(root),
                client_config=str(root/'client.json'),launchd=(result.stdout+result.stderr)[-6000:])
