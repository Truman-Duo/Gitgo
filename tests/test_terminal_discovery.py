from pathlib import Path
import json
import os
import re
import subprocess

import pytest

from backend.core.executable_identity import file_identity, verify_git_bash, verify_windows_terminal
from backend.core.terminal_launcher import terminal_inventory, resolve_executable
from backend.core.application import ApplicationServices, OperationError
from backend.core.config import ConfigManager


def images(root):
    files = [root / "git-bash.exe", root / "bin/bash.exe", root / "usr/bin/bash.exe"]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZtest-image")
    return files


def signed(paths, publisher="Johannes Schindelin", status="Valid"):
    return [{"path": str(p), "status": status, "subject": f"CN={publisher}, O=publisher",
             "thumbprint": "same-certificate", "original_filename": "WindowsTerminal.exe"} for p in paths]


def fixture_package(root):
    return {"verified": True, "files": [file_identity(p) for p in
            (root / "git-bash.exe", root / "bin/bash.exe", root / "usr/bin/bash.exe")],
            "provenance": {"release": "test-fixture"}}


@pytest.mark.parametrize("publisher,status", [("Johannes Schindelin", "NotSigned"), ("Microsoft Corporation", "Valid"), ("Johannes Schindelin", "HashMismatch")])
def test_fake_bash_never_reaches_probe(tmp_path_factory, publisher, status):
    root = tmp_path_factory / "Fake Git"
    images(root)
    calls = []
    result = verify_git_bash(root, signature_reader=lambda p: signed(p, publisher, status),
                             probe_runner=lambda *a, **k: calls.append(a))
    assert not result["verified"]
    assert result["code"] == "TERMINAL_IDENTITY_UNVERIFIED"
    assert not calls


def test_verified_bash_requires_behavior_and_unchanged_images(tmp_path_factory):
    root = tmp_path_factory / "Git"
    files = images(root)
    def probe(argv, **kwargs):
        assert argv[1:4] == ["--noprofile", "--norc", "-c"]
        nonce = re.search(r"gitgo:([a-f0-9]+):", argv[4])[1]
        return subprocess.CompletedProcess(argv, 0, f"gitgo:{nonce}:5.2.37(1)-release\n", "")
    result = verify_git_bash(root, signature_reader=signed, probe_runner=probe, package_verifier=fixture_package)
    assert result["verified"]
    assert len(result["files"]) == 3
    def replacing_probe(argv, **kwargs):
        result = probe(argv, **kwargs)
        files[2].write_bytes(b"MZreplacement")
        return result
    assert not verify_git_bash(root, signature_reader=signed, probe_runner=replacing_probe, package_verifier=fixture_package)["verified"]


def test_windows_terminal_cannot_be_a_renamed_signed_application(tmp_path_factory):
    path = tmp_path_factory / "wt.exe"
    path.write_bytes(b"MZsample")
    def signatures(paths):
        rows = signed(paths, "Microsoft Corporation")
        rows[0]["original_filename"] = "Cmd.Exe"
        return rows
    result = verify_windows_terminal(path, "windows_terminal", signature_reader=signatures)
    assert not result["verified"]
    assert "not a Windows Terminal" in result["message"]


def test_access_time_is_not_content_identity(tmp_path_factory):
    path = tmp_path_factory / "sample.exe"
    path.write_bytes(b"MZsample")
    before = file_identity(path)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns + 1000000000, stat.st_mtime_ns))
    assert file_identity(path) == before


