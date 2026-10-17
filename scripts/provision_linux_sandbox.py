"""Explicit operator provisioning of a delegated Linux cgroup v2 subtree."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--uid', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or args.uid <= 0:
        parser.error('Linux and a non-root runtime uid are required')
    parent = Path('/sys/fs/cgroup').resolve(strict=True)
    root = args.root.absolute()
    # Provision only one named child; never traverse or chown another subtree.
    if root.parent != parent or root.name in {'', '.', '..'} or root.is_symlink():
        parser.error('Use a dedicated direct child of /sys/fs/cgroup')
    required = {'cpu', 'memory', 'pids'}
    available = set((parent / 'cgroup.controllers').read_text().split())
    if not required <= available:
        parser.error('Kernel does not expose cpu, memory and pids controllers')
    print(f'Delegate {root} to uid {args.uid}; enable cpu, memory and pids')
    if not args.apply:
        print('No changes made. Repeat with --apply from an operator terminal.')
        return
    if os.geteuid() != 0:
        parser.error('Provisioning requires root; the normal runtime must remain unprivileged')
    enabled = set((parent / 'cgroup.subtree_control').read_text().split())
    missing = required - enabled
    if missing:
        (parent / 'cgroup.subtree_control').write_text(' '.join('+' + c for c in sorted(missing)))
    root.mkdir(mode=0o700, exist_ok=True)
    if (root / 'cgroup.procs').read_text().strip():
        parser.error('The delegated root must contain no processes')
    (root / 'cgroup.subtree_control').write_text('+cpu +memory +pids')
    for entry in (root, root / 'cgroup.procs', root / 'cgroup.threads', root / 'cgroup.subtree_control'):
        os.chown(entry, args.uid, -1)
    print(f'Export GITGO_SANDBOX_CGROUP_ROOT={root} in the trusted Host environment')

if __name__ == '__main__':
    main()
