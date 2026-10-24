"""Disposable Linux frozen-backend Git/worktree round-trip; no live project."""
import json
import os
from pathlib import Path
import sys
import tempfile

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.adapters.local_git_runner import LocalGitRunner
from backend.core.loop.worktree import AgentWorktreeManager
from backend.core.storage import StorageRuntime


def check():
    if sys.platform != 'linux':
        raise RuntimeError('Linux native Git boundary required')
    with tempfile.TemporaryDirectory(prefix='gitgo-native-git-') as raw:
        root = Path(raw).resolve()
        repo = root / 'project'; repo.mkdir()
        home = root / 'operator'; home.mkdir()
        (home / '.gitconfig').write_text('[user]\nname = Native Fixture\nemail = fixture@gitgo.invalid\n')
        # This is a short-lived acceptance subprocess. Its operator fixture is
        # separate from both the repository and the installer's real home.
        for key in list(os.environ):
            if key.upper().startswith(('GIT_', 'GCM_')):
                del os.environ[key]
        os.environ.update(HOME=str(home), XDG_CONFIG_HOME=str(home / 'xdg'))
        runner = LocalGitRunner(repo)
        for args in [['init']]:
            result = runner.run(args)
            if result.returncode:
                raise RuntimeError(f'Native Git initialization failed: {result.stderr}')
        (repo / 'app.txt').write_bytes(b'base\n')
        if not runner.add_all():
            raise RuntimeError('Native protected staging failed')
        ok, error = runner.commit('native fixture baseline')
        if not ok:
            raise RuntimeError(f'Native protected commit failed: {error}')
        storage = StorageRuntime(repo, state_home=root / 'state')
        lease = None
        manager = AgentWorktreeManager(repo, storage)
        try:
            lease = manager.create(process_id='native-fixture', task_id='native-fixture')
            (Path(lease.path) / 'app.txt').write_bytes(b'validated native change\n')
            manager.seal(lease)
            result = manager.promote(snapshot_commit=lease.snapshot_commit,
                ordered_commits=[lease.own_commit], promotion_id='native-fixture')
            if not result['promoted'] or (repo / 'app.txt').read_bytes() != b'validated native change\n':
                raise RuntimeError('Native protected promotion did not preserve business bytes')
            manager.dispose(lease)
            lease = None
        finally:
            try:
                if lease is not None:
                    manager.dispose(lease)
            finally:
                storage.close()
        print(json.dumps({'frozen': bool(getattr(sys, 'frozen', False)),
            'passed': ['protected init', 'staging', 'commit', 'worktree create',
                       'sealing', 'promotion', 'worktree retirement']}), flush=True)


if __name__ == '__main__':
    check()
