from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.render_product_manifest import render_manifest, render_shell_contract
from scripts.prepare_linux_sqlite import SQLITE_SHA3_256, SQLITE_URL, SQLITE_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_linux_release_pins_a_verified_wal_safe_sqlite_source():
    version = tuple(int(part) for part in SQLITE_VERSION.split("."))

    assert version >= (3, 51, 3)
    assert SQLITE_URL.startswith("https://www.sqlite.org/")
    assert len(SQLITE_SHA3_256) == 64
    assert set(SQLITE_SHA3_256) <= set("0123456789abcdef")


def test_linux_build_and_release_entrypoints_are_fail_closed():
    build = (ROOT / "packaging" / "build_linux.sh").read_text(encoding="utf-8")
    release = (ROOT / "packaging" / "release_linux.sh").read_text(encoding="utf-8")

    assert '"$(uname -s)" != "Linux"' in build
    assert "cli/dashboard/src/main.tsx" in build
    assert "backend/core/native_host_entry.py" in build
    assert "smoke_packaged_runtime.py" in build
    assert "sha256sum" in build
    assert "verify_release_privacy.py" in release
    assert "-m pytest tests -q" in release
    assert "git push" not in release
    assert "--allow-dirty" in release


def test_linux_uninstaller_preserves_xdg_runtime_and_project_state_by_contract():
    uninstall = (ROOT / "packaging" / "linux" / "uninstall.sh").read_text(
        encoding="utf-8",
    )

    assert "credential-cleanup" in uninstall
    assert "XDG runtime databases and every project directory are always preserved" in uninstall
    assert 'rm -rf "$INSTALL_ROOT"' in uninstall
    assert "XDG_STATE_HOME" not in uninstall
    assert ".gitgo/project-id" not in uninstall


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux installer contract")
def test_linux_install_upgrade_and_safe_uninstall(tmp_path_factory):
    home = tmp_path_factory / "home"
    home.mkdir()
    data_home = tmp_path_factory / "data"
    state_home = tmp_path_factory / "state"
    release = tmp_path_factory / "linux-x86_64"
    host_dir = release / "internal" / "gitgo-host"
    host_dir.mkdir(parents=True)

    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
    if architecture in {"amd64"}:
        architecture = "x86_64"
    elif architecture in {"arm64"}:
        architecture = "aarch64"
    manifest = render_manifest(ROOT / "packaging" / "product.json", "linux", architecture)
    (release / "product.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    (release / "product.env").write_text(
        render_shell_contract(manifest), encoding="utf-8",
    )
    for path in (release / "gitgo", host_dir / "gitgo-host"):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    for name in ("install.sh", "uninstall.sh"):
        shutil.copy2(ROOT / "packaging" / "linux" / name, release / name)
        (release / name).chmod(0o755)
    shutil.copy2(ROOT / "packaging/unix/installer-common.sh", release / "installer-common.sh")

    environment = {
        **{
            key: value for key, value in os.environ.items()
            if not key.startswith("GITGO_") and not key.startswith("XDG_")
        },
        "HOME": str(home),
        "SHELL": "/bin/bash",
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    subprocess.run([str(release / "install.sh")], env=environment, check=True)
    subprocess.run([str(release / "install.sh")], env=environment, check=True)

    install_root = data_home / "gitgo" / "app"
    command = home / ".local" / "bin" / "gitgo"
    assert command.is_symlink()
    assert command.readlink() == install_root / "gitgo"
    profile = (home / ".profile").read_text(encoding="utf-8")
    assert profile.count("# >>> Gitgo managed PATH >>>") == 1

    config_root = home / ".gitgo"
    config_root.mkdir()
    for name in ("config.json", "commit-config.json", "llm_config.json", "provider_secrets.json"):
        (config_root / name).write_text("{}", encoding="utf-8")
    runtime_state = state_home / "gitgo" / "projects" / "keep.db"
    runtime_state.parent.mkdir(parents=True)
    runtime_state.write_text("keep", encoding="utf-8")
    project_metadata = tmp_path_factory / "project" / ".gitgo" / "project-id"
    project_metadata.parent.mkdir(parents=True)
    project_metadata.write_text("keep", encoding="utf-8")

    subprocess.run([str(install_root / "uninstall.sh")], env=environment, check=True)

    assert not install_root.exists()
    assert not command.exists()
    assert runtime_state.read_text(encoding="utf-8") == "keep"
    assert project_metadata.read_text(encoding="utf-8") == "keep"
    assert not (config_root / "provider_secrets.json").exists()
    assert "Gitgo managed PATH" not in (home / ".profile").read_text(encoding="utf-8")
