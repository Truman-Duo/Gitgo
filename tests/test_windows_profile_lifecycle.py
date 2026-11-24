"""Profile preparation and native launch have separate ownership."""
import os
import pytest

from tests.test_native_sandbox import isolated_python, native_box, finished

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer profile lifecycle")


def test_prepared_native_launch_derives_sid_without_creating_profile_again(native_box, monkeypatch):
    from backend.core.sandbox_windows import WindowsApi
    workspace, policy, spawn = native_box
    def forbidden(*args):
        pytest.fail("Native launch attempted to create/reopen the prepared profile")
    monkeypatch.setattr(WindowsApi, "profile_sid", forbidden)
    for _ in range(3):
        process = spawn("from pathlib import Path;Path('ready.txt').write_text('same authorized workspace');print('ran')")
        code, out, error = finished(process)
        assert code == 0 and out.strip() == "ran", error
        assert process.close_sandbox()["complete"] is True
    assert (workspace / "ready.txt").read_text() == "same authorized workspace"


def test_invalid_native_profile_has_no_profile_creation_or_user_token_fallback(native_box, monkeypatch):
    from backend.core.sandbox_windows import WindowsApi
    from backend.core.sandbox import SandboxDenied
    workspace, policy, spawn = native_box
    # Exercise the real Rust prelaunch validation. Patching the retired Python
    # SID callback no longer injects a failure into the native implementation.
    with monkeypatch.context() as failure:
        failure.setattr(type(policy), "profile_name", property(lambda self: 'invalid\0profile'))
        failure.setattr(WindowsApi, "profile_sid", lambda *args: pytest.fail("Profile creation was used as launch fallback"))
        with pytest.raises(SandboxDenied) as caught:
            spawn("from pathlib import Path;Path('must-not-run').write_text('bad')")
    assert caught.value.code == "SANDBOX_LAUNCH_DENIED"
    assert caught.value.effect_state == "not_committed" and not (workspace / "must-not-run").exists()
