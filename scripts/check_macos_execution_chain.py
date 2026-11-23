"""Bounded acceptance for the gated macOS execution chain, not installation.

--isolation-only uses the login UID and proves no dedicated-UID lifecycle.
--run needs sudo, retains private recovery artifacts, and uses temporary jobs.
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

def stage_runtime(entry=None, *, support=()):
    # The dedicated UID cannot traverse a private user home. Copy code and the
    # existing venv to a root-owned readable trial runtime; never chmod home.
    import shutil
    root = Path(tempfile.mkdtemp(prefix='gitgo-macos-runtime-', dir='/private/tmp'))
    source = Path(__file__).resolve().parents[1]
    entry = Path(entry or __file__).resolve()
    shutil.copytree(source / 'backend', root / 'backend', ignore=shutil.ignore_patterns('__pycache__'))
    (root / 'scripts').mkdir()
    shutil.copy2(__file__, root / 'scripts' / Path(__file__).name)
    if entry != Path(__file__).resolve():
        shutil.copy2(entry, root / 'scripts' / entry.name)
    for name in support:
        if Path(name).name!=name: raise ValueError('Expected a sibling support script')
        shutil.copy2(source/'scripts'/name,root/'scripts'/name)
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Use the repository build venv for execution-chain acceptance')
    shutil.copytree(sys.prefix, root / 'venv', symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
    for directory, dirs, files in os.walk(root):
        Path(directory).chmod(0o755)
        for name in files:
            p = Path(directory) / name
            if p.is_symlink():
                target = p.resolve(strict=True)
                if str(target).startswith('/Users/'):
                    raise RuntimeError('Runtime symlink still depends on a private home: ' + str(p))
            else:
                p.chmod(0o755 if p.stat().st_mode & 0o111 else 0o644)
    print('Trial runtime: ' + str(root), file=sys.stderr, flush=True)
    os.execv(str(root / 'venv/bin/python'), [str(root / 'venv/bin/python'), '-B', '-I',
        str(root / 'scripts' / entry.name), *sys.argv[1:], '--staged'])


if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid() == 0:
    stage_runtime()

if not getattr(sys,'frozen',False): sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core.sandbox import SandboxPolicy, sandbox_popen, trusted_runtime_roots
from backend.core.sandbox_macos import execution_trial, profile, load_state
from backend.core.sandbox_io import BoundedCommunication
from backend.core import macos_identity as identity

ISOLATION = r'''
import errno,json,os,socket,subprocess,sys
from pathlib import Path
outside,port=sys.argv[1],int(sys.argv[2])
result={}
def denied(name,action):
    try: action()
    except OSError as e:
        result[name]=e.errno in (errno.EPERM,errno.EACCES)
    else: result[name]=False
Path('allowed.txt').write_text('inside')
result['workspace_write']=Path('allowed.txt').read_text()=='inside'
result['child_exec']=subprocess.check_output(['/bin/sh','-c','printf child-ok']).decode()=='child-ok'
denied('outside_read',lambda:Path(outside).read_bytes())
denied('outside_write',lambda:Path(outside).write_text('escaped'))
Path('outside-link').symlink_to(outside)
denied('symlink_read',lambda:Path('outside-link').read_bytes())
denied('symlink_write',lambda:Path('outside-link').write_text('escaped'))
denied('hardlink',lambda:os.link(outside,'outside-hardlink'))
s=socket.socket();s.settimeout(1)
denied('network_connect',lambda:s.connect(('127.0.0.1',port)))
s.close()
denied('setuid_root',lambda:os.setuid(0))
result['uid']=os.getuid()
print(json.dumps(result),flush=True)
'''
BACKGROUND = "\nimport json, os, signal, sys, time\nlifetime = 15.0\nassert 0 < lifetime <= 25\ndeadline = time.monotonic() + lifetime\nfd = os.open('births.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)\ndef event(kind):\n    os.write(fd, (json.dumps({'kind':kind, 'pid':os.getpid(),\n        'uid':os.getuid(), 'time':time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)/1000000000}) + '\\n').encode())\ndef produce():\n    os.setsid()\n    if os.fork(): os._exit(0)\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n    event('producer')\n    children = set()\n    while time.monotonic() < deadline:\n        for pid in list(children):\n            if os.waitpid(pid, os.WNOHANG)[0]: children.remove(pid)\n        if len(children) < 8:\n            pid = os.fork()\n            if pid == 0:\n                os.setsid()\n                event('birth')\n                time.sleep(max(0, min(.55, deadline - time.monotonic())))\n                os._exit(0)\n            children.add(pid)\n        time.sleep(.035)\n    for pid in children: os.waitpid(pid, 0)\n    os._exit(0)\nstarters = []\nfor _ in range(2):\n    pid = os.fork()\n    if pid == 0: produce()\n    starters.append(pid)\nfor pid in starters: os.waitpid(pid, 0)\nfrom pathlib import Path\nPath('detached').write_text(str(os.getpid()))\nprint(json.dumps({'pid':os.getpid(), 'uid':os.getuid()}), flush=True)\ntime.sleep(max(0, deadline - time.monotonic()))\n"


def fixtures():
    root=Path(tempfile.mkdtemp(prefix='gitgo-macos-chain-',dir='/private/tmp'))
    root.chmod(0o700)
    work=root/'work';work.mkdir(mode=0o700)
    # World-readable/writable positive control: failures must come from the
    # sandbox policy, not the parent directory's ordinary DAC permissions.
    canary=root/'outside.txt';canary.write_text('host-canary');canary.chmod(0o666)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(2)
    return root,work,canary,listener


def isolation_only():
    root,work,canary,listener=fixtures()
    try:
        # Positive controls outside sandbox verify file and listener are usable.
        assert canary.read_text()=='host-canary'
        control=socket.create_connection(listener.getsockname(),timeout=1);control.close()
        r=subprocess.run(['/usr/bin/sandbox-exec','-p',profile(work,trusted_runtime_roots()),
            '/usr/bin/python3','-I','-c',ISOLATION,str(canary),str(listener.getsockname()[1])],
            cwd=work,env={'PATH':'/usr/bin:/bin','TMPDIR':str(work),'HOME':str(work)},
            capture_output=True,text=True,timeout=10)
        if r.returncode: raise RuntimeError(r.stderr)
        facts=json.loads(r.stdout)
        return dict(passed=all(v for k,v in facts.items() if k!='uid') and canary.read_text()=='host-canary',
                    facts=facts,dedicated_uid_tested=False,artifacts=str(root))
    finally: listener.close()


def case(name):
    root,work,canary,listener=fixtures()
    proc=None; trial=None
    result={'case':name,'passed':False,'artifacts':str(root)}
    try:
        with execution_trial(work) as trial:
            result['lease']=str(trial['root'])
            if name=='tool_runner':
                # Existing Host journal lives outside the low-UID workspace.
                os.environ['GITGO_STATE_HOME']=str(root/'host-state')
                (work/'.gitgo').mkdir(mode=0o700)
                os.chown(work/'.gitgo',trial['uid'],trial['uid'])
                from backend.core.loop.process_tool_runner import ProcessToolRunner
                response=ProcessToolRunner(timeout=12).run('exec_command',dict(_workspace=str(work),
                    argv=['/usr/bin/printf','native-chain-ok']))
                result['runner']=dataclasses.asdict(response)
                result['passed']=(response.success and bool(response.data) and response.data.get('success') is True
                    and response.data.get('stdout','').strip()=='native-chain-ok'
                    and response.cleanup and response.cleanup.get('complete') and response.evidence_state=='committed')
            else:
                command=['/usr/bin/python3','-I','-c',ISOLATION if name=='isolation' else BACKGROUND]
                if name=='isolation': command += [str(canary),str(listener.getsockname()[1])]
                proc=sandbox_popen(command,SandboxPolicy(work,cpu_seconds=2 if name=='timeout' else 12),
                    invocation_id=uuid.uuid4().hex,cwd=str(work),env={},stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                communication=BoundedCommunication(proc)
                if name!='isolation':
                    end=time.monotonic()+6
                    while not (work/'detached').exists():
                        if proc.poll() is not None or time.monotonic()>end:
                            raise RuntimeError('Detached child not ready')
                        time.sleep(.03)
                    def births():
                        data=identity.bounded_read(work/'births.jsonl',trial['uid'])
                        return sum(json.loads(line)['kind']=='birth' for line in data.split(b'\n')[:-1])
                    end=time.monotonic()+3
                    while births()<6 and time.monotonic()<end: time.sleep(.03)
                    before=births();time.sleep(.15);after=births()
                    if before<6 or after<=before: raise RuntimeError('Producer not active at fault injection')
                    result['births_before_fault']=[before,after]
                    if name=='cancel':
                        from backend.core.process_control import terminate_tree
                        terminate_tree(proc)
                    elif name=='owner_disconnect':
                        proc.control.close();proc.control=None
                    elif name=='guardian_crash':
                        subprocess.run(['/bin/launchctl','kill','SIGKILL','system/'+proc.label],
                                       capture_output=True,timeout=5,check=True)
                out,err=communication.communicate(input='',timeout=18)
                proc.close_sandbox()
                result['stderr']=err[-2000:]
                if name=='isolation':
                    facts=json.loads(out)
                    result['isolation']=facts
                    result['passed']=all(v for k,v in facts.items() if k!='uid') and facts['uid']==trial['uid'] and canary.read_text()=='host-canary'
                else:
                    result['passed']=True
            state=load_state(trial['root'])
            result['state']=state
            # Redact duplicate command/profile/environment from report only.
            result['state']={k:v for k,v in state.items() if k not in ('argv','env','profile')}
            result['remaining']=identity.owned_rows(trial['uid'])
            cleanup=state.get('cleanup',{})
            result['passed']=bool(result['passed'] and state['phase']=='clean' and state['launch_count']==1
                and cleanup.get('observed_empty') and not cleanup.get('issues') and not result['remaining'])
            if name=='guardian_crash': result['passed'] &= state['generation']==2
            if name not in ('isolation','tool_runner'):
                result['task_start_to_clean_seconds']=state['finished']-state['started']
                result['passed'] &= 0<=result['task_start_to_clean_seconds']<14
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)
        result['passed']=False
    finally:
        listener.close()
        if proc:
            try: proc.close_sandbox()
            except Exception as exc:
                result['cleanup_error']=str(exc);result['passed']=False
            for stream in (proc.stdin,proc.stdout,proc.stderr):
                if stream: stream.close()
        if trial:
            try:
                state=load_state(trial['root'])
                if not result['passed']:
                    result['service_diagnostic']={k:v for k,v in state.items() if k not in ('argv','env','profile')}
                    log=trial['root']/'service.err'
                    if log.exists(): result['service_stderr']=identity.bounded_read(log,0).decode(errors='replace')[-3000:]
                result['phase_after_finally']=state['phase']
                if state['phase']!='clean':
                    result['recovery_command']=['sudo','/usr/bin/python3','-I',
                        str(trial['root']/'macos_execution_service.py'),str(trial['root']),'--recover-only']
            except Exception as exc: result['inspection_error']=str(exc)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--isolation-only',action='store_true');g.add_argument('--run',action='store_true')
    p.add_argument('--staged', action='store_true', help=argparse.SUPPRESS)
    args=p.parse_args()
    report={'passed':False,'production_backend_enabled':False}
    try:
        if args.isolation_only:
            if os.geteuid()==0: raise RuntimeError('Isolation-only test must run as login user')
            report.update(isolation_only())
        else:
            if os.getuid()!=0 or os.geteuid()!=0: raise RuntimeError('--run requires sudo')
            report['cases']=[]
            for name in ('isolation','cancel','timeout','owner_disconnect','guardian_crash','tool_runner'):
                result=case(name);report['cases'].append(result)
                print(name+': '+str(result['passed']),file=sys.stderr,flush=True)
                if not result['passed']: break
            report['passed']=len(report['cases'])==6 and all(c['passed'] for c in report['cases'])
    except Exception as exc: report['error']=str(exc)
    print(json.dumps(report,indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
