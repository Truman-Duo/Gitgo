"""Enter a systemd-delegated scope before running native security acceptance.

Operator example: systemd-run --user --scope -p Delegate=yes python SCRIPT -- COMMAND
The wrapper never escalates privileges or changes any ancestor cgroup.
"""
from __future__ import annotations
import os
from pathlib import Path
import sys

def main():
    if sys.platform != 'linux' or '--' not in sys.argv:
        raise SystemExit('Linux delegated scope and -- COMMAND are required')
    command = sys.argv[sys.argv.index('--') + 1:]
    if not command:
        raise SystemExit('A command is required')
    entry = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
                 if line.startswith('0::'))
    root = (Path('/sys/fs/cgroup') / entry.lstrip('/')).resolve(strict=True)
    if root == Path('/sys/fs/cgroup') or not root.name.endswith(('.scope', '.service')):
        raise SystemExit('Run in an explicitly delegated systemd scope or service')
    host = root / 'host'
    host.mkdir(mode=0o700, exist_ok=True)
    (host / 'cgroup.procs').write_text('0')
    (root / 'cgroup.subtree_control').write_text('+cpu +memory +pids')
    os.environ['GITGO_SANDBOX_CGROUP_ROOT'] = str(root)
    os.execvp(command[0], command)

if __name__ == '__main__':
    main()
