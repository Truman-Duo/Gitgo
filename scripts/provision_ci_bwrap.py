"""Build a hash-pinned, non-setuid bubblewrap only on disposable hosted CI."""
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('bubblewrap provisioning requires a disposable hosted CI worker')
    root = Path(__file__).resolve().parents[1] / 'dev-artifacts/ci-bwrap'
    root.mkdir(parents=True, exist_ok=True)
    archive = root / 'bubblewrap-0.11.1.tar.xz'
    expected = 'c1b7455a1283b1295879a46d5f001dfd088c0bb0f238abb5e128b3583a246f71'
    if not archive.exists():
        request = urllib.request.Request('https://github.com/containers/bubblewrap/releases/download/v0.11.1/' + archive.name,
                                         headers={'User-Agent': 'Gitgo-native-CI'})
        with urllib.request.urlopen(request, timeout=30) as source:
            data = source.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit('bubblewrap source digest mismatch')
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise SystemExit('bubblewrap cached source digest mismatch')
    with tempfile.TemporaryDirectory(prefix='build-', dir=root) as raw:
        workspace = Path(raw)
        with tarfile.open(archive) as bundle:
            bundle.extractall(workspace, filter='data')
        source = workspace / 'bubblewrap-0.11.1'
        build = workspace / 'output'
        subprocess.run(['meson', 'setup', str(build), str(source), '-Dtests=false', '-Dman=disabled',
            '-Dselinux=disabled', '-Dbash_completion=disabled', '-Dzsh_completion=disabled'], check=True)
        subprocess.run(['ninja', '-C', str(build), 'bwrap'], check=True)
        subprocess.run(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '0755',
                        str(build / 'bwrap'), '/usr/bin/bwrap'], check=True)
    subprocess.run(['/usr/bin/bwrap', '--version'], check=True)
    print('Pinned unprivileged bubblewrap installed for this disposable worker only')


if __name__ == '__main__':
    main()
