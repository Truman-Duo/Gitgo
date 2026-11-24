"""Exercise the exact standalone import closure without launchd or root."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core import sandbox_macos as mac

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='System Python source service on macOS')


@pytest.mark.parametrize('omitted', ['macos_coalition', 'macos_file_authority'])
def test_source_service_imports_all_local_dependencies_in_isolated_mode(tmp_path_factory, omitted):
    command = mac.stage_source_service(tmp_path_factory)
    assert command == [mac.identity.PYTHON, '-I',
                       str(tmp_path_factory/'macos_execution_service.py'), str(tmp_path_factory)]
    result = subprocess.run([command[0], '-B', '-I', '-c',
        'import sys,json;sys.path.insert(0,sys.argv[1]);import macos_execution_service;'
        'print(json.dumps({n:sys.modules[n].__file__ for n in '
        '("macos_execution_service","macos_identity","macos_resource_guard","macos_coalition","macos_file_authority")}))',
        str(tmp_path_factory)], cwd='/', env={'PATH':'/usr/bin:/bin'},
        capture_output=True, text=True, check=True, timeout=10)
    for path in json.loads(result.stdout).values():
        assert Path(path).parent == tmp_path_factory
        assert Path(path).stat().st_mode & 0o777 == 0o500
    # The previous three-module staging really fails under the same -I entry,
    # rather than accidentally importing the missing file from the checkout.
    (tmp_path_factory/(omitted+'.py')).unlink()
    old = subprocess.run([command[0], '-B', '-I', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);import macos_execution_service', str(tmp_path_factory)],
        cwd='/', env={'PATH':'/usr/bin:/bin'}, capture_output=True, text=True, timeout=10)
    assert old.returncode != 0 and ("No module named '"+omitted+"'") in old.stderr


@pytest.mark.parametrize('omitted', ['macos_coalition.py', 'macos_file_authority.py'])
def test_failed_import_preflight_never_registers_a_service(tmp_path_factory, monkeypatch, omitted):
    real = mac.subprocess.run
    calls = []
    def fail_import(command, **options):
        calls.append(command)
        (tmp_path_factory/omitted).unlink()
        return real(command, **options)
    monkeypatch.setattr(mac.subprocess, 'run', fail_import)
    with pytest.raises(subprocess.CalledProcessError): mac.stage_source_service(tmp_path_factory)
    assert len(calls) == 1 and calls[0][0] == mac.identity.PYTHON
    assert not (tmp_path_factory/'state.json').exists()
    assert not (tmp_path_factory/'job.plist').exists()
