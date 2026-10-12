import json
from pathlib import Path

import pytest

from backend.version import __version__
from scripts.release_artifacts import validate_versions, package
from scripts.render_product_manifest import render_manifest
import hashlib
import subprocess
import zipfile


def fixture(root: Path):
    (root / 'packaging').mkdir()
    (root / 'cli/dashboard').mkdir(parents=True)
    (root / 'packaging/product.json').write_text(json.dumps({'version': __version__, 'windows_version': '1.0.0.2'}))
    (root / 'cli/dashboard/package.json').write_text(json.dumps({'version': __version__}))


def test_versions_must_match_actual_host(tmp_path_factory):
    fixture(tmp_path_factory)
    assert validate_versions(tmp_path_factory)['version'] == __version__
    (tmp_path_factory / 'cli/dashboard/package.json').write_text('{"version":"0.1.0"}')
    with pytest.raises(ValueError, match='disagree'):
        validate_versions(tmp_path_factory)


def test_windows_version_rejects_overflow(tmp_path_factory):
    fixture(tmp_path_factory)
    p = tmp_path_factory / 'packaging/product.json'
    product = json.loads(p.read_text())
    product['windows_version'] = '1.0.0.65536'
    p.write_text(json.dumps(product))
    with pytest.raises(ValueError, match='Windows'):
        validate_versions(tmp_path_factory)


def staging(root):
    fixture(root)
    product = {'schema_version': 2, 'product_id': 'gitgo-terminal',
        'version': __version__, 'windows_version': '1.0.0.2', 'primary_command': 'gitgo',
        'layouts': {'windows': {'dashboard': 'gitgo.exe', 'native_host': 'internal/gitgo-host/gitgo-host.exe'},
                    'macos': {'dashboard': 'gitgo', 'native_host': 'internal/gitgo-host/gitgo-host'}}}
    (root / 'packaging/product.json').write_text(json.dumps(product))
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                    'user.email=test@local.invalid', 'commit', '-m', 'fixture'], check=True, capture_output=True)
    stage = root / 'stage'
    stage.mkdir()
    manifest = render_manifest(root / 'packaging/product.json', 'windows', 'x86_64')
    (stage / 'product.json').write_text(json.dumps(manifest))
    for name in ['gitgo.exe', 'internal/gitgo-host/gitgo-host.exe', 'dashboard/dist/cli.js',
                 'dashboard/dist/trace.js', 'dashboard/scripts/launch.mjs', 'dashboard/scripts/runtime.mjs']:
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic test asset')
    return stage


def test_actual_windows_archive_matches_rendered_contract_and_hashes(tmp_path_factory):
    root = tmp_path_factory.resolve()
    stage = staging(root)
    result = package(root, stage, root / 'archives')
    archive = Path(result['archive'])
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == result['sha256']
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        manifest = json.loads(bundle.read('build-manifest.json'))
        assert manifest['source']['commit'] == result['source']['commit']
        assert len(manifest['source']['tree']) == 40
        assert manifest['architecture'] == 'x86_64'
        for item in manifest['files']:
            assert hashlib.sha256(bundle.read(item['path'])).hexdigest() == item['sha256']
    with pytest.raises(FileExistsError):
        package(root, stage, root / 'archives')


def test_archive_cannot_include_its_own_output(tmp_path_factory):
    root = tmp_path_factory.resolve()
    stage = staging(root)
    with pytest.raises(ValueError, match='outside'):
        package(root, stage, stage / 'archives')
    assert not (stage / 'archives').exists()


@pytest.mark.parametrize('kind', ['metadata', 'missing-host', 'missing-node', 'redirected', 'traversal'])
def test_archive_keeps_platform_metadata_payload_and_scope_checks(tmp_path_factory, kind):
    root = tmp_path_factory.resolve()
    stage = staging(root)
    if kind == 'metadata':
        manifest = json.loads((stage / 'product.json').read_text())
        manifest['version'] = '0.1.0'
        (stage / 'product.json').write_text(json.dumps(manifest))
    elif kind == 'missing-host': (stage / 'internal/gitgo-host/gitgo-host.exe').unlink()
    elif kind == 'missing-node': (stage / 'dashboard/scripts/runtime.mjs').unlink()
    elif kind == 'redirected':
        if __import__('os').name == 'nt': pytest.skip('symlink privilege is environment dependent')
        (stage / 'gitgo.exe').unlink()
        outside = root / 'outside.exe'
        outside.write_bytes(b'outside')
        (stage / 'gitgo.exe').symlink_to(outside)
    else:
        source = root / 'packaging/product.json'
        product = json.loads(source.read_text())
        product['layouts']['windows']['dashboard'] = '../outside.exe'
        source.write_text(json.dumps(product))
        (stage / 'product.json').write_text(json.dumps(render_manifest(source, 'windows', 'x86_64')))
    with pytest.raises((ValueError, FileNotFoundError)):
        package(root, stage, root / 'archives')
    assert not (root / 'archives').exists()
