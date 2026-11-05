"""Check Actions schemas/expressions with a pinned official developer tool."""
import io
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.ci_downloads import fetch_verified
from scripts.development_paths import development_root

VERSION = '1.7.12'
DIGESTS = {
    ('win32', 'amd64'): '6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9',
    ('win32', 'arm64'): 'cadcf7ea4efe3a68728893813643cebe1185e5b1d4be5b96245f65c9a4d5ea41',
    ('linux', 'amd64'): '8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8',
    ('linux', 'arm64'): '325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6',
    ('darwin', 'amd64'): '5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644',
    ('darwin', 'arm64'): 'aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f',
}


def main():
    machine = platform.machine().lower()
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(machine, machine)
    expected = DIGESTS.get((sys.platform, architecture))
    if expected is None:
        raise SystemExit('Unsupported developer-tool platform')
    target = {'win32': 'windows', 'darwin': 'darwin', 'linux': 'linux'}[sys.platform]
    suffix = 'zip' if target == 'windows' else 'tar.gz'
    name = f'actionlint_{VERSION}_{target}_{architecture}.{suffix}'
    raw = fetch_verified(f'https://github.com/rhysd/actionlint/releases/download/v{VERSION}/{name}',
                         expected, limit=16 * 1024 * 1024)
    executable = 'actionlint.exe' if target == 'windows' else 'actionlint'
    if suffix == 'zip':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entry = archive.getinfo(executable)
            if entry.is_dir() or not 1 <= entry.file_size <= 16 * 1024 * 1024:
                raise ValueError('Unexpected official tool archive')
            binary = archive.read(entry)
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            entry = archive.getmember(executable)
            if not entry.isfile() or not 1 <= entry.size <= 16 * 1024 * 1024:
                raise ValueError('Unexpected official tool archive')
            with archive.extractfile(entry) as source:
                binary = source.read()
    output = development_root(ROOT) / 'ci-actionlint'
    output.mkdir(parents=True, exist_ok=True)
    tool = output / executable
    tool.write_bytes(binary)
    tool.chmod(0o755)
    return subprocess.run([str(tool), '-shellcheck=', '-pyflakes='], cwd=ROOT).returncode


if __name__ == '__main__':
    raise SystemExit(main())
