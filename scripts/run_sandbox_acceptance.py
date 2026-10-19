"""Build and exercise an actual onedir Host; source mocks are not package evidence."""
from __future__ import annotations
import os
import argparse
import sqlite3
import shutil
from pathlib import Path
import subprocess
import sys

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage-native-library',type=Path)
    args=parser.parse_args()
    native=None if args.storage_native_library is None else args.storage_native_library.resolve(strict=True)
    if native is not None and (sys.platform!='win32' or not native.is_file()):
        parser.error('Native storage acceptance requires an explicit Windows build')
    root = Path(__file__).resolve().parents[1]
    # Upstream integration changes broader runtime/search/storage behavior.
    # Validate the complete source suite in the same delegated native context.
    subprocess.run([sys.executable, '-B', '-m', 'pytest', 'tests', '-q'],
                   cwd=root, check=True)
    extra = ['--hidden-import', 'resource'] if sys.platform == 'linux' else []
    subprocess.run([sys.executable, '-m', 'PyInstaller', *extra, '--noconfirm', '--clean', '--onedir',
        '--name', 'gitgo-host', '--paths', str(root), '--collect-submodules', 'backend',
        '--hidden-import', 'backend.core.tools.registrations',
        '--add-data', str(root / 'backend/resources/prompts') + os.pathsep + 'backend/resources/prompts',
        '--distpath', str(root / 'dev-artifacts/sandbox-acceptance/dist'),
        '--workpath', str(root / 'dev-artifacts/sandbox-acceptance/build'),
        '--specpath', str(root / 'dev-artifacts/sandbox-acceptance/build'),
        str(root / 'backend/core/native_host_entry.py')], cwd=root, check=True)
    suffix = '.exe' if os.name == 'nt' else ''
    host = root / 'dev-artifacts/sandbox-acceptance/dist/gitgo-host' / ('gitgo-host' + suffix)
    if not host.is_file():
        raise SystemExit('Build did not produce the declared Host artifact')
    # Exercise the bundled search-engine lookup inside the real frozen Host.
    # This disposable security-test artifact is not a release/installer package.
    engine = os.environ.get('GITGO_RIPGREP_PATH') or shutil.which('rg')
    if not engine:
        raise SystemExit('Packaged search acceptance requires the pinned CI engine')
    shutil.copy2(engine, host.parent / ('rg.exe' if os.name == 'nt' else 'rg'))
    # The real artifact must contain the patched database dependency too;
    # a passing source interpreter does not verify the frozen runtime.
    probe = (
        "import sqlite3;from backend.core.storage.runtime import validate_sqlite_runtime;"
        f"assert sqlite3.sqlite_version == {sqlite3.sqlite_version!r};"
        "validate_sqlite_runtime();print('Packaged SQLite',sqlite3.sqlite_version)"
    )
    subprocess.run([str(host), '--gitgo-internal-role', 'python', '-I', '-c', probe],
                   cwd=host.parent, check=True)
    environment = dict(os.environ, GITGO_SANDBOX_TEST_HOST=str(host))
    environment.pop('GITGO_RIPGREP_PATH', None)
    packaged_tests = ['tests/test_native_sandbox.py']
    if sys.platform == 'win32':
        packaged_tests.append('tests/test_windows_sandbox_minimum.py')
    subprocess.run([sys.executable, '-B', '-m', 'pytest', *packaged_tests,
                    '-k', 'packaged', '-q'], cwd=root, env=environment, check=True)
    if native is not None:
        storage_environment=dict(environment,GITGO_STORAGE_NATIVE_TEST_LIBRARY=str(native))
        # This uses owned temporary-account fixtures. The separate native step
        # verifies the unmodified public canonical locator in disposable CI.
        storage_environment.pop('GITGO_STORAGE_CANONICAL_TEST',None)
        subprocess.run([sys.executable,'-B','-m','pytest','tests/test_storage_protected_frozen.py','-q'],
                       cwd=root,env=storage_environment,check=True)

if __name__ == '__main__':
    main()
