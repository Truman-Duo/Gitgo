from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core.executable_identity import windows_system_executable
from backend.core.sandbox import SandboxPolicy
from backend.core.windows_acl import SecurityTree, canonical_security
from scripts.provision_sandbox import provision, restore_record

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows DACL/MIC recovery')


def descriptors(roots):
    with SecurityTree(roots, writable=False) as tree:
        return [canonical_security(r['sddl']) for r in tree.snapshot()]


@pytest.fixture
def provisioning_tree(tmp_path_factory):
    workspace, runtime = tmp_path_factory / 'workspace', tmp_path_factory / 'runtime'
    workspace.mkdir(); runtime.mkdir()
    (workspace / 'nested').mkdir()
    (workspace / 'nested' / 'data.txt').write_text('workspace data', encoding='utf-8')
    (runtime / 'runtime.txt').write_text('runtime data', encoding='utf-8')
    record = tmp_path_factory / 'host-recovery.json'
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


def test_preview_has_no_profile_acl_or_record_side_effects(provisioning_tree, monkeypatch):
    workspace, runtime, record = provisioning_tree
    before = descriptors([workspace, runtime])
    def forbidden(*_):
        raise AssertionError('Preview attempted profile creation')
    monkeypatch.setattr('backend.core.sandbox_windows.WindowsApi.profile_sid', forbidden)
    provision(workspace, [runtime], record=record)
    assert not record.exists()
    assert descriptors([workspace, runtime]) == before


@pytest.mark.parametrize('failure_index', [0, 1, 2])
def test_partial_apply_restores_dacl_and_mic(provisioning_tree, failure_index):
    workspace, runtime, record = provisioning_tree
    before = descriptors([workspace, runtime])
    calls = []
    def fail(command, **options):
        calls.append(command)
        if len(calls) - 1 == failure_index:
            if failure_index == 2:
                subprocess.run(command, **options)  # label changed before reported failure
            raise subprocess.CalledProcessError(5, command)
        return subprocess.run(command, **options)
    with pytest.raises(subprocess.CalledProcessError):
        provision(workspace, [runtime], record=record, apply=True, run=fail)
    assert len(calls) == failure_index + 1
    assert json.loads(record.read_text(encoding='utf-8'))['phase'] == 'rolled_back'
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
        damaged[0]['sddl'] = 'D:(A;;FA;;;WD)'
        damaged[-1]['sddl'] = 'not a security descriptor'
        with pytest.raises(OSError):
            tree.restore(damaged)
        assert tree.snapshot() == original


def test_security_comparison_never_equates_null_and_empty_dacl():
    assert canonical_security('D:NO_ACCESS_CONTROLS:') != canonical_security('D:S:')
    assert canonical_security('D:P(A;;FA;;;SY)') != canonical_security('D:(A;;FA;;;SY)')
    assert canonical_security('D:(A;;FR;;;SY)') != canonical_security('D:(A;;FA;;;SY)')
    assert canonical_security('D:S:(ML;;NW;;;LW)') != canonical_security('D:S:(ML;;NW;;;ME)')


def test_concurrent_new_object_reports_incomplete_rollback_and_keeps_record(provisioning_tree):
    workspace, runtime, record = provisioning_tree
    before = descriptors([runtime, workspace])
    calls = []
    def fail(command, **options):
        calls.append(command)
        if len(calls) == 2:
            (workspace / 'concurrent.txt').write_text('concurrent data')
            raise subprocess.CalledProcessError(5, command)
        return subprocess.run(command, **options)
    with pytest.raises(RuntimeError, match='rollback is incomplete'):
        provision(workspace, [runtime], record=record, apply=True, run=fail)
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
        assert all(bool('D:P' in r['sddl']) == protected for r in original)
        modified_protection = not protected if change_protection else protected
        write_acl(original_acl + '(A;;FR;;;WD)',
                  0x80000000 if modified_protection else 0x20000000)
        assert [canonical_security(r['sddl']) for r in tree.snapshot()] != [
            canonical_security(r['sddl']) for r in original]
        tree.restore(original)
        assert [canonical_security(r['sddl']) for r in tree.snapshot()] == [
            canonical_security(r['sddl']) for r in original]
