from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.render_product_manifest import render_manifest


ROOT = Path(__file__).resolve().parents[1]


def test_macos_build_and_release_entrypoints_are_fail_closed():
    build = (ROOT / "packaging" / "build_macos.sh").read_text(encoding="utf-8")
    release = (ROOT / "packaging" / "release_macos.sh").read_text(encoding="utf-8")

    assert "cli/dashboard/src/main.tsx" in build
    assert "backend/core/native_host_entry.py" in build
    assert "smoke_packaged_runtime.py" in build
    assert "codesign --verify" in build
    assert "shasum -a 256" in build
    assert "verify_release_privacy.py" in release
    assert "-m pytest tests -q" in release
    assert "git push" not in release
    assert "--allow-dirty" in release


def test_macos_uninstaller_preserves_runtime_and_project_state_by_contract():
    uninstall = (ROOT / "packaging" / "macos" / "uninstall.sh").read_text(
        encoding="utf-8",
    )

    assert "credential-cleanup" in uninstall
    assert "Runtime databases and every project directory are always preserved" in uninstall
    assert 'rm -rf "$INSTALL_ROOT"' in uninstall
    assert 'rm -rf "$HOME/Library/Application Support/Gitgo/state"' not in uninstall
    assert ".gitgo/project-id" not in uninstall


def test_dashboard_resolves_symlink_and_ignores_host_directory():
    entry = (ROOT / "cli" / "dashboard" / "src" / "main.tsx").read_text(
        encoding="utf-8",
    )

    assert "realpathSync(process.execPath)" in entry
    assert "statSync(candidate).isFile()" in entry
    nested = 'resolve(GITGO_DIR, "internal", "gitgo-host", name)'
    directory_collision = 'resolve(GITGO_DIR, "internal", name)'
    assert entry.index(nested) < entry.index(directory_collision)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS installer contract")
def test_macos_install_upgrade_and_safe_uninstall(tmp_path_factory):
    home = tmp_path_factory / "home"
    home.mkdir()
    release = tmp_path_factory / "macos-arm64"
    host_dir = release / "internal" / "gitgo-host"
    host_dir.mkdir(parents=True)

    manifest = render_manifest(
        ROOT / "packaging" / "product.json",
        "macos",
        subprocess.check_output(["uname", "-m"], text=True).strip(),
    )
    (release / "product.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    for path in (release / "gitgo", host_dir / "gitgo-host"):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    for name in ("install.sh", "uninstall.sh"):
        shutil.copy2(ROOT / "packaging" / "macos" / name, release / name)
        (release / name).chmod(0o755)
    shutil.copy2(ROOT / "packaging/unix/installer-common.sh", release / "installer-common.sh")

    environment = {
        **dict(os_environ_without_live_gitgo()),
        "HOME": str(home),
        "SHELL": "/bin/zsh",
    }
    subprocess.run([str(release / "install.sh")], env=environment, check=True)
    # An upgrade is an idempotent replacement, not a second PATH mutation.
    subprocess.run([str(release / "install.sh")], env=environment, check=True)

    install_root = home / "Library" / "Application Support" / "Gitgo" / "app"
    command = home / ".local" / "bin" / "gitgo"
    assert command.is_symlink()
    assert command.readlink() == install_root / "gitgo"
    profile = (home / ".zprofile").read_text(encoding="utf-8")
    assert profile.count("# >>> Gitgo managed PATH >>>") == 1

    config_root = home / ".gitgo"
    config_root.mkdir()
    for name in ("config.json", "commit-config.json", "llm_config.json", "provider_secrets.json"):
        (config_root / name).write_text("{}", encoding="utf-8")
    runtime_state = home / "Library" / "Application Support" / "Gitgo" / "state" / "keep.db"
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
    assert "Gitgo managed PATH" not in (home / ".zprofile").read_text(encoding="utf-8")


def os_environ_without_live_gitgo() -> dict[str, str]:
    import os

    return {
        key: value for key, value in os.environ.items()
        if not key.startswith("GITGO_")
    }
