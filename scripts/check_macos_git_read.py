"""Administrator acceptance for the bounded Git object-read prototype.

Uses disposable repositories and the existing UID/launchd/Seatbelt owner.
No real project, production capability, permanent helper install or reboot.
Retains lease evidence; never removes recovery debt after a failed run.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime
if __name__ == '__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid() == 0:
    stage_runtime(__file__, support=('retire_macos_unstarted_trial.py',))
if not getattr(sys,'frozen',False):sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core import macos_git_read as git
from backend.core import macos_identity as identity
from backend.core.sandbox_read import ReadRoot
from backend.core.sandbox import SandboxPolicy, sandbox_popen
from backend.core.sandbox_io import BoundedCommunication
from backend.core.sandbox_macos import execution_trial, load_state

DRIVER = r'''
import errno,json,os,subprocess,sys,time
from pathlib import Path
cases,canary,mode=json.loads(sys.argv[1]),sys.argv[2],sys.argv[3]
for command,expected in cases:
    result=subprocess.run(command,capture_output=True,text=True,timeout=15)
    if result.returncode or result.stdout.strip()!=expected:
        raise RuntimeError('Git query failed: '+result.stderr[-2000:])
for action in (lambda:Path(canary).read_bytes(), lambda:Path(canary).write_text('escaped')):
    try: action()
    except OSError as error:
        if error.errno not in (errno.EACCES,errno.EPERM): raise
    else: raise RuntimeError('Outside access succeeded')
if mode!='normal':
    pid=os.fork()
    if pid==0:
        os.setsid()
        if os.fork(): os._exit(0)
        Path('detached-ready').write_text(str(os.getpid()))
        time.sleep(35)
        os._exit(0)
    os.waitpid(pid,0)
    end=time.monotonic()+5
    while not Path('detached-ready').exists():
        if time.monotonic()>end: raise RuntimeError('Detached child did not start')
        time.sleep(.02)
Path('queries-ready').write_text('ok')
print(json.dumps({'queries_passed':True,'outside_denied':True,'uid':os.getuid()}),flush=True)
if mode!='normal': time.sleep(35)
'''


def fixture(base,checkout=False):
    repo = base / 'source'; repo.mkdir()
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(base), 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null'}
    def raw(*args, data=None):
        return subprocess.run(['/usr/bin/git', *args], input=data, cwd=repo, env=env,
            capture_output=True, check=True, timeout=10).stdout.strip().decode()
    raw('init', '-q'); raw('config', 'user.name', 'Fixture')
    raw('config', 'user.email', 'fixture@gitgo.invalid')
    oid = raw('hash-object', '-w', '--stdin', data=b'object-view-test\n')
    raw('pack-objects', str(repo / '.git/objects/pack/pack'), data=(oid + '\n').encode())
    (repo / '.git/objects' / oid[:2] / oid[2:]).unlink()
    tree = raw('mktree', data=(b'100644 blob '+oid.encode()+b'\tobject.txt\n') if checkout else b'')
    first = raw('commit-tree', tree, '-m', 'first')
    second = raw('commit-tree', tree, '-p', first, '-m', 'second')
    # Poison source configuration BEFORE capture. The prototype never invokes
    # Git against this repository, so even config parsing stays out of Host Git.
    marker = base / 'extension-executed'
    (repo / '.git/config').write_text('[core]\nfsmonitor = touch ' + str(marker) + '\n'
        '[include]\npath = /private/etc/passwd\n[filter "hostile"]\nprocess = touch ' + str(marker) + '\n')
    hook = repo / '.git/hooks/post-checkout'
    hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n'); hook.chmod(0o755)
    directory = repo / '.git/objects'
    with ReadRoot(directory) as root: view = git.capture_objects(root, directory)
    # Replace the selected metadata after capture; private reads must survive.
    (repo / '.git').rename(repo / 'old-git')
    (repo / '.git').mkdir()
    return view, oid, first, second, marker


def case(mode):
    base = Path(tempfile.mkdtemp(prefix='gitgo-macos-chain-', dir='/private/tmp'))
    base.chmod(0o700)
    work = base / 'work'; work.mkdir(mode=0o700)
    result = {'case': mode, 'passed': False, 'artifacts': str(base)}
    proc = trial = None
    try:
        view, oid, first, second, marker = fixture(base)
        result['view_sha256'] = view.sha256
        canary = base / 'canary'; canary.write_text('unchanged'); canary.chmod(0o666)
        with execution_trial(work, wall_seconds=8 if mode == 'timeout' else 25) as trial:
            result['lease'] = str(trial['root'])
            destination = work / 'object-view'
            view.materialize(destination)
            queries = [(git.read_command(destination, q), expected) for q, expected in (
                (['cat-file', '-t', oid], 'blob'), (['cat-file', '-s', oid], '17'),
                (['merge-base', first, second], first))]
            proc = sandbox_popen(['/usr/bin/python3', '-I', '-c', DRIVER,
                json.dumps(queries), str(canary), mode], SandboxPolicy(work, cpu_seconds=15),
                invocation_id=uuid.uuid4().hex, cwd=str(work), env={},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            communication = BoundedCommunication(proc, limit=65536)
            if mode != 'normal':
                deadline = time.monotonic() + 18
                while not (work / 'queries-ready').exists():
                    if proc.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Git queries did not reach fault-injection readiness')
                    time.sleep(.04)
                if identity.bounded_read(work / 'queries-ready', trial['uid']) != b'ok':
                    raise RuntimeError('Invalid readiness marker')
                detached = int(identity.bounded_read(work / 'detached-ready', trial['uid']))
                if not any(row['pid'] == detached for row in identity.owned_rows(trial['uid'])):
                    raise RuntimeError('Detached process is not live at fault injection')
                result['detached_pid_before_fault'] = detached
                if mode == 'cancel': proc.kill()
                elif mode == 'guardian_crash':
                    subprocess.run(['/bin/launchctl', 'kill', 'SIGKILL', 'system/' + proc.label],
                        capture_output=True, check=True, timeout=8)
            out, err = communication.communicate(input='', timeout=35)
            proc.close_sandbox()
            result['stderr'] = err[-3000:]
            result['cleanup'] = proc._gitgo_cleanup
            result['returncode'] = proc.returncode
            state = load_state(trial['root'])
            result['launch_count'] = state['launch_count']
            result['reason'] = state.get('reason')
            result['residual_processes'] = len(identity.owned_rows(trial['uid']))
            accepted = False
            try:
                published = git.accepted_output(proc, out)
                facts = json.loads(published)
                accepted = facts == {'queries_passed': True, 'outside_denied': True, 'uid': trial['uid']}
            except git.GitBoundaryError:
                pass
            result['output_accepted'] = accepted
            result['passed'] = (accepted == (mode == 'normal') and
                proc._gitgo_cleanup.get('complete') is True and state['phase'] == 'clean' and
                state['launch_count'] == 1 and not proc.loaded and not result['residual_processes'] and
                state.get('reason') == {'normal': 'task_exit', 'cancel': 'cancel',
                    'timeout': 'timeout', 'guardian_crash': 'automatic_recovery'}[mode] and
                canary.read_text() == 'unchanged' and not marker.exists())
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        if proc:
            try: proc.close_sandbox()
            except Exception as exc: result['cleanup_error'] = str(exc); result['passed'] = False
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream: stream.close()
        if trial:
            state = load_state(trial['root'])
            result['final_phase'] = state['phase']
            if state['phase'] != 'clean': result['passed'] = False
            if not result['passed']:
                result['service_state'] = {key: value for key, value in state.items()
                    if key not in {'argv', 'env', 'profile'}}
                log = trial['root'] / 'service.err'
                if log.exists():
                    result['service_stderr'] = identity.bounded_read(log, 0).decode(errors='replace')[-3000:]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--staged', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--retire-unstarted-source-lease', metavar='ID',
                        help='Explicitly retire one never-started source trial before testing')
    args = parser.parse_args()
    report = {'passed': False, 'production_backend_enabled': False,
              'frozen_package_verified': False, 'scope': 'SHA-1 object type/size and merge-base only', 'cases': []}
    try:
        if sys.platform != 'darwin' or os.getuid() != 0 or os.geteuid() != 0:
            raise RuntimeError('--run requires sudo on macOS')
        if args.retire_unstarted_source_lease:
            from retire_macos_unstarted_trial import retire
            report['prior_unstarted_retirement'] = retire(args.retire_unstarted_source_lease, source=True)
        for mode in ('normal', 'cancel', 'timeout', 'guardian_crash'):
            item = case(mode); report['cases'].append(item)
            print(mode + ': ' + str(item['passed']), file=sys.stderr, flush=True)
            if not item['passed']: break
        report['passed'] = len(report['cases']) == 4 and all(item['passed'] for item in report['cases'])
    except Exception as exc: report['error'] = type(exc).__name__ + ': ' + str(exc)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
