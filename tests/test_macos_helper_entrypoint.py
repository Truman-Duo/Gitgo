"""The operator helper entry must not bypass per-user managed admission."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX package entrypoint")
SCRIPT = Path(__file__).resolve().parents[1] / "packaging/macos/sandbox-helper.sh"


def entry(tmp_path, *, managed=False, name="release package", marker_kind="file"):
    root = tmp_path / name
    host = root / "internal/gitgo-host/gitgo-host"
    host.parent.mkdir(parents=True)
    host.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GITGO_HELPER_TEST_RECORD"\n')
    host.chmod(0o755)
    shutil.copyfile(SCRIPT, root / "sandbox-helper.sh")
    # An adjacent script is neither needed nor executed by the fixed role.
    (root / "manage_macos_sandbox_service.py").write_text("raise AssertionError('external script executed')")
    if managed:
        control = root.parent / "control"
        control.mkdir()
        marker = control / "registration.json"
        if marker_kind == "link": marker.symlink_to(control / "missing")
        elif marker_kind == "directory": marker.mkdir()
        else: marker.write_text("{}")
    record = tmp_path / "host-invocation.txt"
    result = subprocess.run(["/bin/bash", str(root / "sandbox-helper.sh"), "install",
        "--name", "fixture-service", "--bundle", str(root / "internal/gitgo-host"),
        "--project", str(tmp_path / "project with spaces"), "--allow-local-adhoc"],
        env={"PATH": "/usr/bin:/bin", "GITGO_HELPER_TEST_RECORD": str(record)},
        capture_output=True, text=True, timeout=5)
    return root, record, result


@pytest.mark.parametrize("marker_kind", ["file", "link", "directory"])
def test_managed_active_entry_is_rejected_before_running_host(tmp_path_factory, marker_kind):
    _, record, result = entry(tmp_path_factory, managed=True, name="active", marker_kind=marker_kind)
    assert result.returncode == 2
    assert "requires the extracted release package" in result.stderr
    assert not record.exists()


@pytest.mark.parametrize("name", ["release package", "active"])
def test_release_entry_preserves_explicit_operator_arguments(tmp_path_factory, name):
    root, record, result = entry(tmp_path_factory, name=name)
    assert result.returncode == 0, result.stderr
    assert record.read_text().splitlines() == ["--gitgo-internal-role", "macos-helper-admin",
        "install", "--name", "fixture-service",
        "--bundle", str(root / "internal/gitgo-host"), "--project", str(tmp_path_factory / "project with spaces"),
        "--allow-local-adhoc"]
