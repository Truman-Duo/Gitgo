"""Pure macOS validation/report helpers must collect on every CI platform."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


MODULES = [
    'backend.core.macos_identity',
    'backend.core.macos_execution_service',
    'backend.core.sandbox_macos',
    'backend.core.macos_helper_lifecycle',
    'backend.core.macos_boot_recovery',
    'backend.core.macos_file_authority',
    'backend.core.macos_resource_guard',
    'scripts.check_macos_helper_entry',
    'scripts.check_macos_managed_dashboard',
    'scripts.macos_cross_version_update_probe',
    'scripts.check_macos_user_broker',
    'scripts.check_macos_resource_fixes',
    'scripts.check_macos_stability_concurrency',
    'scripts.check_macos_developer_tools',
    'scripts.sample_macos_trial_node',
    'scripts.retire_macos_unstarted_trial',
]


def test_validation_and_report_modules_import_in_an_isolated_interpreter():
    # Use the actual host platform. Native Windows CI exercises missing Unix
    # modules directly; do not substitute sys.platform or fake system APIs.
    root = Path(__file__).resolve().parents[1]
    code = ('import importlib,json,sys;sys.path.insert(0,sys.argv[1]);'
            'names=json.loads(sys.argv[2]);'
            '[importlib.import_module(name) for name in names];'
            'print(json.dumps(names))')
    result = subprocess.run([sys.executable, '-I', '-c', code, str(root), json.dumps(MODULES)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == MODULES


@pytest.mark.skipif(sys.platform == 'darwin', reason='Actual foreign-platform admission')
@pytest.mark.parametrize('entry', ['install', 'service', 'lifecycle', 'trial'])
def test_importability_does_not_enable_native_execution(tmp_path_factory, entry):
    root = tmp_path_factory / 'must-not-exist'
    if entry == 'install':
        from backend.core.macos_helper_install import install
        with pytest.raises(PermissionError):
            install(root, allow_local_adhoc=True)
    elif entry == 'service':
        from backend.core.macos_execution_service import serve
        with pytest.raises(RuntimeError, match='Invalid service entry'):
            serve(root)
    elif entry == 'lifecycle':
        from backend.core.macos_helper_lifecycle import require_admin
        with pytest.raises(PermissionError):
            require_admin()
    else:
        from backend.core.sandbox import SandboxDenied
        from backend.core.sandbox_macos import execution_trial
        with pytest.raises(SandboxDenied, match='administrator harness'):
            with execution_trial(root):
                pytest.fail('Foreign platform admitted a native task')
    assert not root.exists()
