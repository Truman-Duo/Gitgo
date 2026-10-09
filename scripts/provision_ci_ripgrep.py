"""Provision pinned official ripgrep only inside disposable hosted CI."""
from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

VERSION = '15.2.0'
ARCHIVES = {
    'win32': ('x86_64-pc-windows-msvc.zip',
        '71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5'),
    'linux': ('x86_64-unknown-linux-musl.tar.gz',
        '33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c'),
}

def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Only disposable GitHub-hosted CI is supported')
    suffix, expected = ARCHIVES[sys.platform]
    name = f'ripgrep-{VERSION}-{suffix}'
    url = f'https://github.com/BurntSushi/ripgrep/releases/download/{VERSION}/{name}'
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit('Official ripgrep archive hash mismatch')
    binary_name = 'rg.exe' if sys.platform == 'win32' else 'rg'
    if sys.platform == 'win32':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [member for member in archive.infolist() if Path(member.filename).name == binary_name and not member.is_dir()]
            if len(members) != 1 or members[0].file_size > 64 * 1024 * 1024:
                raise SystemExit('Unexpected ripgrep archive layout')
            binary = archive.read(members[0])
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            members = [member for member in archive.getmembers() if Path(member.name).name == binary_name and member.isfile()]
            if len(members) != 1 or members[0].size > 64 * 1024 * 1024:
                raise SystemExit('Unexpected ripgrep archive layout')
            with archive.extractfile(members[0]) as handle:
                binary = handle.read()
    # No extractall or caller-selected destination; only one verified binary.
    directory = Path(__file__).resolve().parents[1] / '.gitgo/ci-ripgrep'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / binary_name
    target.write_bytes(binary)
    target.chmod(0o755)
    banner = subprocess.check_output([str(target), '--version'], text=True).splitlines()[0]
    if not banner.startswith(f'ripgrep {VERSION}'):
        raise SystemExit('Pinned ripgrep did not report its declared version')
    with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as handle:
        handle.write(str(directory) + '\n')
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as handle:
        handle.write('GITGO_RIPGREP_PATH=' + str(target) + '\n')
    print('Verified official', banner, 'archive SHA256', expected)

if __name__ == '__main__':
    main()
