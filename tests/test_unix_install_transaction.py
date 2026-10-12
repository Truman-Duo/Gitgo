"""Real installer shell transactions with synthetic payloads, not frozen UI acceptance."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.render_product_manifest import render_manifest, render_shell_contract

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["linux", "macos"])
def setup_install(request, tmp_path_factory):
    platform = request.param
    if os.name == "nt" or (platform == "macos" and sys.platform != "darwin"):
        pytest.skip("Unix shell/macOS plutil required")
    root = tmp_path_factory.resolve()
    home, release = root / "home", root / "release"
    home.mkdir()
    release.mkdir()
    architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
    if platform == "linux":
        architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(architecture, architecture)
    manifest = render_manifest(ROOT / "packaging/product.json", platform, architecture)
    manifest["command_aliases"] = ["gitgo-preview"]
    (release / "product.json").write_text(json.dumps(manifest))
    (release / "product.env").write_text(render_shell_contract(manifest))
    for name in ["gitgo", "internal/gitgo-host/gitgo-host"]:
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    (release / "gitgo-preview").symlink_to("gitgo")
    shutil.copy2(ROOT / f"packaging/{platform}/install.sh", release / "install.sh")
    shutil.copy2(ROOT / "packaging/unix/installer-common.sh", release / "installer-common.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITGO_") and not k.startswith("XDG_")}
    env.update(HOME=str(home), SHELL="/bin/zsh")
    return {"root": root, "home": home, "release": release, "env": env,
            "target": home / "application", "bin": home / "bin", "profile": home / ".zprofile"}


def invoke(s, target=None, bin_dir=None):
    return subprocess.run(["/bin/bash", str(s["release"] / "install.sh"),
        "--install-root", str(target or s["target"]), "--bin-dir", str(bin_dir or s["bin"])],
        env=s["env"], capture_output=True, text=True, timeout=15)


def assert_clean(s):
    assert not list(s["home"].rglob(".gitgo-transaction.*"))
    assert not list(s["home"].rglob(".gitgo-profile.*"))
    assert not list(s["home"].rglob(".gitgo-install.lock"))


def test_install_then_owned_upgrade_preserves_profile_and_commands(setup_install):
    s = setup_install
    s["profile"].write_text("# personal settings\n")
    assert invoke(s).returncode == 0
    before = s["profile"].read_bytes()
    (s["release"] / "new-version").write_text("new payload")
    assert invoke(s).returncode == 0
    assert s["profile"].read_bytes() == before
    assert (s["target"] / "new-version").exists()
    assert (s["bin"] / "gitgo-preview").readlink() == s["target"] / "gitgo-preview"
    assert_clean(s)


@pytest.mark.parametrize("kind", ["ordinary", "empty", "forged-product", "wrong-owner"])
def test_unknown_directory_is_never_replaced(setup_install, kind):
    s = setup_install
    s["target"].mkdir()
    if kind != "empty":
        (s["target"] / "sentinel").write_text("keep")
    if kind in ("forged-product", "wrong-owner"):
        shutil.copy2(s["release"] / "product.json", s["target"] / "product.json")
    if kind == "wrong-owner":
        (s["target"] / ".gitgo-install-owner").write_text("not-owned")
    before = sorted(p.name for p in s["target"].iterdir())
    result = invoke(s)
    assert result.returncode != 0 and "ownership" in result.stderr
    assert sorted(p.name for p in s["target"].iterdir()) == before
    assert not s["bin"].exists()
    assert_clean(s)


@pytest.mark.parametrize("kind", ["symlink-parent", "symlink-target", "home-spelling", "inside-source", "contains-source", "bin-in-target", "symlink-bin", "symlink-profile"])
def test_redirected_and_overlapping_targets_are_rejected(setup_install, kind):
    s = setup_install
    target, bin_dir = s["target"], s["bin"]
    if kind == "symlink-parent":
        (s["home"] / "redirect").symlink_to(s["root"], target_is_directory=True)
        target = s["home"] / "redirect/app"
    elif kind == "symlink-target":
        target.symlink_to(s["release"], target_is_directory=True)
    elif kind == "home-spelling": target = str(s["home"]) + "///."
    elif kind == "inside-source": target = s["release"] / "app"
    elif kind == "contains-source": target = s["root"]
    elif kind == "bin-in-target": bin_dir = target / "bin"
    elif kind == "symlink-bin": bin_dir.symlink_to(s["root"], target_is_directory=True)
    elif kind == "symlink-profile": s["profile"].symlink_to(s["release"] / "sentinel")
    (s["release"] / "sentinel").write_text("keep")
    assert invoke(s, target, bin_dir).returncode != 0
    assert (s["release"] / "sentinel").read_text() == "keep"
    if kind == "symlink-profile":
        assert s["profile"].is_symlink()
        assert s["profile"].readlink() == s["release"] / "sentinel"
    else:
        assert not s["profile"].exists()


@pytest.mark.parametrize("kind", ["link", "profile", "interrupt"])
def test_failed_first_install_does_not_leave_new_profile_or_commands(setup_install, kind):
    s = setup_install
    faults(s, kind)
    assert invoke(s).returncode != 0
    assert not s["target"].exists()
    assert not s["profile"].exists()
    assert not (s["bin"] / "gitgo").is_symlink()
    assert_clean(s)


def test_command_and_profile_conflicts_are_preflighted(setup_install):
    s = setup_install
    s["bin"].mkdir()
    (s["bin"] / "gitgo-preview").write_text("user command")
    assert invoke(s).returncode != 0
    assert not s["target"].exists()
    assert (s["bin"] / "gitgo-preview").read_text() == "user command"
    (s["bin"] / "gitgo-preview").unlink()
    s["profile"].write_text("# >>> Gitgo managed PATH >>>\nuser content\n")
    assert invoke(s).returncode != 0
    assert not s["target"].exists()
    assert "user content" in s["profile"].read_text()


def test_existing_lock_is_not_removed(setup_install):
    s = setup_install
    lock = s["home"] / ".gitgo-install.lock"
    lock.mkdir()
    (lock / "sentinel").write_text("keep")
    assert invoke(s).returncode != 0
    assert (lock / "sentinel").read_text() == "keep"
    assert not s["target"].exists()


def test_release_cannot_redirect_the_generated_ownership_record(setup_install):
    s = setup_install
    sentinel = s["home"] / "sentinel"
    sentinel.write_text("keep")
    (s["release"] / ".gitgo-install-owner").symlink_to(sentinel)
    assert invoke(s).returncode != 0
    assert sentinel.read_text() == "keep"
    assert not s["target"].exists()


def faults(s, kind):
    wrappers = s["root"] / "fault-tools"
    wrappers.mkdir()
    for command in ("cp", "ln", "mv"):
        real = shutil.which(command)
        script = wrappers / command
        script.write_text("#!/bin/bash\n" + f"kind={kind!r}\n" + r'''
case "$kind:$0:$*" in
  copy:*cp:*release/.*) exit 71 ;;
  link:*ln:*gitgo-preview*) exit 72 ;;
  publish:*mv:*payload*) exit 73 ;;
  profile:*mv:*.gitgo-profile.*) exit 74 ;;
  profile-write:*cp:*.zprofile*) exit 76 ;;
  interrupt:*ln:*gitgo-preview*) kill -TERM "$PPID"; exit 75 ;;
esac
''' + f'exec "{real}" "$@"\n')
        script.chmod(0o755)
    s["env"]["PATH"] = str(wrappers) + os.pathsep + s["env"]["PATH"]


@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("kind", ["copy", "link", "publish", "profile", "profile-write", "interrupt"])
def test_failure_and_interrupt_restore_files_links_and_profile(setup_install, upgrade, kind):
    s = setup_install
    s["profile"].write_text("# keep my settings\n")
    if upgrade:
        # Old owned package without the new alias makes link creation testable.
        manifest = json.loads((s["release"] / "product.json").read_text())
        manifest["command_aliases"] = []
        (s["release"] / "product.json").write_text(json.dumps(manifest))
        (s["release"] / "product.env").write_text(render_shell_contract(manifest))
        assert invoke(s).returncode == 0
        (s["target"] / "old-version").write_text("preserve previous package")
        manifest["command_aliases"] = ["gitgo-preview"]
        (s["release"] / "product.json").write_text(json.dumps(manifest))
        (s["release"] / "product.env").write_text(render_shell_contract(manifest))
    before = s["profile"].read_bytes()
    faults(s, kind)
    result = invoke(s)
    assert result.returncode != 0, result.stdout
    assert s["profile"].read_bytes() == before
    assert not (s["bin"] / "gitgo-preview").exists()
    assert not (s["bin"] / "gitgo-preview").is_symlink()
    if upgrade:
        assert (s["target"] / "old-version").read_text() == "preserve previous package"
        assert (s["bin"] / "gitgo").readlink() == s["target"] / "gitgo"
    else:
        assert not s["target"].exists()
        assert not (s["bin"] / "gitgo").is_symlink()
    assert_clean(s)
