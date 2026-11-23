"""Opt-in macOS task-identity experiment; never a production sandbox.

--preflight needs no administrator privileges. --run requires root, launches
only the fixed fixtures below with fresh numeric UIDs/GIDs, and installs no
accounts or services. No tool or fixture is ever executed with root identity.
This is a bounded compatibility/lifetime experiment, NOT a hostile-code runner.
"""
from __future__ import annotations

import argparse
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import resource
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

UID_MIN, UID_MAX = 200_000_000, 210_000_000
MODES = ('complete', 'cancel', 'timeout', 'supervisor_crash', 'guardian_crash_recovery')
PYTHON = '/usr/bin/python3'
LOCK = '/private/tmp/gitgo-task-uid-probe-v1.lock'

# macOS Python os.getgroups uses getgroups$DARWIN_EXTSN, which consults the
# account database and fails with EINVAL for an unregistered numeric UID.
# The unextended libSystem symbol returns the kernel credential's groups.
# Do not turn a failed lookup into an empty list or skip the permission check.
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

# A sampled PID is never sent a signal by root. The kernel checks the dedicated
# UID again at signal delivery, including when a sampled PID has been reused.
# Do not use kill(-1), root pkill, or group/session identifiers from a census.
SIGNAL_HELPER = KERNEL_GROUPS + r'''
import json, os, signal, sys
uid, sig, targets = json.loads(sys.argv[1])
if not (200000000 <= uid < 210000000 and
        os.getuid() == os.geteuid() == os.getgid() == os.getegid() == uid and
        set(kernel_groups()) <= {uid} and sig == signal.SIGKILL and
        all(type(pid) is int and pid > 1 for pid in targets)):
    raise SystemExit('refusing unsafe identity or signal')
for pid in targets:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
'''

