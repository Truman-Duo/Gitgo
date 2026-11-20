"""Explicit operator retirement of a provably unstarted temporary trial only.

Not exposed to task RPC. Never launches/signals tasks or recycles a UID.
Keeps original state and boot evidence; does not claim a reboot happened.
"""
from contextlib import contextmanager
import hashlib
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import fcntl

from pathlib import Path
import plistlib
import re
import stat
import subprocess
import time

from backend.core import macos_identity as identity
from backend.core import sandbox_macos as mac
from backend.core.macos_helper_install import verify_bundle_path


@contextmanager
def locked(path):
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
            raise ValueError('Unsafe retirement lock')
        identity.require_no_extended_acl(fd)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:os.close(fd)


def check_unstarted(state, *, source=False):
    if (state.get('phase')!='prepared' or type(state.get('launch_count')) is not int
            or state['launch_count']!=0 or type(state.get('generation')) is not int
            or state['generation']!=0 or 'pid' in state or 'started' in state):
        raise ValueError('Only a never-started generation-zero lease may be retired')
    if source:
        if not identity.valid_boot_id(state.get('boot_id')) or state['boot_id'] != identity.boot_id():
            raise ValueError('Source retirement requires the same kernel boot session')
        return
    legacy=state.get('boot_id')
    if not (type(legacy) is list and len(legacy)==2 and all(type(v) is int for v in legacy)
            and legacy[0]>0 and 0<=legacy[1]<1000000):
        raise ValueError('Expected legacy calendar-time boot identity')


def launchctl(*args):
    return subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True,timeout=15,
                          env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})


def verify_source_service(root, argv):
    if argv != [identity.PYTHON, '-I', str(root/'macos_execution_service.py'), str(root)]:
        raise ValueError('Unexpected source recovery service binding')
    for name in ('macos_identity.py', 'macos_execution_service.py', 'macos_resource_guard.py',
                 'macos_coalition.py', 'macos_file_authority.py'):
        path = root/name
        # The preflight bug specifically omitted the coalition module. Never
        # repair/restart this old job: validate the remaining code, unload it,
        # prove it never launched, and preserve its UID reservation instead.
        if name == 'macos_coalition.py' and not path.exists():
            continue
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_mode & 0o222):
            raise ValueError('Unsafe source recovery module')
        expected = Path(mac.__file__).with_name(name).read_bytes()
        if identity.bounded_read(path, 0, True) != expected:
            raise ValueError('Source recovery module differs; retain recovery debt')


def retire(identifier, *, source=False):
    if os.getuid()!=0 or os.geteuid()!=0:raise PermissionError('Operator retirement requires root')
    if not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError('Invalid temporary lease ID')
    root=mac.STORE/identifier
    mac.private_directory(root.parent);mac.private_directory(root)
    label='system/org.gitgo.execution-trial.'+identifier
    with locked(mac.LOCK),locked(root/'admission.lock'),locked(root/'service.lock'):
        state=mac.load_state(root)
        # Retry after an interrupted command only accepts our archived proof.
        proof=state.get('operator_retirement')
        original=state
        if proof is not None:
            digest=proof.get('prior_sha256','')
            if not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('Invalid retirement proof')
            raw=identity.bounded_read(root/('before-retirement-'+digest+'.json'),0,True)
            original=json.loads(raw)
            expected={**original,'phase':'clean','cleanup':dict(observed_empty=True,issues=[],mode='unstarted_operator'),
                      'operator_retirement':proof}
            if hashlib.sha256(raw).hexdigest()!=digest or state!=expected:
                raise ValueError('Retirement archive differs')
        check_unstarted(original, source=source)
        job=plistlib.loads(identity.bounded_read(root/'job.plist',0,True))
        argv=job.get('ProgramArguments',[])
        if job.get('Label')!=label.removeprefix('system/'):
            raise ValueError('Unexpected recovery service binding')
        if source:
            verify_source_service(root, argv)
        else:
            if len(argv)!=4 or argv[1:]!=['--gitgo-internal-role','macos-execution-service',str(root)]:
                raise ValueError('Unexpected recovery service binding')
            verify_bundle_path(argv[0])
        boot=identity.boot_id()
        for _ in range(3):
            if identity.boot_id()!=boot or identity.owned_rows(original['uid']):
                raise ValueError('UID not empty or boot changed; retain recovery debt')
            time.sleep(.04)
        status=launchctl('print',label)
        if status.returncode==0:
            # The file is bound above; check the live job has the same path and
            # executable before removing it. Never infer from its label alone.
            if ('path = '+str(root/'job.plist') not in status.stdout
                    or 'program = '+argv[0] not in status.stdout):
                raise ValueError('Loaded recovery job differs')
            stopped=launchctl('bootout',label)
            if stopped.returncode:raise RuntimeError('Could not unload unstarted recovery service: '+stopped.stderr)
        absent=launchctl('print',label)
        if absent.returncode!=113 or 'Could not find service' not in absent.stderr:
            raise RuntimeError('Recovery job absence not verified')
        if mac.load_state(root)!=state or identity.boot_id()!=boot or identity.owned_rows(original['uid']):
            raise ValueError('Lease changed; retain recovery debt')
        if proof is None:
            raw=identity.bounded_read(root/'state.json',0,True)
            digest=hashlib.sha256(raw).hexdigest()
            archive=root/('before-retirement-'+digest+'.json')
            if not archive.exists():identity.atomic_json(archive,state)
            if identity.bounded_read(archive,0,True)!=raw:raise ValueError('Retirement archive differs')
            proof=dict(prior_sha256=digest,observed_boot=boot,empty_observations=3,
                       outcome='never_started',method='explicit_operator')
            identity.atomic_json(root/'state.json',{**state,'phase':'clean',
                'cleanup':dict(observed_empty=True,issues=[],mode='unstarted_operator'),
                'operator_retirement':proof})
        return dict(lease=str(root),uid=original['uid'],retired=True,job_unloaded=True,
                    launch_count=0,proof=proof)
