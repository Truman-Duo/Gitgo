"""Real signed probe rejection tests. These do NOT establish VM isolation.

No test installs or boots macOS. Full VM acceptance requires a separate real
guest, trusted agent, attacks, budgets and crash/recovery evidence.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from scripts.run_macos_guest_probe import build_probe, main


@pytest.fixture(scope='module')
def lifecycle_probe():
    if sys.platform != 'darwin':
        pytest.skip('Apple Virtualization rejection tests require macOS')
    with tempfile.TemporaryDirectory(prefix='gitgo-guest-probe-test-') as raw:
        executable = build_probe('guest_lifecycle', Path(raw))
        check = subprocess.run([str(executable), 'boot'], capture_output=True, text=True, timeout=20)
        if '"event":"framework_unavailable"' in check.stdout:
            pytest.skip('Virtualization unavailable in this execution context; no guest evidence')
        assert check.returncode == 1 and 'Invalid experiment arguments' in check.stderr
        yield executable


@pytest.fixture
def tmp_path(tmp_path_factory):
    # This repository overrides pytest's factory with a function-scoped Path.
    return tmp_path_factory


def invoke(executable, *arguments):
    result = subprocess.run([str(executable), *map(str, arguments)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1, (result.stdout, result.stderr)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events
    assert all(event['production_backend_enabled'] is False and
               event['guest_command_execution_tested'] is False for event in events)
    assert not any(event['event'] in {'framework_started', 'installation_started'} for event in events)
    return events


def test_invalid_image_never_creates_vm(lifecycle_probe, tmp_path):
    image = tmp_path / 'not-an-image.ipsw'
    image.write_bytes(b'not an Apple restore image')
    destination = tmp_path / 'new-vm'
    events = invoke(lifecycle_probe, 'install', image, destination)
    assert events[-1]['event'] == 'restore_image_rejected'
    assert not destination.exists()


@pytest.mark.parametrize('contents', [
    b'{', b'[]', b'null', b'{"phase":"installed"}',
    b'{"phase":"created","schema_version":1}',
    b'{"phase":"installed","schema_version":true}',
])
def test_incomplete_or_malformed_vm_cannot_boot(lifecycle_probe, tmp_path, contents):
    root = tmp_path / 'vm'
    root.mkdir(mode=0o700)
    metadata = root / 'configuration.json'
    metadata.write_bytes(contents)
    metadata.chmod(0o600)
    events = invoke(lifecycle_probe, 'boot', root)
    assert events[-1]['event'] == 'installation_incomplete'
    assert sorted(path.name for path in root.iterdir()) == ['configuration.json']


@pytest.mark.parametrize('link', ['root', 'metadata'])
def test_linked_vm_assets_are_rejected(lifecycle_probe, tmp_path, link):
    root = tmp_path / 'private-vm'
    root.mkdir(mode=0o700)
    data = tmp_path / 'data.json'
    data.write_text('{}')
    data.chmod(0o600)
    entry = root / 'configuration.json'
    if link == 'metadata':
        entry.symlink_to(data)
        requested = root
    else:
        entry.write_text('{}')
        entry.chmod(0o600)
        requested = tmp_path / 'vm-alias'
        requested.symlink_to(root, target_is_directory=True)
    events = invoke(lifecycle_probe, 'boot', requested)
    assert events[-1]['event'] == 'experiment_directory_rejected'


def test_shared_directory_is_not_an_experiment(lifecycle_probe, tmp_path):
    root = tmp_path / 'public-vm'
    root.mkdir()
    root.chmod(0o777)
    events = invoke(lifecycle_probe, 'boot', root)
    assert events[-1]['event'] == 'experiment_directory_rejected'


@pytest.mark.parametrize('model,identifier', [('?', 'aA=='), ('aA==', '?')])
def test_invalid_encoded_identity_cannot_reach_apple_constructor(lifecycle_probe, tmp_path, model, identifier):
    root = tmp_path / 'vm'
    root.mkdir(mode=0o700)
    metadata = root / 'configuration.json'
    metadata.write_text(json.dumps({
        'schema_version': 1, 'phase': 'installed', 'build_version': 'test',
        'cpu_count': 2, 'memory_bytes': 4 * 1024**3,
        'hardware_model': model, 'machine_identifier': identifier,
    }))
    metadata.chmod(0o600)
    events = invoke(lifecycle_probe, 'boot', root)
    assert events[-1]['event'] == 'configuration_rejected'


def test_cli_preserves_existing_vm_directory(tmp_path):
    if sys.platform != 'darwin':
        pytest.skip('Mac development CLI')
    existing = tmp_path / 'existing'
    existing.mkdir()
    sentinel = existing / 'user-data'
    sentinel.write_text('retain')
    with pytest.raises(SystemExit) as error:
        main(['install', '--restore-image', os.devnull, '--new-vm-directory', str(existing)])
    assert error.value.code == 2
    assert sentinel.read_text() == 'retain'
