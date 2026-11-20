"""Private macOS UID ownership primitives for the gated execution candidate.

No command admission, permissions, or root signalling of sampled PIDs here.
Unknown observations keep recovery debt. Numeric identities are never recycled.
"""
from __future__ import annotations
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import grp
    import pwd
    import resource

from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
import uuid

if __package__:
    from backend.core.macos_file_authority import require_no_extended_acl
else:
    from macos_file_authority import require_no_extended_acl

PYTHON = '/usr/bin/python3'
UID_MIN, UID_MAX = 200000000, 210000000
MAX_FILE = 256 * 1024

KERNEL_GROUPS = r'''
def kernel_groups():
    import ctypes
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    getgroups = lib.getgroups
    getgroups.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)]
    getgroups.restype = ctypes.c_int
    count = getgroups(0, None)
    if not 0 <= count <= 1024:
        raise OSError(ctypes.get_errno(), 'kernel group count failed')
    groups = (ctypes.c_uint32 * max(count, 1))()
    actual = getgroups(max(count, 1), groups)
    if not 0 <= actual <= count:
        raise OSError(ctypes.get_errno(), 'kernel group read failed')
    return list(groups[:actual])
'''


def valid_boot_id(value):
    return (type(value) is str and re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value) is not None
        and value != '00000000-0000-0000-0000-000000000000')


def boot_id():
    # Calendar boot time can be adjusted during the SAME boot. It is not an
    # incarnation identifier. Do not cache or fall back to kern.boottime.
    result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                            capture_output=True, text=True, timeout=3, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    value = result.stdout.strip().lower()
    if not valid_boot_id(value):
        raise RuntimeError('boot identity unavailable')
    return value

SIGNALS = KERNEL_GROUPS + r'''
import json, os, signal, sys
uid, sig, pids = json.loads(sys.argv[1])
if not (type(uid) is int and 200000000 <= uid < 210000000 and
        os.getuid() == os.geteuid() == os.getgid() == os.getegid() == uid and
        set(kernel_groups()) <= {uid} and sig in (signal.SIGSTOP, signal.SIGKILL) and
        all(type(pid) is int and pid > 1 for pid in pids)):
    raise SystemExit('refusing unsafe signal authority')
for pid in pids:
    try: os.kill(pid, sig)
    except (ProcessLookupError, PermissionError): pass
'''


def parse_census(output):
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 6:
            raise RuntimeError('incomplete process census')
        uid, ruid, svuid, pid, pgid = map(int, fields[:5])
        if min(uid, ruid, svuid) < 0 or pid < 1 or pgid < 0:
            raise RuntimeError('invalid process census')
        rows.append(dict(uid=uid, ruid=ruid, svuid=svuid, pid=pid,
                         pgid=pgid, state=fields[5]))
    if not rows or not any(row['pid'] == os.getpid() for row in rows):
        raise RuntimeError('process census does not include the observer')
    return rows


def census():
    result = subprocess.run(['/bin/ps', '-axo', 'uid=,ruid=,svuid=,pid=,pgid=,stat='],
                            capture_output=True, text=True, timeout=3, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    return parse_census(result.stdout)


def owned_rows(uid):
    # Saved identity matters as well as real/effective identity.
    return [row for row in census() if uid in (row['uid'], row['ruid'], row['svuid'])]


def choose_uid(used):
    occupied = {value for row in census() for value in (row['uid'], row['ruid'], row['svuid'])}
    for _ in range(100):
        uid = UID_MIN + secrets.randbelow(UID_MAX - UID_MIN)
        if uid in used or uid in occupied:
            continue
        try:
            pwd.getpwuid(uid)
        except KeyError:
            pass
        else:
            continue
        try:
            grp.getgrgid(uid)
        except KeyError:
            used.add(uid)
            return uid
    raise RuntimeError('no unused numeric task identity available')


def limits(policy=None):
    if policy is not None:
        if __package__:from backend.core.macos_resource_guard import validate
        else:from macos_resource_guard import validate
        policy=validate(policy)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cpu=policy['cpu_seconds'] if policy else 8
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    processes=policy['process_limit'] if policy else 64
    resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))


