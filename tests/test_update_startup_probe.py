"""Real source Host startup in isolated profiles; optional frozen artifact proof."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.core.update.health import isolated_environment, probe_startup_host, frozen_health_check
from backend.core.update.validation import UpdateError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Managed updater ownership is POSIX-only")


@pytest.mark.parametrize("fault", [None, "existing-profile", "outside-config"])
def test_real_source_host_probe_has_no_project_or_provider_side_effects(tmp_path_factory, fault):
    base = tmp_path_factory.resolve()
    profile = base / "profile"; profile.mkdir(mode=0o700)
    outside = base / "user-config.json"; outside.write_bytes(b"user data must be preserved")
    env = {**isolated_environment(profile), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    if fault == "existing-profile": (profile / "user-data").write_bytes(b"keep")
    if fault == "outside-config": env["GITGO_CONFIG_PATH"] = str(outside)
    result = subprocess.run([sys.executable, "-B", "-m", "backend.core.native_host_entry",
        "--gitgo-internal-role", "update-startup-probe", "--nonce", "e" * 32], cwd=profile,
        env=env, capture_output=True, text=True, timeout=30)
    assert outside.read_bytes() == b"user data must be preserved"
    if fault:
        assert result.returncode != 0
        assert "native_host_check" not in result.stdout
        if fault == "existing-profile": assert (profile / "user-data").read_bytes() == b"keep"
    else:
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["native_host_check"] is True and report["memory_sqlite_check"] is True
        assert report["nonce"] == "e" * 32
        assert not (profile / "providers.json").exists() and not (profile / "secrets.json").exists()


@pytest.mark.skipif(not os.environ.get("GITGO_MANAGED_TEST_STAGE"), reason="Requires a fresh actual frozen bundle")
def test_actual_frozen_startup_still_refuses_incomplete_platform_cleanup(tmp_path_factory):
    stage = Path(os.environ["GITGO_MANAGED_TEST_STAGE"]).resolve(strict=True)
    base = tmp_path_factory.resolve()
    profile = base / "probe"; profile.mkdir(mode=0o700)
    report = probe_startup_host(stage / "internal/gitgo-host/gitgo-host", profile)
    assert report["runtime"]["native_host_check"] is True
    second = base / "health"; second.mkdir(mode=0o700)
    if report["cleanup"]["complete"] is not True:
        with pytest.raises(UpdateError, match="UPDATE_HEALTH_FAILED"): frozen_health_check(stage, second)
    else:
        assert frozen_health_check(stage, second).passed is True
