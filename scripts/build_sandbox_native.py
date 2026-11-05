"""Build the locked Rust sandbox ABI for this platform; no runtime downloads."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cargo', default='cargo')
    parser.add_argument('--output', type=Path, default=ROOT / 'backend/native_runtime')
    args = parser.parse_args()
    manifest = ROOT / 'native/sandbox/Cargo.toml'
    completed = subprocess.run([args.cargo, '+1.90.0', 'build', '--release', '--locked',
        '--message-format=json-render-diagnostics', '--manifest-path', str(manifest)],
        stdout=subprocess.PIPE, text=True, check=False)
    name = {'win32': 'gitgo_native_sandbox.dll', 'darwin': 'libgitgo_native_sandbox.dylib',
            'linux': 'libgitgo_native_sandbox.so'}[sys.platform]
    # Cargo may use a configured target directory/architecture. Select the
    # actual emitted artifact, never a stale file at a guessed output path.
    artifacts = []
    dependency_manifests = set()
    for line in completed.stdout.splitlines():
        message = json.loads(line)
        if message.get('reason') == 'compiler-artifact' and message.get('manifest_path'):
            dependency_manifests.add(Path(message['manifest_path']))
        if message.get('reason') == 'compiler-message' and message['message'].get('rendered'):
            print(message['message']['rendered'], file=sys.stderr)
        if (message.get('reason') == 'compiler-artifact'
                and message.get('target', {}).get('name') == 'gitgo_native_sandbox'):
            artifacts.extend(Path(value) for value in message['filenames'] if Path(value).name == name)
    if completed.returncode:
        raise SystemExit(completed.returncode)
    if len(artifacts) != 1:
        raise SystemExit('Cargo did not emit exactly one native library for the host platform')
    source = artifacts[0].resolve(strict=True)
    expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    # Do not overwrite a previously staged binary: it may be mapped/pinned.
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / name
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise SystemExit('Native staging already exists with different code; use a fresh checkout/output')
    else:
        shutil.copy2(source, target)
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected_digest:
        raise SystemExit('Native staging differs from the actual Cargo artifact')
    # Cargo legitimately hard-links its own outputs. Probe the independent
    # staged copy, keeping the runtime's single-link rule fully intact.
    probe = subprocess.run([sys.executable, '-B', '-c',
        'import sys; from backend.core.native_sandbox import NativeLibrary; '
        'assert NativeLibrary(sys.argv[1]).call(op="capabilities")["abi"] == 1', str(target.resolve(strict=True))],
        cwd=ROOT, check=False)
    if probe.returncode:
        raise SystemExit('Staged native library failed the actual host loader/ABI check')
    notices = args.output / 'notices'
    notices.mkdir(exist_ok=True)
    packages = []
    for package_manifest in sorted(dependency_manifests):
        if package_manifest == manifest:
            continue
        package = tomllib.loads(package_manifest.read_text(encoding='utf8'))['package']
        packages.append({key: package.get(key) for key in ('name', 'version', 'license')})
        destination = notices / (package['name'] + '-' + package['version'])
        destination.mkdir(exist_ok=True)
        licenses = {item for item in package_manifest.parent.iterdir()
                    if item.is_file() and item.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE'))}
        if package.get('license-file'):
            licenses.add(package_manifest.parent / package['license-file'])
        if not licenses:
            raise SystemExit('Missing native dependency notices: ' + package['name'])
        for item in sorted(licenses):
            shutil.copy2(item, destination / item.name)
    rustc = Path(shutil.which(args.cargo) or args.cargo).with_name('rustc.exe' if os.name == 'nt' else 'rustc')
    sysroot = Path(subprocess.check_output([str(rustc), '+1.90.0', '--print', 'sysroot'], text=True).strip())
    rust_notices = sysroot / 'share/doc/rust'
    (notices / 'rust-1.90.0').mkdir(exist_ok=True)
    shutil.copy2(rust_notices / 'COPYRIGHT-library.html', notices / 'rust-1.90.0/COPYRIGHT-library.html')
    shutil.copytree(rust_notices / 'licenses', notices / 'rust-1.90.0/licenses', dirs_exist_ok=True)
    (notices / 'dependencies.json').write_text(json.dumps(packages, sort_keys=True) + '\n', encoding='utf8')
    metadata = {'abi_version': 1, 'platform': sys.platform, 'name': name,
                'sha256': expected_digest}
    (args.output / 'sandbox-build.json').write_text(json.dumps(metadata, sort_keys=True) + '\n', encoding='utf8')
    print('Built native sandbox ABI:', name, metadata['sha256'])


if __name__ == '__main__':
    main()
