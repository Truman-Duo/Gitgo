"""Concrete native causes retain independent codes and original evidence."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.windows_acl import SecurityTree, SecurityInventoryChanged, SecurityConflict
from backend.core.loop.error_taxonomy import classify_tool_error
from backend.core.sandbox import SandboxDenied


def test_directory_drift_has_its_own_code_path_and_changed_members(tmp_path_factory):
    root = tmp_path_factory / 'pinned'
    root.mkdir()
    tree = SecurityTree.__new__(SecurityTree)
    tree.handles = {str(root): 1}
    tree.children = {str(root): set()}
    tree._information = lambda _: None
    (root / 'new.txt').write_text('changed')
    with pytest.raises(SecurityInventoryChanged) as failure:
        tree.verify_inventory()
    assert failure.value.details['path'] == str(root)
    assert failure.value.details['added'] == [str(root / 'new.txt')]
    result = classify_tool_error(failure.value, 'read_file')
    assert result.code == 'SANDBOX_DIRECTORY_INVENTORY_CHANGED'
    assert classify_tool_error(SecurityConflict('descriptor drift')).code == 'SANDBOX_SECURITY_DESCRIPTOR_CONFLICT'


def test_structured_failure_preserves_stage_os_error_and_concrete_recovery():
    failure = SandboxDenied('PYTHON_PRIVATE_DIRECTORY_CREATE_DENIED', 'Windows rejected private mkdir',
        details={'phase':'create_private_directory', 'winerror':5, 'path':'owned-temp'})
    result = failure.result()
    assert result['error'] == 'PYTHON_PRIVATE_DIRECTORY_CREATE_DENIED'
    assert result['error_info']['details']['winerror'] == 5
    assert result['error_info']['details']['phase'] == 'create_private_directory'
    assert result['error_info']['recovery_class'] == 'inspect_exact_parent_scope_and_private_temp_preparation'
    assert classify_tool_error(failure).code == result['error']


def test_generic_access_denial_does_not_overwrite_a_typed_sandbox_failure():
    assert classify_tool_error(PermissionError('ordinary out-of-scope read')).code == 'RESOURCE_SCOPE_APPROVAL_REQUIRED'
    assert classify_tool_error(SandboxDenied('SANDBOX_HOME_PREPARATION_FAILED','private home')).code == 'SANDBOX_HOME_PREPARATION_FAILED'
