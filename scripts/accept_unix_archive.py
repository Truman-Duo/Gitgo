"""Independent final-archive acceptance in a fresh disposable native runner.

No build library/search path is passed to the installed executable. Desktop
Keychain dialogs and physical IME acceptance are deliberately not certified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.unix_release_manifest import verify

def accept(archive, commit):
    archive = archive.resolve(strict=True)
    expected = archive.with_suffix(archive.suffix + '.sha256').read_text().split()[0]
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError('Archive digest mismatch')
    with tempfile.TemporaryDirectory(prefix='gitgo-archive-') as raw:
        root = Path(raw).resolve()
        payload = root / 'payload'
        payload.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(payload, filter='data')
        directories = list(payload.iterdir())
        if len(directories) != 1 or not directories[0].is_dir():
            raise ValueError('Unexpected archive roots')
        release = directories[0]
        manifest = verify(release, commit)
        expected_platform = 'macos' if sys.platform == 'darwin' else 'linux'
        if manifest['platform'] != expected_platform:
            raise ValueError('Not a native archive for this runner')
        home = root / 'home'
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(('GITGO_', 'PYTHON', 'LD_', 'DYLD_'))}
        env.update(HOME=str(home), PATH='/usr/bin:/bin:/usr/sbin:/sbin', SHELL='/bin/bash',
                   XDG_DATA_HOME=str(home / '.local/share'), XDG_STATE_HOME=str(home / '.local/state'),
                   GITGO_CONFIG_PATH=str(home / 'config.json'), GITGO_STATE_HOME=str(home / 'state'),
                   GITGO_LLM_CONFIG_PATH=str(home / 'llm.json'), GITGO_LLM_SECRET_PATH=str(home / 'secrets.json'))
        installed = root / 'app'
        commands = root / 'bin'
        arguments = ['--install-root', str(installed), '--bin-dir', str(commands)]
        def run(*args, environment=env, **kwargs):
            subprocess.run(args, env=environment, cwd=root, check=True, timeout=180, **kwargs)
        run(str(release / 'install.sh'), *arguments)
        # Upgrade the same owned payload twice; unknown/older unmarked directories
        # intentionally cannot be adopted. Failure injection is tested separately.
        run(str(release / 'install.sh'), *arguments)
        verify(installed, commit)
        host = installed / 'internal/gitgo-host/gitgo-host'
        # The local test harness runs from source; its child uses only the installed
        # frozen Host and this deliberately build-free environment.
        run(sys.executable, '-B', str(ROOT / 'scripts/smoke_packaged_runtime.py'), '--host', str(host))
        run(sys.executable, '-B', str(ROOT / 'scripts/check_terminal_continuity.py'), '--host', str(host))
        from backend.core.child_process import INTERNAL_ROLE_FLAG, PYTHON_ROLE
        if sys.platform == 'linux':
            # The installed frozen backend must exercise its own protected Git
            # implementation, not just pass a source-level namespace probe.
            run(str(host), INTERNAL_ROLE_FLAG, PYTHON_ROLE,
                str(ROOT / 'scripts/check_native_git.py'))
        # Create/read/delete with the installed app's own native credential
        # implementation and executable identity, including Keychain ACLs.
        credential_env = dict(env)
        if sys.platform == 'darwin':
            # Keychain preferences resolve through HOME. The workflow configured
            # its disposable Keychain there; Gitgo files remain explicitly isolated.
            credential_env['HOME'] = os.environ['HOME']
        run(str(host), INTERNAL_ROLE_FLAG, PYTHON_ROLE,
            str(ROOT / 'scripts/check_native_credentials.py'), '--host', str(host),
            environment=credential_env)
        scope_env = dict(env, GITGO_SCOPE_TEST_HOST=str(host))
        subprocess.run([sys.executable, '-B', '-m', 'pytest', str(ROOT / 'tests/test_unix_process_scope.py'),
                        '-q', '-k', 'frozen'], cwd=ROOT, env=scope_env, check=True, timeout=180)
        sentinel = home / 'state' / 'keep.txt'
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text('preserve user state')
        run(str(installed / 'uninstall.sh'), '--bin-dir', str(commands), '--keep-config')
        if installed.exists() or sentinel.read_text() != 'preserve user state':
            raise ValueError('Uninstall did not preserve data or remove owned payload')
        return {'source': manifest['source'], 'platform': manifest['platform'],
                'architecture': manifest['architecture'], 'archive_sha256': expected,
                'passed': ['digest', 'payload inventory', 'clean install', 'owned upgrade',
                           'frozen Host/daemon/search/storage', 'conversation continuity',
                           'frozen owned scope', 'real CI keyring and frozen cleanup', 'keep-config uninstall'],
                'linux_frozen_protected_git': sys.platform == 'linux',
                'pending': ['desktop keyring interaction', 'physical IME and visible terminal',
                            'independent maintainer trigger/download'],
                'macos_detached_process_containment': 'not guaranteed'}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    args.report.write_text(json.dumps(accept(args.archive, args.commit), indent=2) + '\n')
