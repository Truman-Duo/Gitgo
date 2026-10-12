"""Provision pinned official ripgrep only inside disposable hosted CI."""
from __future__ import annotations
import hashlib
import io
import os
import platform
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ci_downloads import fetch_verified

VERSION = '15.2.0'
ARCHIVES = {
    'win32': ('x86_64-pc-windows-msvc.zip',
        '71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5'),
    'linux': ('x86_64-unknown-linux-musl.tar.gz',
        '33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c'),
    'darwin-arm64': ('aarch64-apple-darwin.tar.gz',
        '3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4'),
    'darwin-x86_64': ('x86_64-apple-darwin.tar.gz',
        'af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1'),
}

NOTICE_FILES = ('COPYING', 'LICENSE-MIT', 'UNLICENSE')


def archive_notices(entries):
    """Keep all notices shipped with the exact hash-verified release archive."""
    sections = []
    for name in NOTICE_FILES:
        matches = [data for path, data in entries if Path(path).name == name]
        if len(matches) != 1 or not 1 <= len(matches[0]) <= 1024 * 1024:
            raise ValueError('Missing or ambiguous official ripgrep notice: ' + name)
        sections.append(name.encode() + b'\n' + matches[0])
    result = b'\n\n'.join(sections)
    if len(result) > 1024 * 1024:
        raise ValueError('Official ripgrep notices exceed the release limit')
    return result

def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Only disposable GitHub-hosted CI is supported')
    key = f'darwin-{platform.machine()}' if sys.platform == 'darwin' else sys.platform
    suffix, expected = ARCHIVES[key]
    name = f'ripgrep-{VERSION}-{suffix}'
    url = f'https://github.com/BurntSushi/ripgrep/releases/download/{VERSION}/{name}'
    raw = fetch_verified(url, expected, limit=64 * 1024 * 1024)
    binary_name = 'rg.exe' if sys.platform == 'win32' else 'rg'
    if sys.platform == 'win32':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [member for member in archive.infolist() if Path(member.filename).name == binary_name and not member.is_dir()]
            if len(members) != 1 or members[0].file_size > 64 * 1024 * 1024:
                raise SystemExit('Unexpected ripgrep archive layout')
            binary = archive.read(members[0])
            notices = archive_notices([(member.filename, archive.read(member))
                for member in archive.infolist() if Path(member.filename).name in NOTICE_FILES
                and not member.is_dir() and member.file_size <= 1024 * 1024])
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            members = [member for member in archive.getmembers() if Path(member.name).name == binary_name and member.isfile()]
            if len(members) != 1 or members[0].size > 64 * 1024 * 1024:
                raise SystemExit('Unexpected ripgrep archive layout')
            with archive.extractfile(members[0]) as handle:
                binary = handle.read()
            entries = []
            for member in archive.getmembers():
                if Path(member.name).name in NOTICE_FILES and member.isfile() and member.size <= 1024 * 1024:
                    with archive.extractfile(member) as handle:
                        entries.append((member.name, handle.read()))
            notices = archive_notices(entries)
    # No extractall or caller-selected destination; only one verified binary.
    directory = Path(__file__).resolve().parents[1] / '.gitgo/ci-ripgrep'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / binary_name
    target.write_bytes(binary)
    target.chmod(0o755)
    notice_path = directory / 'ripgrep-notices.txt'
    notice_path.write_bytes(notices)
    banner = subprocess.check_output([str(target), '--version'], text=True).splitlines()[0]
    if not banner.startswith(f'ripgrep {VERSION}'):
        raise SystemExit('Pinned ripgrep did not report its declared version')
    with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as handle:
        handle.write(str(directory) + '\n')
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as handle:
        handle.write('GITGO_RIPGREP_PATH=' + str(target) + '\n')
        handle.write('GITGO_RIPGREP_NOTICES=' + str(notice_path) + '\n')
    print('Verified official', banner, 'archive SHA256', expected)

if __name__ == '__main__':
    main()
