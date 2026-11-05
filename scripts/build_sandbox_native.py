"""Build the locked Rust sandbox ABI for this platform; no runtime downloads."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cargo', default='cargo')
    parser.add_argument('--output', type=Path, default=ROOT / 'backend/native_runtime')
    args = parser.parse_args()
    manifest = ROOT / 'native/sandbox/Cargo.toml'
    subprocess.run([args.cargo, '+1.90.0', 'build', '--release', '--locked', '--manifest-path', str(manifest)], check=True)
    name = {'win32': 'gitgo_native_sandbox.dll', 'darwin': 'libgitgo_native_sandbox.dylib',
            'linux': 'libgitgo_native_sandbox.so'}[sys.platform]
    source = manifest.parent / 'target/release' / name
    # Do not overwrite a previously staged binary: it may be mapped/pinned.
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / name
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise SystemExit('Native staging already exists with different code; use a fresh checkout/output')
    else:
        shutil.copy2(source, target)
    metadata = {'abi_version': 1, 'platform': sys.platform, 'name': name,
                'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    (args.output / 'sandbox-build.json').write_text(json.dumps(metadata, sort_keys=True) + '\n', encoding='utf8')
    print('Built native sandbox ABI:', name, metadata['sha256'])


if __name__ == '__main__':
    main()
