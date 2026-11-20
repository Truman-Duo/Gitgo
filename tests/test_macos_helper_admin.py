"""Fixed admin CLI routing without privileged operations or trust enablement."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import sys

import pytest

if sys.platform == 'win32':
    pytest.skip('macOS root entry guards', allow_module_level=True)

from backend.core import macos_helper_admin as admin
from backend.core import native_host_entry as entry


ROLE = ['--gitgo-internal-role', 'macos-helper-admin']


@pytest.fixture
def operator(monkeypatch, tmp_path_factory):
    # No process is elevated. Only guard inputs are replaced for unit routing.
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(os, 'getuid', lambda: 0)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    host = tmp_path_factory / 'release package/internal/gitgo-host/gitgo-host'
    host.parent.mkdir(parents=True)
    host.write_bytes(b'unit guard fixture')
    monkeypatch.setattr(sys, 'executable', str(host))
    monkeypatch.setenv('SUDO_UID', '501')
    return host


@pytest.fixture
def operations(monkeypatch):
    from backend.core import macos_helper_lifecycle as lifecycle
    from backend.core import macos_project_authority as authority
    calls = []

    def capture(operation):
        def called(*args, **kwargs):
            calls.append((operation, args, kwargs))
            return {'operation': operation, 'fixture': True}
        return called

    for operation in ('register', 'upgrade', 'status', 'recover', 'uninstall'):
        monkeypatch.setattr(lifecycle, operation, capture(operation))
    for operation in ('renew', 'revoke'):
        monkeypatch.setattr(authority, operation, capture(operation))
    return calls


@pytest.mark.parametrize('guard', ['platform', 'real_uid', 'effective_uid', 'frozen'])
def test_native_admin_rejects_wrong_context_before_opening_installation(
        monkeypatch, operator, operations, guard):
    from backend.core.update import installation
    if guard == 'platform': monkeypatch.setattr(sys, 'platform', 'linux')
    elif guard == 'real_uid': monkeypatch.setattr(os, 'getuid', lambda: 501)
    elif guard == 'effective_uid': monkeypatch.setattr(os, 'geteuid', lambda: 501)
    else: monkeypatch.setattr(sys, 'frozen', False)

    def forbidden(*args, **kwargs):
        pytest.fail('installation state was opened before the admin guard')

    monkeypatch.setattr(installation, 'installed_runtime', forbidden)
    with pytest.raises(PermissionError):
        entry.main([*ROLE, 'status', '--name', 'fixture'])
    assert operations == []


@pytest.mark.parametrize('marker_kind', ['file', 'link', 'directory'])
@pytest.mark.parametrize('alias', [False, True])
def test_native_admin_refuses_managed_active_even_through_alias_before_admission(
        monkeypatch, tmp_path_factory, operator, operations, marker_kind, alias):
    from backend.core.update import installation
    root = tmp_path_factory / 'managed/active'
    host = root / 'internal/gitgo-host/gitgo-host'
    host.parent.mkdir(parents=True)
    host.write_bytes(b'managed fixture')
    control = root.parent / 'control'
    control.mkdir()
    marker = control / 'registration.json'
    if marker_kind == 'file': marker.write_text('{}')
    elif marker_kind == 'directory': marker.mkdir()
    else: marker.symlink_to(control / 'missing')
    if alias:
        link = tmp_path_factory / 'host alias'
        link.symlink_to(host)
        host = link
    monkeypatch.setattr(sys, 'executable', str(host))
    monkeypatch.setattr(installation, 'installed_runtime', lambda *args: pytest.fail('managed store opened'))
    with pytest.raises(PermissionError, match='MACOS_HELPER_MANAGED_LOCATION'):
        entry.main([*ROLE, 'status', '--name', 'fixture'])
    assert operations == []


def test_source_admin_also_refuses_managed_location(monkeypatch, tmp_path_factory, operator, operations):
    root = tmp_path_factory / 'managed/active'
    control = root.parent / 'control'
    control.mkdir(parents=True)
    (control / 'registration.json').write_text('{}')
    monkeypatch.setattr(admin, '__file__', str(root / 'backend/core/macos_helper_admin.py'))
    with pytest.raises(PermissionError, match='MACOS_HELPER_MANAGED_LOCATION'):
        admin.main(['status', '--name', 'fixture'])
    assert operations == []


def test_sealed_admin_rechecks_its_complete_install_receipt_before_operations(monkeypatch,operator,operations):
    from backend.core import macos_helper_install as install
    host=install.BASE/('a'*64)/'runtime/gitgo-host'
    monkeypatch.setattr(sys,'executable',str(host))
    checked=[]
    def reject(path):
        checked.append(path)
        raise ValueError('damaged sealed manager')
    monkeypatch.setattr(install,'verify_bundle_path',reject)
    with pytest.raises(ValueError,match='sealed manager'):
        admin.frozen_main(['status','--name','fixture'])
    assert checked==[host] and operations==[]


def test_fixed_role_keeps_normal_installed_runtime_context(monkeypatch, operator, operations, capsys):
    from backend.core.update import installation
    trace = []

    @contextmanager
    def admitted(role):
        trace.append(('enter', role))
        try: yield
        finally: trace.append(('exit', role))

    monkeypatch.setattr(installation, 'installed_runtime', admitted)
    monkeypatch.setattr(entry, '_run_python_role', lambda *args: pytest.fail('generic Python role entered'))
    assert entry.main([*ROLE, 'status', '--name', 'fixture']) == 0
    assert trace == [('enter', 'macos-helper-admin'), ('exit', 'macos-helper-admin')]
    assert operations == [('status', ('fixture',), {})]
    assert json.loads(capsys.readouterr().out) == {
        'result': {'operation': 'status', 'fixture': True}, 'production_enabled': False,
    }


def test_installed_runtime_rejection_still_blocks_fixed_role(monkeypatch, operator, operations, capsys):
    from backend.core.update import installation
    from backend.core.update.validation import UpdateError

    @contextmanager
    def refused(role):
        raise UpdateError('UPDATE_INSTALLATION_BLOCKED', 'fixture ownership failure')
        yield

    monkeypatch.setattr(installation, 'installed_runtime', refused)
    assert entry.main([*ROLE, 'status', '--name', 'fixture']) == 2
    assert operations == []
    assert json.loads(capsys.readouterr().out)['code'] == 'UPDATE_INSTALLATION_BLOCKED'


@pytest.mark.parametrize('argv,expected', [
    (['install', '--name', 'fixture', '--bundle', '/candidate', '--project', '/project',
      '--expected-bundle-sha256', 'a'*64],
     ('register', ('fixture', '/candidate', '/project', 501),
      {'lifetime': 180, 'allow_local_adhoc': False, 'expected_bundle_sha256': 'a'*64})),
    (['upgrade', '--name', 'fixture', '--bundle', '/candidate', '--expected-bundle-sha256', 'a'*64],
     ('upgrade', ('fixture', '/candidate'), {'allow_local_adhoc': False, 'expected_bundle_sha256': 'a'*64})),
    (['install', '--name', 'fixture', '--bundle', '/candidate bundle', '--project', '/test project'],
     ('register', ('fixture', '/candidate bundle', '/test project', 501),
      {'lifetime': 180, 'allow_local_adhoc': False})),
    (['install', '--name', 'fixture', '--bundle', '/candidate', '--project', '/test',
      '--uid', '502', '--lifetime', '1800', '--allow-local-adhoc'],
     ('register', ('fixture', '/candidate', '/test', 502),
      {'lifetime': 1800, 'allow_local_adhoc': True})),
    (['upgrade', '--name', 'fixture', '--bundle', '/candidate'],
     ('upgrade', ('fixture', '/candidate'), {'allow_local_adhoc': False})),
    (['upgrade', '--name', 'fixture', '--bundle', '/candidate', '--allow-local-adhoc'],
     ('upgrade', ('fixture', '/candidate'), {'allow_local_adhoc': True})),
    (['renew', '--name', 'fixture'],
     ('renew', ('fixture',), {'lifetime': 3600, 'after_reboot': False})),
    (['renew', '--name', 'fixture', '--lifetime', '1800', '--after-reboot'],
     ('renew', ('fixture',), {'lifetime': 1800, 'after_reboot': True})),
    (['revoke', '--name', 'fixture'], ('revoke', ('fixture',), {})),
    (['recover', '--name', 'fixture'], ('recover', ('fixture',), {})),
    (['uninstall', '--name', 'fixture'], ('uninstall', ('fixture',), {})),
])
def test_fixed_admin_cli_preserves_explicit_flags_and_defaults(operator, operations, argv, expected, capsys):
    assert admin.frozen_main(argv) == 0
    assert operations == [expected]
    assert json.loads(capsys.readouterr().out)['production_enabled'] is False


@pytest.mark.parametrize('argv', [
    ['upgrade', '--name', 'fixture', '--bundle', '/candidate', '--allow-local-adhoc',
     '--expected-bundle-sha256', 'a'*64],
    ['-c', 'anything'], ['-m', 'anything'], ['/external/manage.py'],
    ['status', '--name', 'fixture', '--production-enabled'],
    ['install', '--name', 'fixture', '--bundle', '/candidate', '--project', '/project',
     '--team-identifier', 'TEST123456'],
    ['install', '--name', 'fixture'],
])
def test_admin_rejects_arbitrary_execution_or_trust_overrides_before_operations(operator, operations, argv):
    with pytest.raises(SystemExit) as error:
        admin.frozen_main(argv)
    assert error.value.code == 2
    assert operations == []


def test_source_wrapper_delegates_to_fixed_module(monkeypatch, capsys):
    wrapper = Path(__file__).resolve().parents[1] / 'scripts/manage_macos_sandbox_service.py'
    calls = []
    monkeypatch.setattr(admin, 'main', lambda: calls.append('fixed-module') or 0)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(wrapper), run_name='__main__')
    assert result.value.code == 0 and calls == ['fixed-module']
    assert capsys.readouterr().out == ''


def test_generic_python_role_does_not_gain_admin_dispatch(monkeypatch, capsys):
    from backend.core.update import installation
    trace = []

    @contextmanager
    def admitted(role):
        trace.append(role)
        yield

    monkeypatch.setattr(installation, 'installed_runtime', admitted)
    monkeypatch.setattr(admin, 'require_frozen_admin_entry', lambda: pytest.fail('admin role entered'))
    monkeypatch.setattr(entry, '_run_python_role', lambda argv: trace.append(argv) or 7)
    assert entry.main(['--gitgo-internal-role', 'python', '-c', 'fixture']) == 7
    assert trace == ['python', ['-c', 'fixture']]