def test_auto_discovery_populates_verified_option_without_saved_path(tmp_path_factory):
    root = tmp_path_factory / "Installed Git"
    files = images(root)
    env = {"PATH": "", "ProgramFiles": str(tmp_path_factory)}
    def verifier(root):
        return {"verified": True, "code": "VERIFIED_GIT_BASH", "files": [file_identity(p) for p in files],
                "provenance": {"release": "test-fixture"}}
    inventory = terminal_inventory(platform="win32", environment=env, registered_paths=lambda _: [],
                                    package_paths=[], git_roots=[root], git_verifier=verifier, windows_build=19045)
    assert inventory["configured"] is False
    assert inventory["selected"] == "auto"
    assert inventory["effective"]["id"] == "git_bash"
    assert inventory["effective"]["command"] == str(root / "usr/bin/mintty.exe")
    assert inventory["effective"]["args"][:2] == ["--pcon", "on"]
    legacy = terminal_inventory(platform="win32", environment=env, registered_paths=lambda _: [],
        package_paths=[], git_roots=[root], git_verifier=verifier, windows_build=14393)
    assert legacy["effective"]["args"][:2] == ["--pcon", "off"]
    assert legacy["effective"]["args"][-1] == (root / "usr/bin/winpty.exe").as_posix()
    assert legacy["warnings"]
    missing = terminal_inventory({"terminal": "alacritty"}, platform="win32", environment={"PATH": ""},
                                  registered_paths=lambda _: [], package_paths=[], git_roots=[])
    assert missing["configured"] is True  # legacy explicit setting
    assert missing["effective"]["id"] == "current"
    assert missing["warnings"]


def test_cwd_and_relative_path_entries_are_not_detection_sources(tmp_path_factory, monkeypatch):
    monkeypatch.chdir(tmp_path_factory)
    (tmp_path_factory / "git.exe").write_bytes(b"MZfake")
    assert not resolve_executable("git.exe", {"PATH": os.pathsep.join(["", ".", str(tmp_path_factory)])})


def test_rejected_bash_produces_public_warning_not_available_choice(tmp_path_factory):
    root = tmp_path_factory / "Fake Git"
    images(root)
    inventory = terminal_inventory(platform="win32", environment={"PATH": ""}, registered_paths=lambda _: [],
        package_paths=[], git_roots=[root], git_verifier=lambda _: {
            "verified": False, "code": "TERMINAL_IDENTITY_UNVERIFIED", "message": "Fake publisher"})
    assert not any(c["id"] == "git_bash" for c in inventory["options"])
    assert "Fake publisher" in inventory["warnings"][0]


def test_service_saves_id_and_host_discovered_command_atomically(tmp_path_factory, monkeypatch):
    import backend.core.terminal_launcher as module
    path = tmp_path_factory / "detected.exe"
    path.write_bytes(b"MZsample")
    options = [{"id": "git_bash", "label": "Git Bash", "available": True, "command": str(path), "args": ["verified-args"]}]
    monkeypatch.setattr(module, "terminal_inventory", lambda _: {"options": options, "warnings": []})
    service = ApplicationServices()
    service.config_set("launcher.terminal", "git_bash")
    launcher = ConfigManager.load().launcher
    assert launcher["configured"] is True
    assert launcher["command"] == str(path)
    assert launcher["args"] == ["verified-args"]
    options[0]["available"] = False
    with pytest.raises(OperationError) as error:
        service.config_set("launcher.terminal", "git_bash")
    assert error.value.code == "TERMINAL_UNAVAILABLE"
    assert ConfigManager.load().launcher == launcher


def test_explicit_fake_bash_override_is_rejected_by_identity_resolver(tmp_path_factory, monkeypatch):
    from backend.core.tools import workspace_tools
    from backend.core import executable_identity
    root = tmp_path_factory / "fake"
    images(root)
    monkeypatch.setenv("GITGO_BASH_PATH", str(root / "bin/bash.exe"))
    monkeypatch.setattr(workspace_tools.sys, "platform", "win32")
    monkeypatch.setattr(executable_identity, "verify_git_bash", lambda _: {
        "verified": False, "message": "invalid publisher"})
    result = workspace_tools._resolve_bash()
    assert result["error"] == "BASH_IDENTITY_UNVERIFIED"
    assert "invalid publisher" in result["detail"]
