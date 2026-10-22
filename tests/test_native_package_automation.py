"""Source identity, payload tampering and fail-closed workflow regressions."""
import json
from pathlib import Path
import re
import pytest
import yaml
from scripts.unix_release_manifest import inventory, verify

ROOT = Path(__file__).resolve().parents[1]

def payload(tmp_path_factory):
    (tmp_path_factory / 'gitgo').write_bytes(b'fixture-executable')
    product = {'version': '1.0.0', 'platform': 'linux', 'architecture': 'x86_64'}
    (tmp_path_factory / 'product.json').write_text(json.dumps(product))
    manifest = {**product, 'source': {'commit': 'a' * 40, 'tree': 'b' * 40, 'dirty': False},
                'files': inventory(tmp_path_factory)}
    (tmp_path_factory / 'build-manifest.json').write_text(json.dumps(manifest))
    return tmp_path_factory

def test_clean_payload_identity_and_hashes(tmp_path_factory):
    assert verify(payload(tmp_path_factory), 'a' * 40)['source']['tree'] == 'b' * 40

def test_installer_ownership_marker_does_not_change_payload_identity(tmp_path_factory):
    payload(tmp_path_factory)
    (tmp_path_factory / '.gitgo-install-owner').write_text('fixture')
    verify(tmp_path_factory, 'a' * 40)

@pytest.mark.parametrize('change', ['bytes', 'extra', 'commit', 'dirty'])
def test_modified_payload_or_source_is_rejected(tmp_path_factory, change):
    payload(tmp_path_factory)
    if change == 'bytes': (tmp_path_factory / 'gitgo').write_bytes(b'tampered')
    if change == 'extra': (tmp_path_factory / 'unexpected').touch()
    if change in ('commit', 'dirty'):
        path = tmp_path_factory / 'build-manifest.json'
        data = json.loads(path.read_text())
        data['source'][change] = True if change == 'dirty' else 'c' * 40
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError): verify(tmp_path_factory, 'a' * 40)

def test_external_symlink_cannot_be_release_asset(tmp_path_factory):
    stage = tmp_path_factory / 'stage'
    stage.mkdir()
    external = tmp_path_factory / 'external.txt'
    external.write_text('outside the payload')
    try:
        (stage / 'outside').symlink_to(external)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows account cannot create symbolic links')
        raise
    with pytest.raises(ValueError, match='escapes payload'):
        inventory(stage)

def test_required_native_and_runtime_jobs_are_in_gate():
    ci = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    required = {'runtime', 'unix-packages', 'windows-package', 'managed-update-windows', 'managed-update-linux'}
    assert required <= set(ci['jobs']['gate']['needs'])
    check = ci['jobs']['gate']['steps'][0]['run']
    for name in required: assert repr(name) in check
    native = yaml.safe_load((ROOT / '.github/workflows/linux-package.yml').read_text())
    assert set(native['jobs']['accept']['needs']) == {'resolve', 'build'}
    assert native['jobs']['build']['strategy']['matrix'] == native['jobs']['accept']['strategy']['matrix'] or {
        (x['os'], x['target']) for x in native['jobs']['build']['strategy']['matrix']['include']
    } == {(x['os'], x['target']) for x in native['jobs']['accept']['strategy']['matrix']['include']}

def test_all_external_actions_are_pinned_and_checkout_does_not_persist_credentials():
    for name in ('ci.yml', 'linux-package.yml', 'dashboard-runtime.yml', 'linux-update.yml', 'windows-update.yml'):
        workflow = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
        for job in workflow['jobs'].values():
            for step in job.get('steps', []):
                action = step.get('uses', '')
                if not action: continue
                assert re.fullmatch(r'[^@]+@[0-9a-f]{40}', action), action
                if action.startswith('actions/checkout@'):
                    assert step['with']['persist-credentials'] is False
                assert step.get('with', {}).get('bun-version') != 'latest'

def test_build_lock_has_exact_versions_and_real_digest_shape():
    data = (ROOT / 'packaging/requirements-build.lock').read_text()
    entries = re.split(r'\n(?=[A-Za-z])', data[data.index('altgraph=='):])
    assert len(entries) > 40
    for entry in entries:
        assert re.match(r'[\w.-]+==[^\s;]+', entry)
        assert re.search(r'--hash=sha256:[a-f0-9]{64}', entry)


def test_locked_build_covers_declared_portable_runtime_dependencies():
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    locked = {}
    for line in (ROOT / 'packaging/requirements-build.lock').read_text().splitlines():
        match = re.match(r'([\w.-]+)==([^\s;]+)', line)
        if match:
            locked[canonicalize_name(match[1])] = match[2]
    for line in (ROOT / 'requirements-portable.txt').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        assert name in locked, f'Portable runtime dependency missing from build lock: {name}'
        assert locked[name] in requirement.specifier, f'Locked {name} violates portable runtime requirements'