# Descendants have a safety lifetime if the ENTIRE test harness is killed.
# The assertions below require cleanup well before that lifetime expires.
NODE_CHILD = r'''
const fs = require('node:fs');
process.on('SIGTERM', () => {});
console.log(JSON.stringify({pid:process.pid, uid:process.getuid()}));
setTimeout(() => process.exit(0), Number(process.argv[1]) * 1000);
'''
DOUBLE_FORK = r'''
import json, os, signal, sys, time
if os.fork(): os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({'pid':os.getpid(), 'uid':os.getuid()}), flush=True)
time.sleep(float(sys.argv[1]))
'''
SUPERVISOR = KERNEL_GROUPS + r'''
import json, os, select, subprocess, sys
node, python, node_code, daemon_code, forbidden, uid, gid, lifetime = json.loads(sys.argv[1])
assert os.getuid() == os.geteuid() == uid and os.getgid() == os.getegid() == gid
assert 0 < lifetime <= 25
try:
    os.setuid(0)
except PermissionError:
    pass
else:
    raise RuntimeError('regained root identity')
try:
    open(forbidden, 'rb').read(1)
except PermissionError:
    pass
else:
    raise RuntimeError('read another identity private file')
children = []
for name, command, detached in [
    ('ordinary', [node, '-e', node_code, str(lifetime)], False),
    ('setsid', [node, '-e', node_code, str(lifetime)], True),
    ('double_fork', [python, '-I', '-c', daemon_code, str(lifetime)], False),
]:
    child = subprocess.Popen(command, start_new_session=detached,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if not select.select([child.stdout], [], [], 5)[0]:
        raise RuntimeError('child did not become ready: ' + name)
    info = json.loads(child.stdout.readline(2048))
    child.stdout.close()
    assert info['uid'] == os.getuid()
    children.append(dict(info, name=name))
print(json.dumps({'uid':os.getuid(), 'gid':os.getgid(), 'groups':kernel_groups(), 'pid':os.getpid(),
                  'pgid':os.getpgrp(), 'children':children}), flush=True)
# Owner control input represents completion/cancel; EOF also exits. Children
# deliberately outlive this supervisor, so the separate guardian must clean up.
if select.select([sys.stdin], [], [], 15)[0]:
    sys.stdin.readline()
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


def limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))


def low_spawn(command, uid, workspace, env, **kwargs):
    if os.geteuid() != 0 or not UID_MIN <= uid < UID_MAX:
        raise RuntimeError('dedicated identity launch requires root and an experimental UID')
    return subprocess.Popen(command, cwd=workspace, env=env, user=uid, group=uid,
                            extra_groups=(), umask=0o077, start_new_session=True,
                            preexec_fn=limits, **kwargs)


def cleanup_uid(uid, workspace, env, reap=lambda: None):
    started = time.monotonic()
    empty = 0
    while time.monotonic() - started < 5:
        reap()
        rows = owned_rows(uid)
        if not rows:
            empty += 1
            if empty == 3:
                return {'observed_empty': True, 'seconds': time.monotonic() - started}
        else:
            empty = 0
            helper = low_spawn([PYTHON, '-I', '-c', SIGNAL_HELPER,
                                json.dumps([uid, int(signal.SIGKILL), [r['pid'] for r in rows]])],
                               uid, workspace, env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _, errors = helper.communicate(timeout=2)
            if helper.returncode:
                raise RuntimeError('identity signal helper failed: ' + errors.decode()[:500])
        time.sleep(.05)
    return {'observed_empty': False, 'remaining': owned_rows(uid),
            'seconds': time.monotonic() - started}


def read_event(fd, timeout):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        part = os.read(fd, 1)
        if not part:
            raise RuntimeError('probe event stream closed before a complete event')
        data.extend(part)
        if len(data) > 8192:
            raise RuntimeError('oversized probe event')
        if part == b'\n':
            return json.loads(data)
    raise TimeoutError('probe event deadline exceeded')


def emit(fd, event):
    data = json.dumps(event).encode() + b'\n'
    while data:
        count = os.write(fd, data)
        data = data[count:]


def guardian(mode, uid, workspace, env, tools, forbidden, output):
    child = None
    result = {}
    try:
        child = low_spawn([PYTHON, '-I', '-c', SUPERVISOR,
                           json.dumps([tools['node'], PYTHON, NODE_CHILD, DOUBLE_FORK,
                                       str(forbidden), uid, uid, 25])],
                          uid, workspace, env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready = read_event(child.stdout.fileno(), 7)
        rows = owned_rows(uid)
        by_pid = {r['pid']: r for r in rows}
        assert ready['uid'] == ready['gid'] == uid
        assert set(ready['groups']) <= {uid}
        assert len(ready['children']) == 3
        for item in ready['children']:
            row = by_pid[item['pid']]
            assert row['uid'] == row['ruid'] == row['svuid'] == uid
            assert not row['state'].startswith('Z')
            assert (row['pgid'] == ready['pgid']) == (item['name'] == 'ordinary')
        emit(output, {'event': 'ready', 'identity': ready})
        if mode == 'guardian_crash_recovery':
            # The outer observer kills this guardian and performs a recovery
            # sweep. This does not prove automatic service restart/recovery.
            time.sleep(10)
            raise RuntimeError('guardian crash was not injected')
        if mode == 'supervisor_crash':
            child.kill()  # pinned direct child, not a sampled PID
            result['exit_code'] = child.wait(timeout=2)
            assert result['exit_code'] == -signal.SIGKILL
        elif mode == 'timeout':
            try:
                child.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                result['timeout_observed'] = True
            else:
                raise RuntimeError('supervisor unexpectedly exited before timeout')
        else:
            child.stdin.write((mode + '\n').encode())
            child.stdin.flush()
            result['exit_code'] = child.wait(timeout=2)
            assert result['exit_code'] == 0
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        if child and child.poll() is not None:
            result['supervisor_stderr'] = child.stderr.read(2048).decode(errors='replace')
    finally:
        try:
            result['cleanup'] = cleanup_uid(uid, workspace, env,
                                            reap=child.poll if child else lambda: None)
        except Exception as exc:
            result['cleanup'] = {'observed_empty': False, 'error': str(exc)}
        if child:
            if child.poll() is None:
                child.kill()  # direct-child fallback; does not prove tree cleanup
                child.wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        emit(output, {'event': 'result', **result})


def lifecycle_case(mode, uid, workspace, env, tools, forbidden):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            guardian(mode, uid, workspace, env, tools, forbidden, write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    reaped = False
    result = {'uid': uid}
    try:
        ready = read_event(read_fd, 10)
        if ready.get('event') != 'ready':
            raise RuntimeError('fixture startup failed: ' + json.dumps(ready))
        result['identity'] = ready['identity']
        if mode == 'guardian_crash_recovery':
            os.kill(pid, signal.SIGKILL)  # our unreaped direct child
            _, status = os.waitpid(pid, 0)
            reaped = True
            assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
            result['external_recovery_required'] = True
        else:
            result.update(read_event(read_fd, 10))
            # A result is sent at the very end; don't wait indefinitely on a
            # broken guardian. The finally block reaps the pinned child.
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        if not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(read_fd)
        result['recovery_cleanup'] = cleanup_uid(uid, workspace, env)
    result['passed'] = (not result.get('error') and result['recovery_cleanup']['observed_empty']
                        and result.get('cleanup', {'observed_empty': True})['observed_empty'])
    return result


def tool_commands(tools):
    node_code = "require('node:child_process').execFileSync('/bin/sh',['-c','printf tool-child-ok'],{stdio:'inherit'})"
    return {
        'python': [PYTHON, '-I', '-c', "import subprocess; subprocess.run(['/bin/sh','-c','printf tool-child-ok'], check=True)"],
        'node': [tools['node'], '-e', node_code],
        'bun': [tools['bun'], '-e', node_code],
        'npm': [tools['node'], tools['npm'], '--offline', 'run', 'probe'],
        'git': [tools['git'], '-c', 'user.name=Probe', '-c', 'user.email=probe@example.invalid',
                'commit', '--allow-empty', '-m', 'probe'],
    }


def tool_smoke(uid, workspace, env, tools, results=None):
    if results is None:
        results = {}
    commands = [('git_init', [tools['git'], 'init', '-q']), *tool_commands(tools).items()]
    for name, command in commands:
        proc = None
        try:
            proc = low_spawn(command, uid, workspace, env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = proc.communicate(timeout=8)
            results[name] = {'exit_code': proc.returncode, 'stdout': out.decode(errors='replace')[:2000],
                             'stderr': err.decode(errors='replace')[:1000],
                             'passed': proc.returncode == 0 and
                             (name == 'git_init' or b'tool-child-ok' in out + err)}
        except Exception as exc:
            results[name] = {'passed': False, 'error': str(exc)}
        finally:
            cleaned = cleanup_uid(uid, workspace, env, reap=proc.poll if proc else lambda: None)
            if proc:
                for stream in (proc.stdout, proc.stderr):
                    stream.close()
            if not cleaned['observed_empty']:
                raise RuntimeError('tool cleanup incomplete; refusing to start another tool')
        if name == 'git_init' and results[name]['passed']:
            # Write the hook as the task identity, never through a task-owned
            # path as root. No host Git configuration or credentials are used.
            hook = "from pathlib import Path; p=Path('.git/hooks/pre-commit'); p.write_text('#!/bin/sh\\nprintf tool-child-ok\\n'); p.chmod(0o700)"
            helper = low_spawn([PYTHON, '-I', '-c', hook], uid, workspace, env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _, error = helper.communicate(timeout=3)
            if helper.returncode:
                raise RuntimeError('could not create private test hook: ' + error.decode()[:500])
    return results


def environment(workspace, tools):
    path = ':'.join(dict.fromkeys([str(Path(p).parent) for p in tools.values()] + ['/usr/bin', '/bin']))
    return {'PATH': path, 'HOME': str(workspace), 'TMPDIR': str(workspace), 'LC_ALL': 'C',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
            'npm_config_cache': str(workspace / '.npm'), 'npm_config_update_notifier': 'false'}


def prepare_workspace(root, name, uid):
    workspace = root / name
    workspace.mkdir(mode=0o700)
    package = workspace / 'package.json'
    package.write_text(json.dumps({'name': 'gitgo-uid-probe', 'version': '0.0.0',
                                  'scripts': {'probe': 'node -e "process.stdout.write(\'tool-child-ok\')"'}}))
    os.chown(package, uid, uid)
    os.chown(workspace, uid, uid)
    return workspace


def resolve_tools():
    paths = {}
    search = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:' + os.environ.get('PATH', '')
    for name in ('node', 'bun', 'npm', 'git'):
        path = shutil.which(name, path=search)
        if not path:
            raise RuntimeError('required local tool missing: ' + name)
        # Keep the bin path (not npm's resolved JS path) in PATH for npm scripts.
        paths[name] = str(Path(path).absolute())
    return paths


def run_suite(tools):
    if sys.platform != 'darwin' or os.getuid() != 0 or os.geteuid() != 0:
        raise RuntimeError('--run requires sudo on macOS; no identities were changed')
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    work_root = None
    identities = []
    control = None
    control_identity = None
    safe_remove = False
    report = {'production_backend_enabled': False, 'tools': tools, 'lifecycle': {}}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError('unsafe prototype lock file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        used = set()
        work_root = Path(tempfile.mkdtemp(prefix='gitgo-task-uid-', dir='/private/tmp'))
        report['artifacts_directory'] = str(work_root)
        work_root.chmod(0o711)  # root-owned parent, tasks cannot rename siblings
        private = work_root / 'private'
        private.mkdir(mode=0o700)
        forbidden = private / 'sentinel'
        forbidden.write_text('private-test-data')
        # A concurrent unrelated task identity must survive every cleanup.
        control_uid = choose_uid(used)
        control_workspace = prepare_workspace(work_root, 'control', control_uid)
        control_env = environment(control_workspace, tools)
        control_identity = (control_uid, control_workspace, control_env)
        control = low_spawn(['/bin/sleep', '90'], *control_identity,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        for mode in ('tools', *MODES):
            uid = choose_uid(used)
            workspace = prepare_workspace(work_root, mode, uid)
            env = environment(workspace, tools)
            identities.append((uid, workspace, env))
            if mode == 'tools':
                report['compatibility'] = {}
                tool_smoke(uid, workspace, env, tools, results=report['compatibility'])
            else:
                report['lifecycle'][mode] = lifecycle_case(mode, uid, workspace, env, tools, forbidden)
            if control.poll() is not None:
                raise RuntimeError('unrelated identity control exited during ' + mode)
            print('completed ' + mode, file=sys.stderr, flush=True)
        report['unrelated_identity_survived'] = True
        report['passed'] = (all(r['passed'] for r in report['compatibility'].values()) and
                            all(r['passed'] for r in report['lifecycle'].values()))
    except Exception as exc:
        # Preserve partial compatibility evidence and final cleanup even when
        # the suite aborts, instead of replacing everything with one error.
        report['passed'] = False
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        try:
            cleanups = []
            if control:
                # Preserve the foreign-identity control throughout the test,
                # but reap our own direct child even if the UID helper fails.
                try:
                    if control.poll() is None:
                        control.kill()
                    control.wait(timeout=2)
                except Exception as exc:
                    cleanups.append({'observed_empty': False, 'error': 'control exit: ' + str(exc)})
            targets = [(identity, lambda: None) for identity in identities]
            if control_identity:
                targets.append((control_identity, control.poll if control else lambda: None))
            for identity, reap in targets:
                try:
                    cleanups.append({'uid': identity[0], **cleanup_uid(*identity, reap=reap)})
                except Exception as exc:
                    cleanups.append({'uid': identity[0], 'observed_empty': False, 'error': str(exc)})
            safe_remove = all(c['observed_empty'] for c in cleanups)
            report['final_cleanup'] = cleanups
            report['passed'] = report.get('passed', False) and safe_remove
        finally:
            # Do not recursively delete task-owned content as root: Python's
            # rmtree platform protections differ. Keep the isolated artifacts
            # for inspection, with a root-only outer directory after stopping.
            if work_root:
                work_root.chmod(0o700)
                print('artifacts=' + str(work_root) + '; all_identities_empty=' + str(safe_remove),
                      file=sys.stderr, flush=True)
            os.close(fd)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight', action='store_true')
    mode.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if sys.platform != 'darwin':
            raise RuntimeError('this experiment requires macOS')
        tools = resolve_tools()
        if args.preflight:
            census()
            result = {'preflight': 'passed', 'tools': tools, 'root_required_for_run': True,
                      'identity_experiment_run': False, 'production_backend_enabled': False}
        else:
            result = run_suite(tools)
        print(json.dumps(result, indent=2))
        return 0 if result.get('passed', True) else 1
    except Exception as exc:
        print(json.dumps({'passed': False, 'error': str(exc), 'production_backend_enabled': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
