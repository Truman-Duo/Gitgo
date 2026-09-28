from pathlib import Path
import json
import subprocess
import sys

import pytest

from backend.core.protocol_io import dump_protocol_json
from scripts import sync_windows_runtime_packages as runtime_packages


def test_protocol_json_escapes_unpaired_surrogates_without_losing_unicode():
    value = {"message": "中文\udc80tail"}

    encoded = dump_protocol_json(value, separators=(",", ":"))

    assert encoded.isascii()
    assert "\\udc80" in encoded
    assert json.loads(encoded) == value
    assert encoded.encode("utf-8")


def test_frozen_children_use_private_host_roles(monkeypatch):
    from backend.core.child_process import daemon_command, python_command, tool_runner_command

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Gitgo\internal\gitgo-host.exe")
    daemon = daemon_command("demo", Path(r"C:\source"))
    runner = tool_runner_command()
    python = python_command(["-B", "-c", "print('ok')"])

    assert daemon[:3] == [
        r"C:\Gitgo\internal\gitgo-host.exe", "--gitgo-internal-role", "daemon",
    ]
    assert daemon[3:5] == ["--project", "demo"]
    assert runner == [
        r"C:\Gitgo\internal\gitgo-host.exe", "--gitgo-internal-role", "tool-runner",
    ]
    assert python == [
        r"C:\Gitgo\internal\gitgo-host.exe", "--gitgo-internal-role", "python",
        "-B", "-c", "print('ok')",
    ]


def test_terminal_product_manifest_has_one_public_command_and_safe_uninstall():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "packaging" / "product.json").read_text(encoding="utf-8"))
    assert manifest["primary_command"] == "gitgo"
    assert isinstance(manifest["command_aliases"], list)
    assert manifest["layout"]["native_host"].startswith("internal/")
    assert manifest["uninstall"]["remove_runtime_state"] is False
    assert manifest["uninstall"]["remove_project_directories"] is False
    assert manifest["uninstall"]["remove_project_gitgo_metadata"] is False


def test_terminal_build_pipeline_does_not_use_legacy_qt_entrypoint():
    root = Path(__file__).resolve().parents[1]
    script = (root / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "cli\\dashboard\\src\\main.tsx" in script
    assert "backend\\core\\native_host_entry.py" in script
    assert "frontend.gui_main" not in script
    assert '"$($product.primary_command).exe"' in script
    assert "smoke_packaged_runtime.py" in script


def test_release_entrypoint_is_fail_closed_and_never_pushes():
    root = Path(__file__).resolve().parents[1]
    script = (root / "packaging" / "release_windows.ps1").read_text(encoding="utf-8")
    assert "scripts\\verify_release_privacy.py" in script
    assert "-m pytest tests -q" in script
    assert '"Dashboard test suite"' in script
    assert '"Dashboard production build"' in script
    assert '"build_windows.ps1"' in script
    assert "git push" not in script
    assert "-AllowDirty" in script


def test_windows_installer_registers_command_without_owning_project_state():
    root = Path(__file__).resolve().parents[1]
    script = (root / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    assert "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths" in script
    assert "AddUserPathEntry" in script
    assert "RemoveUserPathEntry" in script
    assert "{userprofile}\\.gitgo\\config.json" in script
    assert "{userprofile}\\.gitgo\\provider_secrets.json" in script
    assert "project-local .gitgo/.git/gitgo metadata are never traversed" in script
    assert "Type: filesandordirs; Name: \"{userprofile}\\.gitgo\\state\"" not in script


def test_terminal_launcher_is_part_of_the_single_public_executable():
    root = Path(__file__).resolve().parents[1]
    entry = (root / "cli" / "dashboard" / "src" / "main.tsx").read_text(encoding="utf-8")
    launcher = (root / "cli" / "dashboard" / "src" / "backend" / "terminalLauncher.ts").read_text(encoding="utf-8")

    assert "shouldRelaunchInConfiguredTerminal" in entry
    assert "windowsParentProcessName" in entry
    assert 'resolve(EXECUTABLE_DIR, "product.json")' in entry
    assert '"--attached"' in launcher
    assert '"explorer"' in launcher
    assert '"windows_terminal"' in launcher
    assert "gitgo-host" not in launcher


def test_failed_package_install_removes_only_owned_staging_directory(
    tmp_path_factory: Path, monkeypatch,
):
    bootstrap = tmp_path_factory / "bootstrap.exe"
    bootstrap.write_bytes(b"")
    runtime = tmp_path_factory / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    requirements = tmp_path_factory / "requirements.txt"
    requirements.write_text("example==1\n", encoding="utf-8")
    active = runtime / "packages-existing"
    active.mkdir()

    monkeypatch.setattr(runtime_packages, "_probe", lambda *_args: "")

    def fail_install(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["pip"])

    monkeypatch.setattr(runtime_packages.subprocess, "run", fail_install)

    with pytest.raises(subprocess.CalledProcessError):
        runtime_packages.sync(bootstrap, runtime, requirements)

    assert active.is_dir()
    assert list(runtime.glob("packages.staging-*")) == []
