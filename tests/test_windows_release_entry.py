"""Release CLI guards; all cases stop before builds, keys or user state."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not POWERSHELL, reason="Windows release entry point")


def invoke(*arguments):
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / "packaging/release_windows.ps1"), *arguments], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.parametrize("arguments,message", [
    (["-BuildOnly", "-VerifyOnly"], "Choose -BuildOnly or -VerifyOnly"),
    (["-Sign", "-BuildOnly", "-ManagedUpdate"], "Signing requires a clean managed release"),
    (["-Sign", "-VerifyOnly", "-ManagedUpdate"], "Signing requires a clean managed release"),
    (["-Sign", "-AllowDirty", "-ManagedUpdate"], "Signing requires a clean managed release"),
    (["-Sign"], "Signing requires a clean managed release"),
    (["-ManagedUpdate", "-BuildInstaller"], "Managed releases use the complete ZIP"),
])
def test_release_modes_do_not_sign_unvalidated_or_mismatched_packages(arguments, message):
    result = invoke(*arguments)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr


@pytest.mark.parametrize("field", ["Sign", "AllowDirty", "BuildOnly"])
def test_local_settings_cannot_enable_signing_or_omit_validation(tmp_path_factory, field):
    path = tmp_path_factory / "release-settings.json"
    path.write_text(json.dumps({"schema_version": 1, field: "true"}), encoding="utf-8")
    result = invoke("-SettingsFile", str(path))
    assert result.returncode != 0
    assert "Unknown or invalid release setting" in result.stdout + result.stderr


def test_boolean_settings_schema_is_not_a_version(tmp_path_factory):
    path = tmp_path_factory / "release-settings.json"
    path.write_text(json.dumps({"schema_version": True}), encoding="utf-8")
    result = invoke("-SettingsFile", str(path))
    assert result.returncode != 0
    assert "Unsupported release settings schema" in result.stdout + result.stderr
