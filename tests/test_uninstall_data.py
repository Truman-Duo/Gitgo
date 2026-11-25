from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from uuid import uuid4

import pytest


spec = spec_from_file_location('uninstall_data', Path(__file__).resolve().parents[1] / 'packaging/uninstall-data.py')
data = module_from_spec(spec)
spec.loader.exec_module(data)


def fixture_profile(root):
    profile = root / 'profile'
    profile.mkdir()
    workspace = root / 'workspace'
    metadata = workspace / '.gitgo'
    metadata.mkdir(parents=True)
    (metadata / 'project-id').write_text(str(uuid4()), encoding='ascii')
    (workspace / 'source.txt').write_text('keep', encoding='ascii')
    (profile / 'config.json').write_text(json.dumps({'projects': [
        {'name': 'test', 'workspace_path': str(workspace)},
    ]}), encoding='utf-8')
    (profile / 'commit-config.json').write_text('{}', encoding='utf-8')
    (profile / 'state').mkdir()
    (profile / 'state' / 'history').write_text('keep', encoding='ascii')
    return profile, workspace


@pytest.mark.parametrize('include_metadata', [False, True])
def test_clear_settings_never_removes_project_or_external_runtime(tmp_path_factory, include_metadata):
    profile, workspace = fixture_profile(tmp_path_factory)
    config = data.plan(profile, include_metadata)[0]
    data.cleanup(profile, include_metadata, config['token'], [])
    assert (workspace / 'source.txt').read_text() == 'keep'
    assert (workspace / '.gitgo').exists() is not include_metadata
    assert not (profile / 'config.json').exists()
    assert not (profile / 'commit-config.json').exists()
    assert (profile / 'state' / 'history').read_text() == 'keep'


def test_nested_project_in_metadata_is_kept(tmp_path_factory):
    profile, workspace = fixture_profile(tmp_path_factory)
    child = workspace / '.gitgo' / 'nested-project'
    (child / '.git').mkdir(parents=True)
    summary = data.plan(profile, True)[0]
    assert not summary['metadata'] and summary['skipped']
    data.cleanup(profile, True, summary['token'], [])
    assert (child / '.git').is_dir()


def test_stale_confirmation_does_not_modify_profile(tmp_path_factory):
    profile, workspace = fixture_profile(tmp_path_factory)
    summary = data.plan(profile, True)[0]
    (profile / 'config.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='changed after confirmation'):
        data.cleanup(profile, True, summary['token'], [])
    assert (profile / 'commit-config.json').is_file()
    assert workspace.is_dir()


def test_credential_failure_keeps_settings_and_metadata(tmp_path_factory, monkeypatch):
    profile, workspace = fixture_profile(tmp_path_factory)
    index = profile / 'provider_secrets.json'
    index.write_text('{}', encoding='utf-8')

    def unavailable(self):
        raise OSError('credential service unavailable')

    monkeypatch.setattr(data.EncryptedSecretStore, 'clear', unavailable)
    with pytest.raises(OSError, match='credential service unavailable'):
        data.cleanup(profile, True, None, [])
    assert index.exists()
    assert (profile / 'config.json').exists()
    assert (workspace / '.gitgo').exists()
