"""Real macOS single-process probe restriction, not full application isolation."""
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest

from backend.core.process_control import spawn_managed, terminate_tree
from backend.core.process_output import collect_output


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin sandbox interface")


def interpreter():
    # A framework's bin/python is itself a posix_spawn trampoline. Use the
    # executable image directly so the test does not need child creation.
    framework = sysconfig.get_config_var("PYTHONFRAMEWORKINSTALLDIR")
    if framework:
        candidate = Path(framework) / "Versions" / f"{sys.version_info.major}.{sys.version_info.minor}" / "Resources/Python.app/Contents/MacOS/Python"
        if candidate.is_file():
            return str(candidate.resolve())
    return sys.executable


def launch(code):
    return spawn_managed([interpreter(), "-c", code], macos_forkless=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def collect(proc):
    return collect_output(proc, timeout=10, limit=4096,
                          terminate=lambda: terminate_tree(proc))


def test_forkless_probe_preserves_output_and_exit_code():
    proc = launch("import sys; print('中文'); sys.exit(7)")
    out, err, _ = collect(proc)
    assert proc.returncode == 7 and out.strip() == "中文" and not err
    assert proc._gitgo_cleanup == {"mode": "macos-forkless-sandbox", "complete": True, "issues": []}


def test_exec_does_not_remove_kernel_restrictions():
    child = "import os; os.fork()"
    proc = launch(f"import os; os.execve({interpreter()!r}, [{interpreter()!r}, '-c', {child!r}], os.environ)")
    _, err, _ = collect(proc)
    assert proc.returncode != 0 and "PermissionError" in err
    assert proc._gitgo_cleanup["complete"] is True


@pytest.mark.parametrize("operation", ["os.fork()", "os.setsid()", "os.setpgid(0, 0)",
    "os.posix_spawn('/bin/true', ['/bin/true'], os.environ)",
    "os.posix_spawn('/bin/true', ['/bin/true'], os.environ, setpgroup=0)"])
def test_kernel_rejects_fork_and_direct_detachment(operation):
    proc = launch(f"""import os
try:
    result = {operation}
except PermissionError:
    print('restricted')
else:
    if result == 0 and {operation.startswith('os.fork')!r}: os._exit(9)
    raise SystemExit('restriction was bypassed')
""")
    out, _, _ = collect(proc)
    assert proc.returncode == 0 and out.strip() == "restricted"
    assert proc._gitgo_cleanup["complete"] is True


@pytest.mark.parametrize("flags", [0x0040, 0x0400, 0x0002])
def test_posix_spawn_attributes_do_not_bypass_restriction(flags):
    # SETEXEC, SETSID and SETPGROUP must be denied too. Just denying setsid()
    # would leave the kernel's internal posix_spawn attribute path unconfined.
    code = f"""import ctypes, errno, os
libc = ctypes.CDLL(None)
attr = ctypes.c_void_p()
assert libc.posix_spawnattr_init(ctypes.byref(attr)) == 0
assert libc.posix_spawnattr_setflags(ctypes.byref(attr), {flags}) == 0
argv = (ctypes.c_char_p * 2)(b'/bin/true', None)
env = (ctypes.c_char_p * 1)(None)
pid = ctypes.c_int()
result = libc.posix_spawn(ctypes.byref(pid), b'/bin/true', None, ctypes.byref(attr), argv, env)
libc.posix_spawnattr_destroy(ctypes.byref(attr))
if result == 0:
    os.waitpid(pid.value, 0)
assert result == errno.EPERM, result
print('restricted')
"""
    proc = launch(code)
    out, _, _ = collect(proc)
    assert proc.returncode == 0 and out.strip() == "restricted"
    assert proc._gitgo_cleanup["complete"] is True


def test_cancellation_reaps_the_restricted_child_and_keeps_unrelated_process():
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    proc = launch("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)")
    try:
        terminate_tree(proc)
        assert proc.poll() is not None and proc._gitgo_cleanup["complete"] is True
        assert sentinel.poll() is None
    finally:
        sentinel.terminate(); sentinel.wait(timeout=5)


def test_invalid_forkless_request_is_not_silently_enabled():
    with pytest.raises(ValueError):
        spawn_managed(["/bin/true"], macos_forkless="true")


def test_forkless_capability_cannot_be_requested_on_other_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError):
        spawn_managed(["/bin/true"], macos_forkless=True)


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_frozen_supervisor_enforces_the_forkless_role(monkeypatch, cancel):
    host = os.environ.get("GITGO_SCOPE_TEST_HOST")
    if not host:
        pytest.skip("Requires a fresh actual frozen Host")
    python = interpreter()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", host)
    code = "import time; time.sleep(30)" if cancel else "import os; os.fork()"
    proc = spawn_managed([python, "-c", code], macos_forkless=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if cancel:
        terminate_tree(proc)
    else:
        _, err, _ = collect(proc)
        assert proc.returncode != 0 and "PermissionError" in err
    assert proc._gitgo_cleanup == {"mode": "macos-forkless-sandbox", "complete": True, "issues": []}
