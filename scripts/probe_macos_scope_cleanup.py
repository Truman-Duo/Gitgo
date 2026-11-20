"""Measure macOS cleanup with bounded, disposable Node subprocesses.

This probe exercises process lifetime only. It never enables tool execution.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


WORKLOAD = """
const { spawn } = require('node:child_process');
const fs = require('node:fs');
function childWorkload() {
    const fs = require('node:fs');
    const name = process.argv[1];
    const deadline = Date.now() + 5000;
    fs.writeFileSync(name + '.ready', '');
    const timer = setInterval(() => {
        if (fs.existsSync('release')) {
            fs.writeFileSync(name + '.survived', '');
            clearInterval(timer);
        } else if (Date.now() >= deadline) {
            clearInterval(timer);
        }
    }, 10);
}
const childCode = '(' + childWorkload.toString() + ')()';
for (const detached of [false, true]) {
    const name = detached ? 'detached' : 'ordinary';
    const child = spawn(process.execPath, ['-e', childCode, name],
                        { detached, stdio: 'ignore' });
    child.on('error', error => { throw error; });
    child.unref();
}
const deadline = Date.now() + 6500;
const timer = setInterval(() => {
    if (fs.existsSync('release') || Date.now() >= deadline) {
        fs.writeFileSync('worker.finished', '');
        clearInterval(timer);
    }
}, 10);
"""

MODES = ('cancel', 'timeout', 'owner_crash', 'supervisor_crash')


def wait_for_children(workspace: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if all((workspace / f'{name}.ready').exists() for name in ('ordinary', 'detached')):
            return
        time.sleep(.01)
    raise RuntimeError('Node did not start both the ordinary and detached controls')


def run_owner(mode: str, node: str, workspace: Path) -> None:
    from backend.core.process_control import spawn_managed, terminate_tree
    from backend.core.process_output import collect_output

    proc = spawn_managed([node, '-e', WORKLOAD], cwd=workspace,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    try:
        wait_for_children(workspace)
        if mode == 'timeout':
            try:
                collect_output(proc, timeout=.2, limit=4096,
                               terminate=lambda: terminate_tree(proc))
            except subprocess.TimeoutExpired:
                pass
            else:
                raise RuntimeError('The timeout control did not expire')
        elif mode == 'owner_crash':
            os.kill(os.getpid(), signal.SIGKILL)
        elif mode == 'supervisor_crash':
            proc.kill()
    finally:
        terminate_tree(proc)
        proc.stdout.close()
        proc.stderr.close()
    print(json.dumps(proc._gitgo_cleanup), flush=True)


def observe(mode: str, node: str, workspace: Path) -> dict:
    result = subprocess.run([sys.executable, '-B', __file__, node,
                             '--owner', mode, '--workspace', str(workspace)],
                            capture_output=True, text=True, timeout=9)
    if mode == 'owner_crash':
        if result.returncode != -signal.SIGKILL:
            raise RuntimeError(f'Owner did not receive SIGKILL: {result.stderr}')
        # The surviving supervisor observes control socket EOF asynchronously.
        # This delay is for observing the probe, never a cleanup guarantee.
        time.sleep(.35)
    else:
        result.check_returncode()
    (workspace / 'release').touch()
    # Bounded children exit on their own after observation, without signalling
    # a grandchild's sampled PID.
    deadline = time.monotonic() + 1.5
    while not (workspace / 'detached.survived').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    time.sleep(.15)
    return {
        'cleanup_report': json.loads(result.stdout) if result.stdout else None,
        'ordinary_child_continued': (workspace / 'ordinary.survived').exists(),
        'detached_child_continued': (workspace / 'detached.survived').exists(),
        'worker_continued': (workspace / 'worker.finished').exists(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('node', type=Path, help='Path to a local Node executable')
    parser.add_argument('--owner', choices=MODES, help=argparse.SUPPRESS)
    parser.add_argument('--workspace', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This probe requires macOS')
    if bool(args.owner) != bool(args.workspace):
        parser.error('Internal probe arguments must be used together')
    node = str(args.node.resolve(strict=True))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if args.owner:
        run_owner(args.owner, node, args.workspace)
        return
    with tempfile.TemporaryDirectory(prefix='gitgo-macos-scope-') as raw:
        report = {}
        for mode in MODES:
            workspace = Path(raw) / mode
            workspace.mkdir()
            report[mode] = observe(mode, node, workspace)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
