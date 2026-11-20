"""Audit native macOS primitives. A passing audit does NOT enable a backend.

Uses disposable directories, an unprivileged interpreter and a loopback listener.
The full audit also runs a temporary Hypervisor/Virtualization allocation probe.
No guest is booted, and no installed application or system configuration is changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import time


def seatbelt_profile(workspace: Path, *, allow_fork: bool = True) -> str:
    roots = ["/System", "/bin", "/usr/lib", "/usr/share", "/private/var/db/dyld",
             str(Path(sys.base_prefix).resolve()), str(Path(sys.prefix).resolve()),
             str(Path(sys.executable).resolve().parent), str(workspace)]
    quoted = lambda value: json.dumps(value, ensure_ascii=False)
    read_rules = " ".join(f"(subpath {quoted(root)})" for root in dict.fromkeys(roots))
    fork_rule = "(allow process-fork) " if allow_fork else ""
    return ('(version 1) (deny default (with message "GitgoMacAudit")) '
            f"(allow process-exec) {fork_rule}(allow sysctl-read) "
            "(allow process-info* (target self)) (allow signal (target self)) "
            '(allow file-read-metadata) (allow file-read* (literal "/")) '
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
    child = ("import os,time;open('detached-pid.tmp','w').write(str(os.getpid()));os.replace('detached-pid.tmp','detached-ready');"
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



def process_audit(report: dict, workspace: Path, permissive: str) -> None:
    restricted = seatbelt_profile(workspace, allow_fork=False)
    report['detached_child_survives_group_kill'] = detached_probe(permissive, workspace)
    assert run(restricted, workspace, "print('executed')") == 'executed'
    report['single_process_execution_control'] = True
    operations = {
        'fork': "pid = os.fork()",
        'posix_spawn': "pid = os.posix_spawn('/bin/sh', ['sh', '-c', 'exit 0'], {})",
        'posix_spawn_setsid': "pid = os.posix_spawn('/bin/sh', ['sh', '-c', 'exit 0'], {}, setsid=True)",
    }
    for name, operation in operations.items():
        source = (
            "import errno, os\n"
            f"try:\n {operation}\n"
            "except OSError as exc:\n"
            " if exc.errno != errno.EPERM:\n  raise\n"
            " print('denied')\n"
            "else:\n"
            " if pid == 0:\n  os._exit(0)\n"
            " assert os.waitpid(pid, 0)[1] == 0\n"
            " print('created')"
        )
        assert run(permissive, workspace, source) == 'created', name
        assert run(restricted, workspace, source) == 'denied', name
        # Replacing the process image must not lift the restriction.
        replacement = (
            "import os, sys\n"
            f"os.execv(sys.executable, [sys.executable, '-I', '-B', '-c', {source!r}])"
        )
        assert run(permissive, workspace, replacement) == 'created', name
        assert run(restricted, workspace, replacement) == 'denied', name
        report[f'{name}_denied_in_single_process_profile'] = True
        report[f'{name}_denied_after_exec_in_single_process_profile'] = True


def virtualization_probe() -> dict:
    """Exercise a signed native VM allocation, not just the hv_support sysctl.

    No guest image, network adapter, directory share or persistent VM is used.
    An unsupported machine is an observation; compile/sign/cleanup failures
    are audit errors and must not be misreported as unsupported hardware.
    """
    source = Path(__file__).with_name('probe_macos_virtualization.m')
    with tempfile.TemporaryDirectory(prefix='gitgo-macos-hypervisor-') as raw:
        root = Path(raw)
        executable = root / 'virtualization-probe'
        entitlements = root / 'entitlements.plist'
        entitlements.write_bytes(plistlib.dumps({
            'com.apple.security.hypervisor': True,
            'com.apple.security.virtualization': True,
        }))
        commands = [
            ['/usr/bin/xcrun', 'clang', '-fobjc-arc', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(executable),
             '-framework', 'Foundation',
             '-framework', 'Hypervisor', '-framework', 'Virtualization'],
            ['/usr/bin/codesign', '--force', '--sign', '-', '--identifier',
             'org.gitgo.sandbox.virtualization-probe', '--entitlements',
             str(entitlements), str(executable)],
            [str(executable)],
        ]
        result = None
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise RuntimeError(f'Virtualization probe {Path(command[0]).name} failed '
                                   f'({result.returncode}): {result.stderr[-1600:]}')
        report = json.loads(result.stdout)
        assert report['schema_version'] == 1
        assert report['native_macos_guest_ready'] is False
        assert report['guest_os_boot_tested'] is False
        assert report['production_backend_enabled'] is False
        if report['hypervisor_vm_created']:
            assert report['hypervisor_destroy_status'] == 0
        return report


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
        process_audit(report, workspace, profile)
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
    parser.add_argument('--native-only', action='store_true',
                        help='Audit native sandbox primitives without allocating a VM')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Run the native audit on macOS; other OS results are not evidence')
    if not Path('/usr/bin/sandbox-exec').is_file():
        raise SystemExit('Seatbelt launcher is unavailable; audit cannot pass')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    hv = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.hv_support'], capture_output=True, text=True, timeout=3)
    hv_value = hv.stdout.strip() if hv.returncode == 0 else ''
    report = {'os': platform.mac_ver()[0], 'architecture': platform.machine(),
              'interpreter': str(Path(sys.executable).resolve()),
              'base_prefix': str(Path(sys.base_prefix).resolve()),
              'hypervisor_available': {'1': True, '0': False}.get(hv_value),
              'production_backend': 'unavailable', 'macos_support_complete': False, 'audit_passed': False}
    try:
        if not args.native_only:
            report['virtualization'] = virtualization_probe()
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
