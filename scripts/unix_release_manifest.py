"""Describe and verify Unix payloads without recording machine-specific paths."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_artifacts import source_identity, validate_versions
from scripts.render_product_manifest import render_manifest

BASELINES = {('macos', 'arm64'): 'macOS 15', ('linux', 'x86_64'): 'Ubuntu 24.04 / glibc 2.39'}

def inventory(stage):
    files = []
    for path in sorted(stage.rglob('*')):
        name = path.relative_to(stage).as_posix()
        if name in ('build-manifest.json', '.gitgo-install-owner'):
            continue
        if path.is_symlink():
            # Installer aliases and PyInstaller framework links must stay inside payload.
            target = path.readlink()
            if target.is_absolute() or not path.resolve(strict=True).is_relative_to(stage):
                raise ValueError('Release symlink escapes payload: ' + name)
            files.append({'path': name, 'link': str(target)})
        elif path.is_file():
            files.append({'path': name, 'bytes': path.stat().st_size,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return files

def write(stage, bun):
    stage = stage.resolve(strict=True)
    product = json.loads((stage / 'product.json').read_text())
    validate_versions(ROOT)
    target = product['platform'], product['architecture']
    if product != render_manifest(ROOT / 'packaging/product.json', *target):
        raise ValueError('Staged product contract differs from source')
    required = ('gitgo', 'internal/gitgo-host/gitgo-host', 'installer-common.sh',
                'install.sh', 'uninstall.sh', 'dashboard/dist/cli.js', 'dashboard/dist/trace.js',
                'dashboard/scripts/launch.mjs', 'dashboard/scripts/runtime.mjs')
    if any(not (stage / name).is_file() for name in required):
        raise ValueError('Incomplete native payload')
    import sqlite3
    dependencies = sorted({d.metadata['Name']: d.version for d in importlib.metadata.distributions()}.items())
    result = {'schema_version': 1, 'version': product['version'],
              'platform': target[0], 'architecture': target[1],
              'supported_baseline': BASELINES.get(target, 'experimental; no CI acceptance'),
              'source': source_identity(ROOT),
              'toolchain': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                            'bun': subprocess.check_output([bun, '--version'], text=True).strip(),
                            'pyinstaller': importlib.metadata.version('pyinstaller')},
              'dependencies': dict(dependencies), 'files': inventory(stage),
              'validation': {'build_smoke': True, 'independent_archive_acceptance': 'separate CI artifact',
                             'formal_terminal_verified': False, 'desktop_keyring_verified': False}}
    (stage / 'build-manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    return result

def verify(stage, commit):
    stage = stage.resolve(strict=True)
    manifest = json.loads((stage / 'build-manifest.json').read_text())
    source = manifest['source']
    if source['commit'] != commit or source['dirty'] or len(source['tree']) != 40:
        raise ValueError('Archive source identity is not the requested clean commit')
    if manifest['files'] != inventory(stage):
        raise ValueError('Archive payload hashes or layout differ from build manifest')
    product = json.loads((stage / 'product.json').read_text())
    if any(manifest[k] != product[k] for k in ('version', 'platform', 'architecture')):
        raise ValueError('Archive target identity differs from product')
    return manifest

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--bun', default='bun')
    args = parser.parse_args()
    write(args.stage, args.bun)
