"""Build and exercise an actual onedir Host; source mocks are not package evidence."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys

def main():
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, '-B', '-m', 'pytest', 'tests/test_native_sandbox.py', '-q'],
                   cwd=root, check=True)
    extra = ['--hidden-import', 'resource'] if sys.platform == 'linux' else []
    subprocess.run([sys.executable, '-m', 'PyInstaller', *extra, '--noconfirm', '--clean', '--onedir',
        '--name', 'gitgo-host', '--paths', str(root), '--collect-submodules', 'backend',
        '--hidden-import', 'backend.core.tools.registrations',
        '--distpath', str(root / '.gitgo/sandbox-dist'),
        '--workpath', str(root / '.gitgo/sandbox-build'),
        '--specpath', str(root / '.gitgo/sandbox-build'),
        str(root / 'backend/core/native_host_entry.py')], cwd=root, check=True)
    suffix = '.exe' if os.name == 'nt' else ''
    host = root / '.gitgo/sandbox-dist/gitgo-host' / ('gitgo-host' + suffix)
    if not host.is_file():
        raise SystemExit('Build did not produce the declared Host artifact')
    environment = dict(os.environ, GITGO_SANDBOX_TEST_HOST=str(host))
    subprocess.run([sys.executable, '-B', '-m', 'pytest', 'tests/test_native_sandbox.py',
                    '-k', 'packaged', '-q'], cwd=root, env=environment, check=True)

if __name__ == '__main__':
    main()