def low_spawn(command, uid, workspace, env, *, policy=None, **kwargs):
    if os.geteuid() != 0 or not UID_MIN <= uid < UID_MAX:
        raise RuntimeError('dedicated identity launch requires root and an experimental UID')
    if policy is not None:
        if __package__:from backend.core.macos_resource_guard import validate
        else:from macos_resource_guard import validate
        policy=validate(policy)
    # CoreFoundation's default-encoding initialization otherwise looks up the
    # user's home via directory services before main(). The sandbox denies
    # that IPC. Generate the hint for the actual task identity, never inherit
    # the controller's UID or grant directory-service access to the task.
    child_env = {**env, '__CF_USER_TEXT_ENCODING': f'0x{uid:X}:0:0'}
    return subprocess.Popen(command, cwd=workspace, env=child_env, user=uid, group=uid,
                            extra_groups=(), umask=0o077, start_new_session=True,
                            preexec_fn=lambda:limits(policy), **kwargs)



def bounded_read(path, owner=None, private=False):
    # A writer may atomically replace the name after open() and before fstat().
    # The pinned old inode then has nlink=0 despite being a valid private file.
    # Retry that observation only, never an operation or task launch. Retain the
    # original strict checks for owner, mode, type, size and multiple hard links.
    for attempt in range(3):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > MAX_FILE:
                raise RuntimeError('not a bounded regular evidence file')
            if owner is not None and info.st_uid != owner:
                raise RuntimeError('unexpected evidence owner')
            if private and info.st_mode & 0o077:
                raise RuntimeError('control file is not private')
            if info.st_nlink == 0:
                continue
            if owner == 0:
                require_no_extended_acl(fd)
            data = os.read(fd, MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise RuntimeError('evidence exceeds limit')
            return data
        finally:
            os.close(fd)
    raise RuntimeError('evidence changed repeatedly during atomic replacement')


def atomic_json(path, value):
    # Caller has validated a non-task-writable control directory. File and
    # directory fsync precede any task launch or completion acknowledgement.
    temp = path.with_name('.write-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(value, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink()



def send_uid_signal(uid, rows, sig):
    # cwd/env intentionally do not use task-writable directories during recovery.
    helper = low_spawn([PYTHON, '-I', '-c', SIGNALS,
                             json.dumps([uid, int(sig), [row['pid'] for row in rows]])],
                            uid, '/', {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        _, err = helper.communicate(timeout=2)
        if helper.returncode:
            raise RuntimeError('signal helper failed: ' + err.decode(errors='replace')[:500])
    finally:
        if helper.poll() is None:
            helper.kill()  # pinned direct child only
            helper.communicate(timeout=2)


def drain_uid(uid, reap=lambda: None, frozen_hook=lambda: None):
    """Bounded quiescence observation, NOT an atomic fork-admission barrier."""
    start = time.monotonic()
    stop_rounds = 0
    quiescent = False
    issues = []
    try:
        while time.monotonic() - start < 2:
            reap()
            rows = owned_rows(uid)
            if not rows:
                break
            send_uid_signal(uid, rows, signal.SIGSTOP)
            stop_rounds += 1
            time.sleep(.025)
            after = owned_rows(uid)
            if (after and {r['pid'] for r in rows} == {r['pid'] for r in after} and
                    all(r['state'].startswith(('T', 'Z')) for r in after)):
                quiescent = True
                frozen_hook()  # test crash with stopped tasks; launchd must recover
                break
    except Exception as exc:
        issues.append(str(exc))
    # Always attempt KILL, even if observation or freeze failed. Never leave
    # stopped tasks solely because they cannot reach their safety deadline.
    empty = 0
    while time.monotonic() - start < 6:
        reap()
        rows = owned_rows(uid)
        if rows:
            empty = 0
            send_uid_signal(uid, rows, signal.SIGKILL)
        else:
            empty += 1
            if empty == 3:
                return {'observed_empty': True, 'quiescence_observed': quiescent,
                        'stop_rounds': stop_rounds, 'issues': issues,
                        'seconds': time.monotonic() - start}
        time.sleep(.04)
    return {'observed_empty': False, 'quiescence_observed': quiescent,
            'stop_rounds': stop_rounds, 'issues': issues, 'remaining': owned_rows(uid),
            'seconds': time.monotonic() - start}
