"""Validate product versions and create traceable Windows ZIP artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.version import __version__


def validate_versions(root: Path) -> dict:
    product = json.loads((root / 'packaging/product.json').read_text(encoding='utf-8'))
    dashboard = json.loads((root / 'cli/dashboard/package.json').read_text(encoding='utf-8'))
    version = product.get('version', '')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9]+(?:\.[a-z0-9]+)*)?', version):
        raise ValueError('Invalid product version')
    if version != dashboard.get('version') or version != __version__:
        raise ValueError('Product, Dashboard and Host versions disagree')
    windows = product.get('windows_version', '')
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', windows) or any(int(x) > 65535 for x in windows.split('.')):
        raise ValueError('Invalid Windows file version')
    return product


def source_identity(root: Path) -> dict:
    def git(*args: str) -> str:
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    return {'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'),
            'dirty': bool(git('status', '--porcelain', '--untracked-files=normal'))}


def package(root: Path, stage: Path, output: Path) -> dict:
    product = validate_versions(root)
    stage = stage.resolve(strict=True)
    staged = json.loads((stage / 'product.json').read_text(encoding='utf-8'))
    if staged != product:
        raise ValueError('Staged product metadata differs from the current source')
    required = (product['layout']['dashboard'], product['layout']['native_host'])
    for name in required:
        target = (stage / name).resolve(strict=True)
        if not target.is_relative_to(stage) or not target.is_file():
            raise ValueError('Missing or redirected product component')
    files = []
    for path in sorted(stage.rglob('*')):
        if path.is_symlink() or not path.resolve().is_relative_to(stage):
            raise ValueError('Redirected release asset')
        if path.is_file() and path.name != 'build-manifest.json':
            files.append({'path': path.relative_to(stage).as_posix(), 'bytes': path.stat().st_size,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {'schema_version': 1, 'version': product['version'], 'platform': 'win-x64',
                'source': source_identity(root), 'files': files,
                'validation': {'packaged_smoke_required': True, 'formal_terminal_verified': False}}
    # This records build identity. Human/API acceptance is recorded separately.
    (stage / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"gitgo-{product['version']}-win-x64-{manifest['source']['commit'][:8]}.zip"
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                bundle.write(path, arcname=path.relative_to(stage).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError('Release ZIP integrity check failed')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    return {'version': product['version'], 'archive': str(archive), 'sha256': digest,
            'source': manifest['source'], 'files': len(files)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.check:
        print('Release version:', validate_versions(args.root)['version'])
    else:
        if args.stage is None or args.output is None:
            parser.error('--stage and --output are required for packaging')
        print(json.dumps(package(args.root, args.stage, args.output), indent=2))


if __name__ == '__main__':
    main()
