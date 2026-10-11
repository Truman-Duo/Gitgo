import json
from pathlib import Path

import pytest

from backend.version import __version__
from scripts.release_artifacts import validate_versions


def fixture(root: Path):
    (root / 'packaging').mkdir()
    (root / 'cli/dashboard').mkdir(parents=True)
    (root / 'packaging/product.json').write_text(json.dumps({'version': __version__, 'windows_version': '1.0.0.2'}))
    (root / 'cli/dashboard/package.json').write_text(json.dumps({'version': __version__}))


def test_versions_must_match_actual_host(tmp_path):
    fixture(tmp_path)
    assert validate_versions(tmp_path)['version'] == __version__
    (tmp_path / 'cli/dashboard/package.json').write_text('{"version":"0.1.0"}')
    with pytest.raises(ValueError, match='disagree'):
        validate_versions(tmp_path)


def test_windows_version_rejects_overflow(tmp_path):
    fixture(tmp_path)
    p = tmp_path / 'packaging/product.json'
    product = json.loads(p.read_text())
    product['windows_version'] = '1.0.0.65536'
    p.write_text(json.dumps(product))
    with pytest.raises(ValueError, match='Windows'):
        validate_versions(tmp_path)
