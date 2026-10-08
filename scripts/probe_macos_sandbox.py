"""Audit native macOS primitives. A passing audit does NOT enable a backend.

Only disposable directories, an unprivileged interpreter and a loopback listener
are used. No installation, entitlements or system configuration is changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import time


def seatbelt_profile(workspace: Path) -> str:
    roots = ["/System", "/bin", "/usr/lib", "/usr/share", "/private/var/db/dyld",
             str(Path(sys.base_prefix).resolve()), str(Path(sys.prefix).resolve()),
             str(Path(sys.executable).resolve().parent), str(workspace)]
    quoted = lambda value: json.dumps(value, ensure_ascii=False)
    read_rules = " ".join(f"(subpath {quoted(root)})" for root in dict.fromkeys(roots))
    return ('(version 1) (deny default (with message "GitgoMacAudit")) '
            "(allow process-exec process-fork) (allow sysctl-read) "
            "(allow process-info* (target self)) (allow signal (target self)) "
            "(allow file-read-metadata) "
            f"(allow file-map-executable {read_rules}) "
            f"(allow file-read* {read_rules} (literal \"/dev/urandom\") (literal \"/dev/random\") (literal \"/dev/null\")) "
            f"(allow file-write* (subpath {quoted(str(workspace))}) (literal \"/dev/null\"))")


def launch(profile: str, workspace: Path, source: str, **options):
    from backend.core.sandbox import sandbox_environment
    return subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile,
                             sys.executable, '-I', '-B', '-c', source],
                            cwd=workspace, env=sandbox_environment(os.environ),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **options)


def run(profile: str, workspace: Path, source: str) -> str:
    proc = launch(profile, workspace, source, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=8)
        if proc.returncode:
            raise RuntimeError(f'Native probe exit {proc.returncode}: {err[-1000:]}')
        return out.strip()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


def detached_probe(profile: str, workspace: Path) -> bool:
    child = ("import os,time;open('detached-ready','w').write(str(os.getpid()));"
             "time.sleep(0.8);open('survived-group-kill','w').write('yes');time.sleep(60)")
    source = ("import subprocess,sys,time;"
              f"subprocess.Popen([sys.executable,'-I','-c',{child!r}],start_new_session=True,"
              "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
              "time.sleep(60)")
    proc = launch(profile, workspace, source, start_new_session=True)
    child_pid = None
    try:
        ready = workspace / 'detached-ready'
        deadline = time.monotonic() + 5
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            raise RuntimeError('Detached-child positive control did not start')
        # The marker is written by the detached child after setsid succeeds.
        child_pid = int(ready.read_text())
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        time.sleep(1.1)
        return (workspace / 'survived-group-kill').exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.stdout.close()
        proc.stderr.close()


def audit(report: dict):
    from backend.core.sandbox import SandboxDenied, SandboxPolicy, sandbox_popen
    with tempfile.TemporaryDirectory(prefix='gitgo-macos-audit-') as raw:
        root = Path(raw).resolve()
        workspace = root / 'workspace'
        workspace.mkdir()
        outside = root / 'host-only.txt'
        outside.write_text('host-only')
        assert outside.read_text() == 'host-only'
        (workspace / 'escape').symlink_to(outside)
        profile = seatbelt_profile(workspace)
        control = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, '/bin/echo', 'seatbelt'],
            capture_output=True, text=True, cwd=workspace, timeout=8)
        assert control.returncode == 0 and control.stdout.strip() == 'seatbelt', (control.returncode, control.stdout, control.stderr)
        report['seatbelt_launcher_control'] = True
        assert run(profile, workspace, "open('allowed','w').write('ok');print('executed')") == 'executed'
        assert (workspace / 'allowed').read_text() == 'ok'
        report['native_execution_control'] = True
        for name, target in [('outside_read_denied', str(outside)), ('symlink_read_denied', 'escape')]:
            source = (f"try:\n open({target!r}).read()\nexcept OSError:\n print('blocked')"
                      "\nelse:\n print('escaped')")
            assert run(profile, workspace, source) == 'blocked', name
            report[name] = True
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(2)
            address = listener.getsockname()
            with socket.create_connection(address, timeout=2):
                peer, _ = listener.accept()
                peer.close()
            source = ("import socket\ntry:\n "
                      f"socket.create_connection({address!r},timeout=1)"
                      "\nexcept OSError:\n print('blocked')\nelse:\n print('escaped')")
            assert run(profile, workspace, source) == 'blocked'
            report['network_denied_with_positive_control'] = True
        # Report observations, including kernel-version differences. Neither
        # rlimits nor process-group cleanup establish an aggregate tree policy.
        report['detached_child_survives_group_kill'] = detached_probe(profile, workspace)
        source = ("import resource,mmap\ntry:\n "
                  "resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))"
                  "\nexcept (ValueError,OSError):\n print('limit_setting_rejected')"
                  "\nelse:\n try:\n  m=mmap.mmap(-1,1024**3);m.close()"
                  "\n except (ValueError,OSError,MemoryError):\n  print('allocation_denied')"
                  "\n else:\n  print('allocation_allowed')")
        report['address_space_limit_observation'] = run(profile, workspace, source)
        assert report['address_space_limit_observation'] in {
            'limit_setting_rejected', 'allocation_denied', 'allocation_allowed'}
        # Production remains fail closed, even where a subset of primitives
        # works. This audit is never an alternative tool execution route.
        try:
            sandbox_popen([sys.executable, '-c', 'print(1)'], SandboxPolicy(workspace))
        except SandboxDenied as exc:
            assert exc.code == 'SANDBOX_UNAVAILABLE'
            report['production_refuses_incomplete_backend'] = True
        else:
            raise AssertionError('Update the audit with full backend acceptance before enabling macOS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Run the native audit on macOS; other OS results are not evidence')
    if not Path('/usr/bin/sandbox-exec').is_file():
        raise SystemExit('Seatbelt launcher is unavailable; audit cannot pass')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    report = {'os': platform.mac_ver()[0], 'architecture': platform.machine(),
              'interpreter': str(Path(sys.executable).resolve()),
              'base_prefix': str(Path(sys.base_prefix).resolve()),
              'production_backend': 'unavailable', 'macos_support_complete': False, 'audit_passed': False}
    try:
        audit(report)
        report['audit_passed'] = True
    except Exception as exc:
        report['audit_error'] = str(exc)[-2000:]
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
