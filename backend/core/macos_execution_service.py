"""One sealed invocation, supervised by launchd; restart means cleanup only.

Internal entry for the administrator execution-chain trial. All mutable control
files and the copied service image are root-only. No general-purpose root RPC.
"""
from __future__ import annotations
import array
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import fcntl

from pathlib import Path
import select
import socket
import stat
import subprocess
import sys
import time

if __package__:
    from backend.core import macos_identity as identity
    from backend.core.macos_resource_guard import Guard,validate
    from backend.core.macos_coalition import CoalitionCPU
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import macos_identity as identity
    from macos_resource_guard import Guard,validate
    from macos_coalition import CoalitionCPU


def read_state(root):
    s = json.loads(identity.bounded_read(root / 'state.json', 0, True))
    if (type(s) is not dict or s.get('schema') != 1 or
            type(s.get('uid')) is not int or not identity.UID_MIN <= s['uid'] < identity.UID_MAX or
            s.get('phase') not in ('prepared', 'starting', 'running', 'cleanup_pending', 'clean') or
            type(s.get('launch_count')) is not int or s['launch_count'] not in (0, 1) or
            type(s.get('generation')) is not int or not 0 <= s['generation'] < 100 or
            s.get('clock') != 'CLOCK_MONOTONIC_RAW'):
        raise RuntimeError('Untrusted or stale execution lease')
    if not identity.valid_boot_id(s.get('boot_id')) or s['boot_id'] != identity.boot_id():
        raise RuntimeError('Execution lease boot session mismatch; retain recovery debt')
    if s['phase']=='prepared' and s['launch_count']:
        raise RuntimeError('Task replay forbidden')
    return s


def may_launch(state, recover_only=False):
    return (not recover_only and state['phase'] == 'prepared'
            and state['launch_count'] == 0 and state['generation'] == 1)


def serve(root, recover_only=False):
    root = Path(root).absolute()
    if (sys.platform != 'darwin' or os.getuid()!=0 or os.geteuid()!=0 or
            root.parent not in (Path('/private/tmp/gitgo-macos-execution-v1'),Path('/private/var/db/gitgo-execution-v1'))):
        raise RuntimeError('Invalid service entry')
    for path in (root.parent, root):
        directory=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            info=os.fstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o077:
                raise RuntimeError('Untrusted execution directory')
            identity.require_no_extended_acl(directory)
        finally: os.close(directory)
    lock=os.open(root/'service.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    child=connection=listener=None
    fds=[]; state=None
    def commit(**changes):
        state.update(changes)
        identity.atomic_json(root/'state.json',state)
    def now(): return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)/1e9
    try:
        info=os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077:
            raise RuntimeError('Unsafe execution service lock')
        identity.require_no_extended_acl(lock)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=read_state(root)
        if state['phase']=='clean': return 0
        commit(generation=state['generation']+1)
        if may_launch(state, recover_only):
            resources=validate(state.get('resources'))
            address=root/'owner.sock'
            if address.exists(): address.unlink()
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            listener.bind(str(address)); listener.listen(1); listener.settimeout(10)
            connection,_=listener.accept(); connection.settimeout(5)
            message,ancillary,flags,_=connection.recvmsg(2,socket.CMSG_SPACE(3*array.array('i').itemsize))
            for level,kind,data in ancillary:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    values=array.array('i'); values.frombytes(data); fds.extend(values)
            if message!=b'GO' or len(fds)!=3 or flags & socket.MSG_CTRUNC:
                raise RuntimeError('Expected exactly three private pipe descriptors')
            for fd in fds:
                os.set_inheritable(fd,False)
                if not stat.S_ISFIFO(os.fstat(fd).st_mode): raise RuntimeError('Only stdio pipes allowed')
            # Durably claim execution BEFORE exec; even an unacknowledged exec
            # may have effects. Every later incarnation drains without replay.
            guard=Guard(state['uid'],resources,cpu_ledger=CoalitionCPU())
            commit(phase='starting',launch_count=1,started=now())
            child=identity.low_spawn(['/usr/bin/sandbox-exec','-p',state['profile'],*state['argv']],
                state['uid'],state['cwd'],state['env'],policy=resources,stdin=fds[0],stdout=fds[1],stderr=fds[2],close_fds=True)
            for fd in fds: os.close(fd)
            fds=[]
            commit(phase='running',pid=child.pid)
            connection.sendall(('PID:'+str(child.pid)+'\n').encode())
            end=time.monotonic()+min(state['timeout'],1800)
            reason='timeout'
            next_sample=0
            while time.monotonic()<end:
                if time.monotonic()>=next_sample:
                    try: exceeded=guard.check()
                    except Exception as exc:
                        commit(resource_error=type(exc).__name__+': '+str(exc))
                        reason='resource_observation_failed';break
                    if exceeded:reason=exceeded;break
                    next_sample=time.monotonic()+.1
                if child.poll() is not None:
                    # Dead descendants remain in the coalition ledger. Take
                    # a final reading before allowing successful publication.
                    try: reason=guard.check() or 'task_exit'
                    except Exception as exc:
                        commit(resource_error=type(exc).__name__+': '+str(exc))
                        reason='resource_observation_failed'
                    break
                if select.select([connection],[],[],.03)[0]:
                    command=connection.recv(32)
                    reason='owner_eof' if not command else 'cancel'
                    break
            commit(reason=reason,exit_code=child.poll(),resource_observation=guard.evidence())
        else:
            commit(reason='automatic_recovery')
        commit(phase='cleanup_pending')
        cleanup=identity.drain_uid(state['uid'],child.poll if child else lambda:None)
        complete=cleanup['observed_empty'] and not cleanup['issues']
        commit(cleanup=cleanup,finished=now(),phase='clean' if complete else 'cleanup_pending')
        return 0 if complete else 75
    except Exception as exc:
        if state is not None:
            commit(phase='cleanup_pending',error=type(exc).__name__+': '+str(exc))
        print(str(exc),file=sys.stderr,flush=True)
        return 75
    finally:
        for fd in fds: os.close(fd)
        if connection: connection.close()
        if listener: listener.close()
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        os.close(lock)


if __name__=='__main__':
    raise SystemExit(serve(sys.argv[1],recover_only='--recover-only' in sys.argv[2:]))
