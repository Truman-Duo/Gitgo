import shutil
import subprocess

from tests.test_unix_install_transaction import setup_install, invoke


def test_failed_credential_cleanup_keeps_app_index_links_and_path(setup_install):
    s = setup_install
    platform = s["release"].joinpath("product.env").read_text().split("GITGO_PRODUCT_PLATFORM=", 1)[1].splitlines()[0]
    from tests.test_unix_install_transaction import ROOT
    shutil.copy2(ROOT / f"packaging/{platform}/uninstall.sh", s["release"] / "uninstall.sh")
    host = s["release"] / "internal/gitgo-host/gitgo-host"
    host.write_text("#!/bin/sh\nexit 73\n")
    assert invoke(s).returncode == 0
    config = s["home"] / ".gitgo"
    config.mkdir()
    index = config / "provider_secrets.json"
    index.write_text('{"version":2,"secrets":{},"pending_cleanup":["test-opaque-reference"]}')
    before_index, before_profile = index.read_bytes(), s["profile"].read_bytes()
    command = ["/bin/bash", str(s["target"] / "uninstall.sh"), "--bin-dir", str(s["bin"])]
    result = subprocess.run(command, env=s["env"], capture_output=True)
    assert result.returncode == 73
    assert index.read_bytes() == before_index
    assert s["profile"].read_bytes() == before_profile
    assert s["target"].is_dir()
    assert (s["bin"] / "gitgo").is_symlink()
    # An explicit keep-config does not retry/erase credentials.
    result = subprocess.run(command + ["--keep-config"], env=s["env"], capture_output=True)
    assert result.returncode == 0
    assert index.read_bytes() == before_index
    assert not s["target"].exists()


def test_uninstall_can_be_retried_after_service_recovers(setup_install):
    s = setup_install
    platform = s["release"].joinpath("product.env").read_text().split("GITGO_PRODUCT_PLATFORM=", 1)[1].splitlines()[0]
    from tests.test_unix_install_transaction import ROOT
    shutil.copy2(ROOT / f"packaging/{platform}/uninstall.sh", s["release"] / "uninstall.sh")
    assert invoke(s).returncode == 0
    config = s["home"] / ".gitgo"
    config.mkdir()
    index = config / "provider_secrets.json"
    index.write_text('{}')
    host = s["target"] / "internal/gitgo-host/gitgo-host"
    host.write_text("#!/bin/sh\nexit 73\n")
    command = ["/bin/bash", str(s["target"] / "uninstall.sh"), "--bin-dir", str(s["bin"])]
    assert subprocess.run(command, env=s["env"], capture_output=True).returncode == 73
    host.write_text("#!/bin/sh\nexit 0\n")
    assert subprocess.run(command, env=s["env"], capture_output=True).returncode == 0
    assert not index.exists()
    assert not (s["bin"] / "gitgo").is_symlink()
