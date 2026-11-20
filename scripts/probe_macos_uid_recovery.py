"""Bounded UID churn and launchd crash-recovery experiment, NOT a sandbox.

--run requires sudo. Loads temporary system launchd jobs from private /tmp
snapshots; never installs a LaunchDaemon or account. Task code always runs as
an unused numeric UID. --launchd-smoke tests restart only as the login user.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

# -I deliberately excludes cwd; only import this script's sibling. For launchd
# both source files are copied into a root-owned, non-task-writable directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_macos_task_uid as base

CASES = ('cancel', 'timeout', 'owner_crash', 'supervisor_crash',
         'guardian_crash', 'cleanup_crash', 'repeated_crash')
PHASES = ('prepared', 'starting', 'running', 'stopping', 'cleanup_pending', 'clean')
MAX_FILE = 256 * 1024
CLOCK_NAME = 'CLOCK_MONOTONIC_RAW'


def system_time():
    # macOS Python 3.9 time.monotonic has a per-process origin. Persist only
    # the kernel's shared clock so an exec/restart cannot reset elapsed time.
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) / 1_000_000_000


def within_task_deadline(state):
    if state.get('clock') != CLOCK_NAME:
        return False
    start, end = state.get('started'), state.get('finished')
    if type(start) not in (int, float) or type(end) not in (int, float):
        return False
    return 0 <= end - start < 20


def boot_id():
    result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'],
                            capture_output=True, text=True, timeout=3, check=True,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    match = re.match(r'\{ sec = (\d+), usec = (\d+) \}', result.stdout)
    if not match:
        raise RuntimeError('boot identity unavailable')
    return [int(value) for value in match.groups()]

SIGNALS = base.KERNEL_GROUPS + r'''
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

# Two detached producers, each with at most eight short-lived direct children.
# No exponential reproduction; all descendants share a fixed deadline. SIGSTOP
# can pause their timers, which is why a failed cleanup remains recovery debt.
CHURN = r'''
import json, os, signal, sys, time
lifetime = float(sys.argv[1])
assert 0 < lifetime <= 25
deadline = time.monotonic() + lifetime
fd = os.open('births.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
def event(kind):
    os.write(fd, (json.dumps({'kind':kind, 'pid':os.getpid(),
        'uid':os.getuid(), 'time':time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)/1000000000}) + '\n').encode())
def produce():
    os.setsid()
    if os.fork(): os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    event('producer')
    children = set()
    while time.monotonic() < deadline:
        for pid in list(children):
            if os.waitpid(pid, os.WNOHANG)[0]: children.remove(pid)
        if len(children) < 8:
            pid = os.fork()
            if pid == 0:
                os.setsid()
                event('birth')
                time.sleep(max(0, min(.55, deadline - time.monotonic())))
                os._exit(0)
            children.add(pid)
        time.sleep(.035)
    for pid in children: os.waitpid(pid, 0)
    os._exit(0)
starters = []
for _ in range(2):
    pid = os.fork()
    if pid == 0: produce()
    starters.append(pid)
for pid in starters: os.waitpid(pid, 0)
print(json.dumps({'pid':os.getpid(), 'uid':os.getuid()}), flush=True)
time.sleep(max(0, deadline - time.monotonic()))
'''


def bounded_read(path, owner=None, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
            raise RuntimeError('not a bounded regular evidence file')
        if owner is not None and info.st_uid != owner:
            raise RuntimeError('unexpected evidence owner')
        if private and info.st_mode & 0o077:
            raise RuntimeError('control file is not private')
        data = os.read(fd, MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise RuntimeError('evidence exceeds limit')
        return data
    finally:
        os.close(fd)


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


def load_state(root, owner=0):
    value = json.loads(bounded_read(root / 'control/state.json', owner, True))
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] not in (1, 2) or
            type(value.get('uid')) is not int or not base.UID_MIN <= value['uid'] < base.UID_MAX or
            value.get('case') not in CASES or value.get('phase') not in PHASES):
        raise RuntimeError('invalid recovery record; refusing launch and UID signals')
    for key in ('generation', 'launch_count', 'faults'):
        if type(value.get(key)) is not int or not 0 <= value[key] <= 100:
            raise RuntimeError('invalid recovery counters')
    if value['launch_count'] > 1 or (value['phase'] == 'prepared' and value['launch_count'] != 0):
        raise RuntimeError('task replay forbidden')
    boot = value.get('boot_id')
    if not isinstance(boot, list) or len(boot) != 2 or any(type(n) is not int or n < 0 for n in boot):
        raise RuntimeError('missing boot identity')
    if value['schema'] == 2 and value.get('clock') != CLOCK_NAME:
        raise RuntimeError('invalid shared clock identity')
    return value


def verify_root(root):
    if sys.platform != 'darwin' or os.getuid() != 0 or os.geteuid() != 0:
        raise RuntimeError('recovery experiment requires root on macOS')
    root = Path(root).absolute()
    if root.parent != Path('/private/tmp') or not root.name.startswith('gitgo-uid-recovery-'):
        raise RuntimeError('not an experiment directory')
    for path, private in ((root, False), (root / 'control', True), (root / 'code', True)):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                info.st_mode & (0o077 if private else 0o022)):
            raise RuntimeError('untrusted experiment directory')
    return root


def reserved_uids():
    # Quarantine identities recorded by this experiment for the whole boot,
    # including a collector crash before the service has spawned any process.
    # This is a temporary experiment reservation, not a production allocator.
    current = boot_id()
    reserved = set()
    for path in Path('/private/tmp').glob('gitgo-uid-recovery-*'):
        info = path.lstat()
        if info.st_uid != 0:
            continue
        root = verify_root(path)
        try:
            state = load_state(root)
        except FileNotFoundError:
            continue  # setup did not reach state creation; no task may launch
        if state['boot_id'] == current:
            reserved.add(state['uid'])
    return reserved


def update(root, state, **changes):
    state.update(changes)
    atomic_json(root / 'control/state.json', state)


def birth_summary(root, uid):
    try:
        data = bounded_read(root / 'work/births.jsonl', uid)
    except FileNotFoundError:
        return {'births': 0, 'producers': 0, 'last_birth': 0}
    # Append-only fixture writes may be in progress; only consume full lines.
    events = [json.loads(line) for line in data.split(b'\n')[:-1]]
    if any(e.get('uid') != uid or e.get('kind') not in ('producer', 'birth') for e in events):
        raise RuntimeError('invalid workload evidence')
    births = [e for e in events if e['kind'] == 'birth']
    return {'births': len(births), 'producers': sum(e['kind'] == 'producer' for e in events),
            'last_birth': max((e['time'] for e in births), default=0)}


def send_uid_signal(uid, rows, sig):
    # cwd/env intentionally do not use task-writable directories during recovery.
    helper = base.low_spawn([base.PYTHON, '-I', '-c', SIGNALS,
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
            rows = base.owned_rows(uid)
            if not rows:
                break
            send_uid_signal(uid, rows, signal.SIGSTOP)
            stop_rounds += 1
            time.sleep(.025)
            after = base.owned_rows(uid)
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
        rows = base.owned_rows(uid)
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
            'stop_rounds': stop_rounds, 'issues': issues, 'remaining': base.owned_rows(uid),
            'seconds': time.monotonic() - start}


def crash_once(root, state, maximum):
    if state['faults'] < maximum:
        update(root, state, faults=state['faults'] + 1)
        os.kill(os.getpid(), signal.SIGKILL)


def service(root, recover_only=False):
    root = verify_root(root)
    lock = os.open(root / 'control/runner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    child = None
    connection = None
    listener = None
    state = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        loaded_state = load_state(root)
        if loaded_state['boot_id'] != boot_id():
            raise RuntimeError('stale boot identity; refusing task launch and UID signals')
        state = loaded_state
        if state['phase'] == 'clean':
            return 0
        incarnations = state.get('incarnations', [])
        update(root, state, generation=state['generation'] + 1,
               incarnations=[*incarnations, {'pid': os.getpid(), 'ppid': os.getppid(),
                                            'time': system_time()}])
        # Once start is recorded, every subsequent service incarnation only
        # recovers. Crash between record and exec must NOT cause replay.
        if state['phase'] == 'prepared' and state['schema'] == 2 and not recover_only:
            address = root / 'control/owner.sock'
            if address.exists():
                address.unlink()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(address))
            listener.listen(1)
            listener.settimeout(6)
            connection, _ = listener.accept()
            connection.settimeout(3)
            if base.read_event(connection.fileno(), 3) != {'start': True}:
                raise RuntimeError('invalid owner start request')
            update(root, state, phase='starting', launch_count=1, started=system_time())
            child = base.low_spawn([base.PYTHON, '-I', '-c', CHURN, '25'], state['uid'], root / 'work',
                                   base.environment(root / 'work', {}), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ready = base.read_event(child.stdout.fileno(), 4)
            if ready.get('uid') != state['uid'] or ready.get('pid') != child.pid:
                raise RuntimeError('invalid task identity')
            deadline = time.monotonic() + 3
            before = birth_summary(root, state['uid'])
            while time.monotonic() < deadline:
                time.sleep(.08)
                progress = birth_summary(root, state['uid'])
                if progress['producers'] == 2 and progress['births'] >= before['births'] + 6:
                    break
            else:
                raise RuntimeError('bounded producers did not continuously spawn')
            rows = base.owned_rows(state['uid'])
            if not rows or any(r['uid'] != state['uid'] or r['ruid'] != state['uid'] or
                               r['svuid'] != state['uid'] for r in rows):
                raise RuntimeError('workload changed identity')
            update(root, state, phase='running', progress=progress,
                   task_pid=child.pid, live_at_ready=len(rows))
            end = time.monotonic() + (1 if state['case'] == 'timeout' else 6)
            reason = 'timeout'
            while time.monotonic() < end:
                if child.poll() is not None:
                    reason = 'task_exit'
                    break
                if select.select([connection], [], [], .03)[0]:
                    message = connection.recv(32)
                    if not message:
                        reason = 'owner_eof'
                    elif message == b'CANCEL':
                        reason = 'cancel'
                    elif message == b'KILL_TASK':
                        child.kill()
                        child.wait(timeout=2)
                        reason = 'supervisor_crash'
                    elif message == b'CRASH':
                        crash_once(root, state, 1)
                        raise RuntimeError('crash injection did not fire')
                    else:
                        raise RuntimeError('invalid owner command')
                    break
            update(root, state, reason=reason)
        else:
            update(root, state, reason='automatic_recovery' if not recover_only else 'emergency_recovery')
            if state['case'] == 'repeated_crash':
                crash_once(root, state, 2)
        update(root, state, phase='stopping', before_cleanup=birth_summary(root, state['uid']))
        frozen = (lambda: crash_once(root, state, 1)) if state['case'] == 'cleanup_crash' and not recover_only else lambda: None
        cleanup = drain_uid(state['uid'], child.poll if child else lambda: None, frozen)
        update(root, state, cleanup=cleanup, after_cleanup=birth_summary(root, state['uid']),
               finished=system_time(), phase='clean' if cleanup['observed_empty'] else 'cleanup_pending')
        return 0 if cleanup['observed_empty'] else 75
    except Exception as exc:
        if state is not None:
            update(root, state, phase='cleanup_pending', error=type(exc).__name__ + ': ' + str(exc))
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr, flush=True)
        return 75
    finally:
        if connection:
            connection.close()
        if listener:
            listener.close()
        if child:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()
            child.stderr.close()
        os.close(lock)


def plist(label, script, args, logs):
    return {'Label': label, 'ProgramArguments': [base.PYTHON, '-I', str(script), *map(str, args)],
            'KeepAlive': {'SuccessfulExit': False}, 'RunAtLoad': True, 'ThrottleInterval': 1,
            'ExitTimeOut': 3, 'Umask': 0o077,
            'EnvironmentVariables': {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
            'StandardOutPath': str(logs / 'stdout.log'), 'StandardErrorPath': str(logs / 'stderr.log')}


def launchctl(*args, check=True):
    return subprocess.run(['/bin/launchctl', *map(str, args)], capture_output=True,
                          text=True, timeout=8, check=check)


def wait_state(root, predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = load_state(root)
        if predicate(state):
            return state
        time.sleep(.03)
    raise TimeoutError('launchd recovery deadline: ' + json.dumps(load_state(root)))


def setup_case(case, uid):
    root = Path(tempfile.mkdtemp(prefix='gitgo-uid-recovery-', dir='/private/tmp'))
    root.chmod(0o711)
    for name in ('control', 'code'):
        (root / name).mkdir(mode=0o700)
    base.prepare_workspace(root, 'work', uid)
    for name in (Path(__file__).name, 'probe_macos_task_uid.py'):
        destination = root / 'code' / name
        destination.write_bytes(Path(__file__).with_name(name).read_bytes())
        destination.chmod(0o500)
    atomic_json(root / 'control/state.json', {'schema': 2, 'clock': CLOCK_NAME, 'uid': uid, 'case': case,
                'phase': 'prepared', 'generation': 0, 'launch_count': 0, 'faults': 0,
                'boot_id': boot_id()})
    label = 'org.gitgo.uid-probe.' + uuid.uuid4().hex
    job = root / 'control' / (label + '.plist')
    job.write_bytes(plistlib.dumps(plist(label, root / 'code' / Path(__file__).name,
                                      ['--service', root], root / 'control')))
    job.chmod(0o600)
    return root, label, job


def run_case(case, uid):
    root, label, job = setup_case(case, uid)
    result = {'case': case, 'uid': uid, 'artifacts_directory': str(root), 'passed': False}
    connection = None
    owner = None
    loaded = False
    try:
        loaded = True
        launchctl('bootstrap', 'system', job)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + 6
        while True:
            try:
                connection.connect(str(root / 'control/owner.sock'))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('service did not open owner socket')
                time.sleep(.04)
        connection.sendall(b'{"start":true}\n')
        ready = wait_state(root, lambda s: s['phase'] in ('running', 'cleanup_pending', 'clean'), 6)
        if ready['phase'] != 'running':
            raise RuntimeError('workload was not ready: ' + json.dumps(ready))
        # Confirm new births after readiness, immediately before cancellation
        # or crash. A finite static process tree cannot pass this assertion.
        time.sleep(.2)
        progress = birth_summary(root, uid)
        if progress['births'] <= ready['progress']['births']:
            raise RuntimeError('producer was not still spawning at fault injection')
        result['births_at_fault'] = progress['births']
        if case == 'owner_crash':
            owner = os.fork()
            if owner == 0:
                time.sleep(15)
                os._exit(0)
            connection.close()
            connection = None
            os.kill(owner, signal.SIGKILL)  # direct unreaped owner, not a sampled PID
            os.waitpid(owner, 0)
            owner = None
        elif case in ('guardian_crash', 'repeated_crash'):
            connection.sendall(b'CRASH')
        elif case == 'supervisor_crash':
            connection.sendall(b'KILL_TASK')
        elif case in ('cancel', 'cleanup_crash'):
            connection.sendall(b'CANCEL')
        state = wait_state(root, lambda s: s['phase'] == 'clean', 12)
        result['service'] = state
        result['remaining_before_observer_cleanup'] = base.owned_rows(uid)
        expected_generations = 3 if case == 'repeated_crash' else 2 if case in ('guardian_crash', 'cleanup_crash') else 1
        expected_reason = {'cancel': 'cancel', 'timeout': 'timeout', 'owner_crash': 'owner_eof',
                           'supervisor_crash': 'supervisor_crash'}.get(case, 'automatic_recovery')
        cleanup = state['cleanup']
        incarnations = state['incarnations']
        result['passed'] = (state['generation'] == expected_generations and state['launch_count'] == 1
                            and len({entry['pid'] for entry in incarnations}) == expected_generations
                            and all(entry['ppid'] == 1 for entry in incarnations)
                            and state.get('reason') == expected_reason and not state.get('error')
                            and cleanup['observed_empty'] and cleanup['quiescence_observed']
                            and not cleanup['issues'] and not result['remaining_before_observer_cleanup']
                            and within_task_deadline(state))
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        try:
            result['service'] = load_state(root)
            result['service_stderr'] = bounded_read(root / 'control/stderr.log', 0).decode(errors='replace')[-4000:] if (root / 'control/stderr.log').exists() else ''
        except Exception as diagnostic:
            result['diagnostic_error'] = str(diagnostic)
    finally:
        if connection:
            connection.close()
        if owner:
            os.kill(owner, signal.SIGKILL)
            os.waitpid(owner, 0)
        if loaded:
            try:
                unloaded = launchctl('bootout', 'system/' + label, check=False)
                result['job_unloaded'] = unloaded.returncode == 0
                if not result['job_unloaded']:
                    result['unload_error'] = unloaded.stderr
            except Exception as exc:
                result['job_unloaded'] = False
                result['unload_error'] = str(exc)
            if not result['job_unloaded']:
                result['passed'] = False
        # Observer cleanup is emergency housekeeping only. It cannot make the
        # service's automatic recovery test pass retroactively.
        try:
            remaining = base.owned_rows(uid)
            result['observer_cleanup_needed'] = bool(remaining)
            if remaining:
                result['passed'] = False
            # A failed bootout must not create two concurrent cleaners for the
            # same identity. Respect the service's ownership lock even here.
            recovery_lock = os.open(root / 'control/runner.lock',
                                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(recovery_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result['final_cleanup'] = drain_uid(uid)
            finally:
                os.close(recovery_lock)
        except Exception as exc:
            result['final_cleanup'] = {'observed_empty': False, 'error': str(exc)}
            result['passed'] = False
        result['passed'] = result['passed'] and result['final_cleanup']['observed_empty']
        if not result['final_cleanup']['observed_empty']:
            result['emergency_recovery_command'] = [base.PYTHON, '-I',
                        str(root / 'code' / Path(__file__).name), '--recover', str(root)]
        root.chmod(0o700)
    return result


def run_suite():
    if sys.platform != 'darwin' or os.getuid() != 0 or os.geteuid() != 0:
        raise RuntimeError('--run requires sudo on macOS')
    fd = os.open(base.LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    control = None
    control_root = None
    report = {'production_backend_enabled': False, 'cases': {}, 'passed': False}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError('unsafe suite lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        used = reserved_uids()
        control_uid = base.choose_uid(used)
        control_root = Path(tempfile.mkdtemp(prefix='gitgo-uid-control-', dir='/private/tmp'))
        control_root.chmod(0o711)
        work = base.prepare_workspace(control_root, 'work', control_uid)
        control = base.low_spawn(['/bin/sleep', '180'], control_uid, work, base.environment(work, {}),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for case in CASES:
            report['cases'][case] = run_case(case, base.choose_uid(used))
            if control.poll() is not None:
                raise RuntimeError('unrelated UID control did not survive')
            print('completed ' + case + ': ' + str(report['cases'][case]['passed']), file=sys.stderr, flush=True)
            if not report['cases'][case]['final_cleanup']['observed_empty']:
                raise RuntimeError('cleanup unresolved; refusing further tasks')
        report['unrelated_identity_survived'] = True
        report['passed'] = all(case['passed'] for case in report['cases'].values())
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        report['passed'] = False
    finally:
        if control:
            if control.poll() is None:
                control.kill()
            control.wait(timeout=2)
            try:
                report['control_cleanup'] = drain_uid(control_uid)
            except Exception as exc:
                report['control_cleanup'] = {'observed_empty': False, 'error': str(exc)}
            report['passed'] = report['passed'] and report['control_cleanup']['observed_empty']
        if control_root:
            control_root.chmod(0o700)
        os.close(fd)
    return report


def launchd_smoke():
    """Same launchd restart policy, no task UIDs or workload signals."""
    if sys.platform != 'darwin' or os.getuid() == 0:
        raise RuntimeError('run launchd smoke as the login user on macOS')
    root = Path(tempfile.mkdtemp(prefix='gitgo-launchd-smoke-', dir='/private/tmp'))
    root.chmod(0o700)
    code = root / 'smoke.py'
    code.write_text('import os, signal, sys\nfrom pathlib import Path\n'
                    'root=Path(sys.argv[1])\nmarker=root/"first"\n'
                    'if not marker.exists():\n marker.write_text(str(os.getpid()))\n os.kill(os.getpid(), signal.SIGKILL)\n'
                    '(root/"restarted").write_text(str(os.getpid()))\n')
    label = 'org.gitgo.uid-probe.smoke.' + uuid.uuid4().hex
    domain = 'gui/' + str(os.getuid())
    job = root / (label + '.plist')
    job.write_bytes(plistlib.dumps(plist(label, code, [root], root)))
    try:
        launchctl('bootstrap', domain, job)
        end = time.monotonic() + 12
        while not (root / 'restarted').exists() and time.monotonic() < end:
            time.sleep(.05)
        first = (root / 'first').read_text()
        second = (root / 'restarted').read_text()
        return {'launchd_restart_observed': first != second, 'task_uid_tested': False,
                'artifacts_directory': str(root), 'passed': first != second}
    finally:
        launchctl('bootout', domain + '/' + label, check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run', action='store_true')
    group.add_argument('--launchd-smoke', action='store_true')
    group.add_argument('--service', type=Path, help=argparse.SUPPRESS)
    group.add_argument('--recover', type=Path, help='emergency cleanup only, never replay a task')
    args = parser.parse_args(argv)
    try:
        if args.service or args.recover:
            return service(args.service or args.recover, recover_only=bool(args.recover))
        report = launchd_smoke() if args.launchd_smoke else run_suite()
        print(json.dumps(report, indent=2))
        return 0 if report['passed'] else 1
    except Exception as exc:
        print(json.dumps({'passed': False, 'error': str(exc), 'production_backend_enabled': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
