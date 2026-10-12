from __future__ import annotations
import base64
import ctypes as C
from dataclasses import replace
from ctypes import wintypes as W
import json
import hashlib
import re
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core.executable_identity import windows_system_executable
from backend.core.sandbox import SandboxPolicy
from backend.core.windows_acl import SecurityTree, SecurityConflict
from backend.core.windows_security_descriptor import SecurityDescriptor
from backend.core.windows_security_privileges import security_privileges, ThreadTokenRestoreError
from scripts.provision_sandbox import provision, restore_record

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows DACL/MIC recovery')


@pytest.fixture(autouse=True)
def recovery_diagnostics(monkeypatch):
    restore = SecurityTree.restore
    def diagnose(tree, records, **options):
        try:
            return restore(tree, records, **options)
        except ThreadTokenRestoreError:
            raise  # An unknown thread identity forbids even diagnostic ACL reads.
        except OSError as error:
            # Only ephemeral test descriptors; never log paths or raw user /
            # generated AppContainer SIDs. Retain ACE flags and MIC levels.
            def safe(sddl):
                return re.sub(r'S-1-[0-9-]+', lambda match:
                    'principal-' + hashlib.sha256(match[0].encode()).hexdigest()[:12], sddl)
            try:
                for expected, actual in zip(records, tree.snapshot()):
                    if descriptor_key(expected) != descriptor_key(actual):
                        error.add_note('Test ACL expected=' + safe(expected['sddl']) +
                                       ' actual=' + safe(actual['sddl']))
            except OSError:
                error.add_note('Test descriptor diagnostics unavailable')
            raise
    monkeypatch.setattr(SecurityTree, 'restore', diagnose)


def descriptor_key(record):
    return SecurityDescriptor.from_bytes(base64.b64decode(record['descriptor'], validate=True)).comparison_key()


def descriptor_keys(records):
    return [descriptor_key(record) for record in records]


