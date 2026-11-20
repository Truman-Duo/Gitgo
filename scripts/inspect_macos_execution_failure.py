"""Read-only administrator diagnostics for a single failed execution lease.

Does not run workloads, send signals, alter leases, or unload recovery jobs.
Only selected state fields are emitted; task argv/env/profile are excluded.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


STATE_FIELDS = (
    'schema', 'uid', 'invocation_id', 'phase', 'generation', 'launch_count',
    'boot_id', 'clock', 'pid', 'started', 'finished', 'reason', 'exit_code',
    'error', 'cleanup', 'resources', 'resource_error', 'resource_observation',
)


def read_private(path, *, tail=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise RuntimeError('Expected root-owned regular evidence file')
        if tail:
            head = os.read(fd, 6000)
            os.lseek(fd, max(6000, info.st_size - 6000), os.SEEK_SET)
            return dict(size=info.st_size, head=head.decode(errors='replace'),
                        tail=os.read(fd, 6000).decode(errors='replace'))
        if info.st_size > 256 * 1024:
            raise RuntimeError('State exceeds evidence bound')
        return json.loads(os.read(fd, 256 * 1024 + 1))
    finally:
        os.close(fd)


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
    return dict(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lease', required=True, help='32-character temporary lease ID')
    args = parser.parse_args()
    if sys.platform != 'darwin' or os.geteuid() != 0:
        parser.error('Run on macOS with sudo to read private recovery evidence')
    if not re.fullmatch('[0-9a-f]{32}', args.lease):
        parser.error('Invalid lease ID')
    root = Path('/private/tmp/gitgo-macos-execution-v1') / args.lease
    for directory in (root.parent, root):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise RuntimeError('Untrusted execution directory')
    report = dict(read_only=True, lease=str(root))
    try:
        state = read_private(root / 'state.json')
        report['state'] = {k: state[k] for k in STATE_FIELDS if k in state}
    except Exception as exc:
        state = {}
        report['state_error'] = type(exc).__name__ + ': ' + str(exc)
    for name in ('service.err', 'service.out'):
        try:
            report[name] = read_private(root / name, tail=True)
        except Exception as exc:
            report[name] = dict(error=type(exc).__name__ + ': ' + str(exc))
    report['launchctl'] = command(['/bin/launchctl', 'print',
        'system/org.gitgo.execution-trial.' + args.lease])
    report['current_boot'] = command(['/usr/sbin/sysctl', '-n', 'kern.boottime'])
    report['current_boot_session'] = command(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'])
    census = command(['/bin/ps', '-axo', 'uid=,ruid=,svuid=,pid=,ppid=,pgid=,stat='])
    uid = state.get('uid')
    if census['returncode'] == 0 and type(uid) is int and 200000000 <= uid < 210000000:
        report['owned_process_rows'] = [line for line in census['stdout'].splitlines()
            if str(uid) in line.split()[:3]]
    else:
        report['census_error'] = dict(returncode=census['returncode'], stderr=census['stderr'],
                                      valid_uid=type(uid) is int and 200000000 <= uid < 210000000)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