def native_descriptor(path, information=0x1FF):
    """Independent Win32 query; never use the reconstructing GetSecurityInfo."""
    kernel = C.WinDLL('kernel32', use_last_error=True)
    advapi = C.WinDLL('advapi32', use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    open_file.restype = W.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [W.HANDLE], W.BOOL
    query = advapi.GetKernelObjectSecurity
    query.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    query.restype = W.BOOL
    needs_security = information not in (4 | 16,)
    access = 0x20000 | (0x1000000 if needs_security else 0)
    handle = open_file(str(path), access, 1 | 2 | 4, None, 3, 0x2200000, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        size = W.DWORD()
        assert not query(handle, information, None, 0, C.byref(size))
        assert C.get_last_error() == 122
        assert 20 <= size.value <= 1024 * 1024
        buffer = C.create_string_buffer(size.value)
        if not query(handle, information, buffer, len(buffer), C.byref(size)):
            raise C.WinError(C.get_last_error())
        return SecurityDescriptor.from_bytes(buffer.raw[:size.value])
    finally:
        if not close(handle):
            raise C.WinError(C.get_last_error())


def descriptors(roots):
    with SecurityTree(roots, writable=False) as tree:
        records = tree.snapshot()
        with security_privileges():
            for record in records:
                assert descriptor_key(record) == native_descriptor(record['path']).comparison_key()
        return descriptor_keys(records)


@pytest.fixture
def operator_privileges():
    context = security_privileges()
    try:
        context.__enter__()
    except PermissionError as error:
        if os.environ.get('GITHUB_ACTIONS', '').lower() == 'true':
            pytest.fail('Hosted ACL acceptance requires the native operator privileges: ' + str(error))
        pytest.skip('Native ACL management needs an authorized elevated operator token: ' + str(error))
    else:
        context.__exit__(None, None, None)


@pytest.fixture
def owned_tree(tmp_path_factory):
    workspace, runtime = tmp_path_factory / 'workspace', tmp_path_factory / 'runtime'
    workspace.mkdir(); runtime.mkdir()
    (workspace / 'nested').mkdir()
    (workspace / 'nested' / 'data.txt').write_text('workspace data', encoding='utf-8')
    (runtime / 'runtime.txt').write_text('runtime data', encoding='utf-8')
    return workspace, runtime, tmp_path_factory / 'host-recovery.json'


@pytest.fixture
def provisioning_tree(operator_privileges, owned_tree):
    workspace, runtime, record = owned_tree
    policy = SandboxPolicy(workspace)
    try:
        yield workspace, runtime, record
    finally:
        # Only a test-owned fresh profile; no shared user profile is deleted.
        from backend.core.sandbox_windows import WindowsApi
        api = WindowsApi()
        delete = api.userenv.DeleteAppContainerProfile
        delete.argtypes, delete.restype = [W.LPCWSTR], C.c_long
        status = delete(policy.profile_name)
        assert status >= 0 or status == -2147024894  # ERROR_FILE_NOT_FOUND


def test_preview_has_no_profile_acl_or_record_side_effects(owned_tree, monkeypatch):
    workspace, runtime, record = owned_tree
    targets = [workspace, workspace / 'nested', workspace / 'nested' / 'data.txt', runtime, runtime / 'runtime.txt']
    before = [native_descriptor(path, 4 | 16).comparison_key() for path in targets]
    def forbidden(*_, **__):
        raise AssertionError('Preview attempted a management mutation')
    monkeypatch.setattr('backend.core.sandbox_windows.WindowsApi.profile_sid', forbidden)
    monkeypatch.setattr('scripts.provision_sandbox.save_record', forbidden)
    monkeypatch.setattr(SecurityTree, '_write_descriptor', forbidden)
    provision(workspace, [runtime], record=record)
    assert not record.exists()
    assert [native_descriptor(path, 4 | 16).comparison_key() for path in targets] == before


@pytest.mark.parametrize('failure_index', [0, 2, 4])
@pytest.mark.parametrize('after_write', [False, True])
def test_partial_apply_restores_dacl_and_mic(provisioning_tree, monkeypatch, failure_index, after_write):
    workspace, runtime, record = provisioning_tree
    before = descriptors([workspace, runtime])
    write = SecurityTree._write_descriptor
    calls = []
    failed = False
    def fail(tree, handle, raw):
        nonlocal failed
        calls.append(handle)
        if not failed and len(calls) - 1 == failure_index:
            failed = True
            if after_write:
                write(tree, handle, raw)
            raise OSError('Injected native descriptor write failure')
        return write(tree, handle, raw)
    monkeypatch.setattr(SecurityTree, '_write_descriptor', fail)
    with pytest.raises(OSError, match='Injected native descriptor write failure'):
        provision(workspace, [runtime], record=record, apply=True)
    assert failed
    journal = json.loads(record.read_text(encoding='utf-8'))
    assert journal['phase'] == 'rolled_back' and journal['version'] == 3
    assert len(journal['records']) == len(journal['planned']) == 5
    assert descriptor_keys(journal['records']) != descriptor_keys(journal['planned'])
    assert descriptors([workspace, runtime]) == before
    assert (workspace / 'nested' / 'data.txt').read_text() == 'workspace data'


def test_apply_and_explicit_restore_preserve_protected_child_and_medium_label(provisioning_tree):
    workspace, runtime, record = provisioning_tree
    editor = str(windows_system_executable('System32/icacls.exe'))
    subprocess.run([editor, str(workspace / 'nested'), '/inheritance:d'], check=True, capture_output=True)
    subprocess.run([editor, str(workspace), '/setintegritylevel', '(OI)(CI)M'], check=True, capture_output=True)
    before = descriptors([runtime, workspace])
    provision(workspace, [runtime], record=record, apply=True)
    applied = descriptors([runtime, workspace])
    assert applied != before
    restore_record(record)  # readonly preview
    assert descriptors([runtime, workspace]) == applied
    restore_record(record, apply=True)
    assert descriptors([runtime, workspace]) == before
    assert json.loads(record.read_text(encoding='utf-8'))['phase'] == 'restored'
    with pytest.raises(ValueError, match='already recovered'):
        restore_record(record, apply=True)


@pytest.mark.parametrize('change', ['add', 'replace'])
def test_changed_tree_refuses_restore_before_any_acl_mutation(provisioning_tree, change):
    workspace, runtime, record = provisioning_tree
    provision(workspace, [runtime], record=record, apply=True)
    if change == 'add':
        (workspace / 'new.txt').write_text('new data')
    else:
        replacement = workspace / 'replacement.txt'
        replacement.write_text('replacement data')
        os.replace(replacement, workspace / 'nested' / 'data.txt')
    before = descriptors([runtime, workspace])
    with pytest.raises(ValueError, match='tree changed'):
        restore_record(record, apply=True)
    assert descriptors([runtime, workspace]) == before
    assert json.loads(record.read_text(encoding='utf-8'))['phase'] == 'applied'


@pytest.mark.parametrize('kind', ['junction', 'hardlink'])
def test_redirected_objects_refused_before_record_or_acl_changes(provisioning_tree, tmp_path_factory, kind):
    workspace, runtime, record = provisioning_tree
    outside = tmp_path_factory / 'outside'
    outside.mkdir(); (outside / 'secret').write_text('Host data')
    before = descriptors([outside])
    if kind == 'junction':
        subprocess.run([str(windows_system_executable('System32/cmd.exe')), '/d', '/c',
                        'mklink', '/J', str(workspace / 'alias'), str(outside)], check=True, capture_output=True)
    else:
        os.link(outside / 'secret', workspace / 'alias')
    with pytest.raises(OSError, match='reparse|multiply-linked'):
        provision(workspace, [runtime], record=record, apply=True)
    assert not record.exists()
    if kind == 'junction':
        (workspace / 'alias').rmdir()
    else:
        (workspace / 'alias').unlink()
    assert descriptors([outside]) == before


def test_exposed_or_reused_record_rejected_before_changes(provisioning_tree):
    workspace, runtime, record = provisioning_tree
    before = descriptors([workspace, runtime])
    for exposed in (workspace / 'recovery.json', runtime / 'recovery.json'):
        with pytest.raises(ValueError, match='outside workspace'):
            provision(workspace, [runtime], record=exposed, apply=True)
        assert not exposed.exists()
    record.write_text('existing private record')
    with pytest.raises(FileExistsError):
        provision(workspace, [runtime], record=record, apply=True)
    assert record.read_text() == 'existing private record'
    assert descriptors([workspace, runtime]) == before


def test_invalid_later_descriptor_cannot_partially_restore(provisioning_tree):
    workspace, runtime, _ = provisioning_tree
    with SecurityTree([workspace, runtime]) as tree:
        original = tree.snapshot()
        damaged = [dict(r) for r in original]
        first = SecurityDescriptor.from_bytes(base64.b64decode(original[0]['descriptor']))
        changed = replace(first, control=first.control ^ 0x1000).to_bytes()
        damaged[0]['descriptor'] = base64.b64encode(changed).decode('ascii')
        damaged[0]['sddl'] = tree._text(changed)
        damaged[-1]['descriptor'] = 'not valid base64!'
        with pytest.raises(ValueError):
            tree.restore(damaged, expected=original)
        assert tree.snapshot() == original


def sddl_bytes(text):
    advapi = C.WinDLL('advapi32', use_last_error=True)
    kernel = C.WinDLL('kernel32', use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)]
    convert.restype = W.BOOL
    free = kernel.LocalFree
    free.argtypes, free.restype = [C.c_void_p], C.c_void_p
    pointer, size = C.c_void_p(), W.DWORD()
    if not convert(text, 1, C.byref(pointer), C.byref(size)):
        raise C.WinError(C.get_last_error())
    try:
        return C.string_at(pointer, size.value)
    finally:
        free(pointer)


def test_security_comparison_never_equates_authority_or_inheritance():
    key = lambda text: SecurityDescriptor.from_bytes(sddl_bytes(text)).comparison_key()
    for left, right in [
        ('D:NO_ACCESS_CONTROL', 'D:'),
        ('D:P(A;;FA;;;SY)', 'D:(A;;FA;;;SY)'),
        ('D:AI(A;;FA;;;SY)', 'D:(A;;FA;;;SY)'),
        ('D:(A;ID;FA;;;SY)', 'D:(A;;FA;;;SY)'),
        ('D:(A;;FR;;;SY)', 'D:(A;;FA;;;SY)'),
        ('D:S:(ML;;NW;;;LW)', 'D:S:(ML;;NW;;;ME)'),
        ('O:SYG:BAD:', 'O:BAG:BAD:'),
        ('D:S:(AU;SA;FR;;;WD)', 'D:S:(AU;FA;FR;;;WD)'),
    ]:
        assert key(left) != key(right)


def test_concurrent_new_object_reports_incomplete_rollback_and_keeps_record(provisioning_tree, monkeypatch):
    workspace, runtime, record = provisioning_tree
    before = descriptors([runtime, workspace])
    write = SecurityTree._write_descriptor
    calls = []
    def fail(tree, handle, raw):
        calls.append(handle)
        if len(calls) == 2:
            (workspace / 'concurrent.txt').write_text('concurrent data')
            raise OSError('Injected concurrent inventory change')
        return write(tree, handle, raw)
    with monkeypatch.context() as patch:
        patch.setattr(SecurityTree, '_write_descriptor', fail)
        with pytest.raises(RuntimeError, match='rollback is incomplete'):
            provision(workspace, [runtime], record=record, apply=True)
    assert json.loads(record.read_text(encoding='utf-8'))['phase'] == 'rollback_failed'
    assert (workspace / 'concurrent.txt').read_text() == 'concurrent data'
    (workspace / 'concurrent.txt').unlink()  # test operator resolves the identity mismatch
    restore_record(record, apply=True)
    assert descriptors([runtime, workspace]) == before


def test_held_parent_and_object_handles_prevent_replacement(provisioning_tree):
    workspace, runtime, _ = provisioning_tree
    with SecurityTree([workspace, runtime]):
        for path in (workspace.parent, workspace, workspace / 'nested' / 'data.txt'):
            with pytest.raises(PermissionError):
                path.rename(path.with_name(path.name + '-replaced'))
    assert (workspace / 'nested' / 'data.txt').read_text() == 'workspace data'


@pytest.mark.parametrize('protected', [False, True])
@pytest.mark.parametrize('change_protection', [False, True])
def test_restore_explicit_owner_rights_without_adding_parent_permissions(
        provisioning_tree, protected, change_protection):
    workspace, runtime, _ = provisioning_tree
    with SecurityTree([workspace, runtime]) as tree:
        def write_acl(sddl, protection):
            descriptor, dacl = C.c_void_p(), C.c_void_p()
            present, defaulted = W.BOOL(), W.BOOL()
            tree.check(tree.from_sddl(sddl, 1, C.byref(descriptor), None))
            try:
                tree.check(tree.dacl(descriptor, C.byref(present), C.byref(dacl), C.byref(defaulted)))
                for handle in tree.handles.values():
                    status = tree.set_security(handle, 1, 4 | protection, None, None, dacl, None)
                    assert status == 0
                    # Establish the explicit hosted-runner OWNER_RIGHTS ACL
                    # after the native protection transition adds inheritance.
                    assert tree.set_security(handle, 1, 4, None, None, dacl, None) == 0
            finally:
                tree.free(descriptor)
        original_acl = 'D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;OW)'
        write_acl(original_acl, 0x80000000 if protected else 0x20000000)
        original = tree.snapshot()
        assert all('(A;;FA;;;OW)' in r['sddl'] for r in original)
        assert all(bool(SecurityDescriptor.from_bytes(base64.b64decode(r['descriptor'])).control & 0x1000) == protected
                   for r in original)
        modified_protection = not protected if change_protection else protected
        write_acl(original_acl + '(A;;FR;;;WD)',
                  0x80000000 if modified_protection else 0x20000000)
        modified = tree.snapshot()
        assert descriptor_keys(modified) != descriptor_keys(original)
        tree.restore(original, expected=modified)
        assert descriptor_keys(tree.snapshot()) == descriptor_keys(original)


@pytest.mark.parametrize('medium', [False, True])
def test_restore_explicit_children_without_recalculating_parent_inheritance(
        provisioning_tree, medium):
    workspace, runtime, _ = provisioning_tree
    editor = str(windows_system_executable('System32/icacls.exe'))
    with SecurityTree([workspace, runtime]) as tree:
        # Represent both naturally inherited and legacy explicit child ACLs.
        # They authorize the same identities but have different ACE flags.
        for path, handle in tree.handles.items():
            inherited = path == str(workspace)
            flags = 'OICI' if inherited else ''
            text = 'D:' + ('P' if inherited else '') + ''.join(
                f'(A;{flags};FA;;;{sid})' for sid in ('SY', 'BA', 'OW'))
            desc = C.c_void_p()
            tree.check(tree.from_sddl(text, 1, C.byref(desc), None))
            try:
                assert tree.set_object_security(handle, 4, desc) == 0
            finally:
                tree.free(desc)
        if medium:
            subprocess.run([editor, str(workspace), '/setintegritylevel', '(OI)(CI)M'],
                           check=True, capture_output=True)
        # LABEL propagation may have recalculated child ACEs; establish one
        # explicit child as found in legacy/Python-created directory trees.
        child = workspace / 'nested' / 'data.txt'
        desc = C.c_void_p()
        text = 'D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;OW)'
        tree.check(tree.from_sddl(text, 1, C.byref(desc), None))
        try:
            assert tree.set_object_security(tree.handles[str(child)], 4, desc) == 0
        finally:
            tree.free(desc)
        original = tree.snapshot()
        child_record = next(r for r in original if r['path'] == str(child))
        assert descriptor_key(child_record)[-1] == SecurityDescriptor.from_bytes(sddl_bytes(text)).comparison_key()[-1]
        subprocess.run([editor, str(workspace), '/grant', '*S-1-1-0:(OI)(CI)R'],
                       check=True, capture_output=True)
        subprocess.run([editor, str(workspace), '/setintegritylevel', '(OI)(CI)L'],
                       check=True, capture_output=True)
        tree.restore(original, expected=tree.snapshot())
        assert descriptor_keys(tree.snapshot()) == descriptor_keys(original)


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_partial_security_records_cannot_authorize_restore(owned_tree, monkeypatch, version):
    workspace, runtime, record = owned_tree
    record.write_text(json.dumps({'version': version, 'phase': 'applied',
        'workspace': str(workspace), 'runtimes': [str(runtime)], 'records': []}), encoding='utf-8')
    def forbidden(*_, **__):
        raise AssertionError('Legacy recovery must be rejected before opening a security tree')
    monkeypatch.setattr(SecurityTree, '__init__', forbidden)
    with pytest.raises(ValueError, match='invalid|supported|legacy|version'):
        restore_record(record, apply=True)


def test_snapshot_and_roundtrip_include_owner_group_audit_and_mic(provisioning_tree):
    workspace, runtime, _ = provisioning_tree
    with SecurityTree([workspace, runtime]) as tree:
        original = tree.snapshot()
        path = str(workspace / 'nested' / 'data.txt')
        baseline = SecurityDescriptor.from_bytes(base64.b64decode(
            next(record for record in original if record['path'] == path)['descriptor']))
        extras = SecurityDescriptor.from_bytes(sddl_bytes('O:BAG:SYS:(AU;SAFA;FR;;;WD)(ML;;NW;;;ME)'))
        target = replace(baseline, owner=extras.owner, group=extras.group,
                         sacl=extras.sacl, control=baseline.control | 0x10)
        tree._write_descriptor(tree.handles[path], target.to_bytes())
        modified = tree.snapshot()
        stored = next(record for record in modified if record['path'] == path)
        assert descriptor_key(stored) == target.comparison_key()
        with security_privileges():
            # The Win32 query and BACKUP information selector independently
            # corroborate the production NtQuery snapshot of all components.
            assert native_descriptor(path).comparison_key() == target.comparison_key()
            assert native_descriptor(path, 0x10000).comparison_key() == target.comparison_key()
        observed = SecurityDescriptor.from_bytes(base64.b64decode(stored['descriptor']))
        assert observed.owner == extras.owner and observed.group == extras.group
        assert observed.sacl == extras.sacl
        tree.restore(original, expected=modified)
        assert descriptor_keys(tree.snapshot()) == descriptor_keys(original)
        with security_privileges():
            for record in original:
                assert native_descriptor(record['path']).comparison_key() == descriptor_key(record)
    assert (workspace / 'nested' / 'data.txt').read_text() == 'workspace data'


@pytest.mark.parametrize('kind', ['dacl', 'owner', 'group', 'audit', 'mic'])
def test_external_security_change_refuses_restore_before_any_write(provisioning_tree, monkeypatch, kind):
    workspace, runtime, record = provisioning_tree
    provision(workspace, [runtime], record=record, apply=True)
    before = descriptors([runtime, workspace])
    # Apply a legitimate external management change with a separate Win32 API,
    # not the production recovery writer or its planned descriptor path.
    changes = {
        'dacl': (4, 'D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;OW)(A;;FR;;;WD)'),
        'owner': (1, 'O:SY'),
        'group': (2, 'G:SY'),
        'audit': (8 | 16, 'S:(AU;SAFA;FR;;;WD)(ML;;NW;;;LW)'),
        'mic': (16, 'S:(ML;;NW;;;ME)'),
    }
    information, text = changes[kind]
    with SecurityTree([workspace, runtime]) as tree, security_privileges():
        current = SecurityDescriptor.from_bytes(base64.b64decode(
            next(item for item in tree.snapshot() if item['path'] == str(workspace / 'nested' / 'data.txt'))['descriptor']))
        if kind == 'owner' and current.owner == SecurityDescriptor.from_bytes(sddl_bytes(text)).owner:
            text = 'O:BA'
        if kind == 'group' and current.group == SecurityDescriptor.from_bytes(sddl_bytes(text)).group:
            text = 'G:BA'
        change = C.create_string_buffer(sddl_bytes(text))
        setter = tree.advapi.SetKernelObjectSecurity
        setter.argtypes, setter.restype = [W.HANDLE, W.DWORD, C.c_void_p], W.BOOL
        tree.check(setter(tree.handles[str(workspace / 'nested' / 'data.txt')], information, change))
    drifted = descriptors([runtime, workspace])
    assert drifted != before
    def forbidden(*_, **__):
        raise AssertionError('Conflict must be found across the complete inventory before any overwrite')
    monkeypatch.setattr(SecurityTree, '_write_descriptor', forbidden)
    with pytest.raises((SecurityConflict, ValueError), match='conflict'):
        restore_record(record, apply=True)
    assert descriptors([runtime, workspace]) == drifted
    assert json.loads(record.read_text(encoding='utf-8'))['phase'] == 'applied'


def test_fatal_thread_restore_failure_does_not_begin_acl_rollback(provisioning_tree, monkeypatch):
    from scripts import provision_sandbox as operator
    workspace, runtime, record = provisioning_tree
    before = descriptors([runtime, workspace])
    native_write = SecurityTree._write_descriptor
    native_read = SecurityTree._security_bytes
    native_restore = SecurityTree.restore
    save = operator.save_record
    stopped = False
    writes, restores, phases = [], [], []

    def fatal_after_write(tree, handle, raw):
        nonlocal stopped
        assert not stopped, 'Fatal token restoration failure must stop subsequent ACL writes'
        native_write(tree, handle, raw)
        writes.append(handle)
        stopped = True
        # Inject the classified boundary failure after one real write. Never
        # deliberately leave pytest itself impersonating a privileged identity.
        raise ThreadTokenRestoreError(5, 'Injected fatal thread token restoration failure')

    def read_until_fatal(tree, handle):
        assert not stopped, 'Fatal token restoration failure must stop ACL reads and diagnostics'
        return native_read(tree, handle)

    def restore_once(tree, records, **options):
        restores.append(options)
        assert not stopped, 'Fatal token restoration failure must not begin rollback'
        return native_restore(tree, records, **options)

    def save_until_fatal(path, payload, **options):
        assert not stopped, 'Fatal token restoration failure must retain the durable applying record'
        phases.append(payload['phase'])
        return save(path, payload, **options)

    with monkeypatch.context() as patch:
        patch.setattr(SecurityTree, '_write_descriptor', fatal_after_write)
        patch.setattr(SecurityTree, '_security_bytes', read_until_fatal)
        patch.setattr(SecurityTree, 'restore', restore_once)
        patch.setattr(operator, 'save_record', save_until_fatal)
        with pytest.raises(ThreadTokenRestoreError, match='Injected fatal'):
            provision(workspace, [runtime], record=record, apply=True)
    assert len(writes) == len(restores) == 1
    assert phases == ['prepared', 'applying']
    journal = json.loads(record.read_text(encoding='utf-8'))
    assert journal['phase'] == 'applying'
    assert descriptor_keys(journal['records']) != descriptor_keys(journal['planned'])
    assert descriptors([runtime, workspace]) != before
    # A new explicit recovery invocation can inspect this exact mixed state.
    restore_record(record, apply=True)
    assert descriptors([runtime, workspace]) == before
