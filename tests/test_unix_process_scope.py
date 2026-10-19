"""Owned scope safety tests; commands/sentinels are disposable test children."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from backend.core.process_control import spawn_managed, terminate_tree
from backend.core.process_output import collect_output

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix session supervision")


def launch(code, **kwargs):
    return spawn_managed([sys.executable, "-c", code], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)


def collect(proc, timeout=5):
    return collect_output(proc, timeout=timeout, limit=4096, terminate=lambda: terminate_tree(proc))


def test_owned_scope_preserves_stdin_unicode_output_and_exit_status():
    proc = launch("import sys; print(sys.stdin.read()); print('diagnostic',file=sys.stderr); sys.exit(7)")
    out, err, _ = collect_output(proc, timeout=5, input="中文", limit=4096,
                               terminate=lambda: terminate_tree(proc))
    assert out.strip() == "中文" and err.strip() == "diagnostic"
    assert proc.returncode == 7
    assert proc._gitgo_scope_socket is None
    if sys.platform == "darwin":
        assert proc._gitgo_cleanup["complete"] is False
        assert "detached_descendants_not_covered" in proc._gitgo_cleanup["issues"]


def test_missing_command_is_not_a_fake_success():
    with pytest.raises(FileNotFoundError):
        spawn_managed(["/nonexistent-gitgo-test-command"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_root_exit_cannot_leave_its_attached_child_running(tmp_path_factory):
    marker = tmp_path_factory / "survived"
    child = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(1); pathlib.Path(%r).write_text('bad')" % str(marker)
    proc = launch(f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])")
    collect(proc)
    time.sleep(1.1)
    assert not marker.exists()


def test_cancellation_keeps_unrelated_sentinel_alive_without_ps(monkeypatch):
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    proc = launch("import time; time.sleep(10)")
    try:
        monkeypatch.setattr(os, "getpgid", lambda _: (_ for _ in ()).throw(AssertionError("PID snapshot authority forbidden")))
        terminate_tree(proc)
        assert sentinel.poll() is None
        assert proc.poll() is not None
    finally:
        sentinel.terminate(); sentinel.wait(timeout=3)


def test_unmanaged_root_never_promotes_a_reused_pid_to_group_authority(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    monkeypatch.setattr(os, "killpg", lambda *_: (_ for _ in ()).throw(AssertionError("unowned group")))
    terminate_tree(proc)
    assert proc._gitgo_cleanup["issues"] == ["unmanaged_root_only"]


def test_owning_runner_disconnect_cleans_its_scope(tmp_path_factory):
    marker = tmp_path_factory / "survived"
    proc = launch(f"import time,pathlib; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('bad')")
    proc._gitgo_scope_socket.close()
    proc._gitgo_scope_socket = None
    proc.wait(timeout=5)
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native adoption/pidfd required")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_linux_nested_setsid_double_fork_and_parent_exit_are_owned(tmp_path_factory, parent_exits):
    marker = tmp_path_factory / "escaped"
    code = f'''import os,time,pathlib
pid=os.fork()
if pid == 0:
    os.setsid()
    pid=os.fork()
    if pid == 0:
        time.sleep(1)
        pathlib.Path({str(marker)!r}).write_text('bad')
    os._exit(0)
if not {parent_exits!r}: time.sleep(10)
'''
    proc = launch(code)
    if not parent_exits:
        time.sleep(.1); terminate_tree(proc)
    else:
        collect(proc)
    assert proc._gitgo_cleanup["mode"] == "linux-subreaper-pidfd"
    assert proc._gitgo_cleanup["complete"] is True
    time.sleep(1.1)
    assert not marker.exists()


def test_mac_detached_child_is_not_misreported_as_complete(tmp_path_factory):
    if sys.platform != "darwin": pytest.skip("macOS explicit coverage boundary")
    marker = tmp_path_factory / "detached"
    child = f"import os,time,pathlib; os.setsid(); time.sleep(.5); pathlib.Path({str(marker)!r}).write_text('observed')"
    # Detached process closes all inherited pipes and ends itself promptly.
    proc = launch(f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(.1)")
    collect(proc)
    assert not proc._gitgo_cleanup["complete"]
    time.sleep(.6)
    assert marker.exists()  # declared limitation, never silently counted as clean


@pytest.mark.parametrize("cancel", [False, True])
def test_frozen_host_scope_role_preserves_command_semantics(monkeypatch, cancel):
    host = os.environ.get("GITGO_SCOPE_TEST_HOST")
    if not host: pytest.skip("Fresh frozen Host required")
    source_python = sys.executable
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", host)
    code = "import time; time.sleep(10)" if cancel else "print('frozen-scope-ok'); raise SystemExit(7)"
    proc = spawn_managed([source_python, "-c", code],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if cancel:
        terminate_tree(proc)
        assert proc.poll() is not None
        expected_mode = "linux-subreaper-pidfd" if sys.platform.startswith("linux") else "owned-session-only"
        assert proc._gitgo_cleanup["mode"] == expected_mode
        return
    out, err, _ = collect(proc, timeout=10)
    assert proc.returncode == 7
    assert out.strip() == "frozen-scope-ok"
    assert not err


def test_pidfd_cleanup_rejects_changed_parent_without_group_kill(monkeypatch):
    from backend.core import unix_process_scope as scope
    sent, closed = [], []
    monkeypatch.setattr(os, "pidfd_open", lambda _: 123456, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: sent.append(args), raising=False)
    monkeypatch.setattr(Path, "read_text", lambda _: f"123 (test) S {os.getpid()+1} 0")
    assert scope.signal_adopted(123) is False
    assert not sent and closed == [123456]


def test_pidfd_permission_failure_does_not_widen_authority(monkeypatch):
    from backend.core import unix_process_scope as scope
    closed = []
    monkeypatch.setattr(os, "pidfd_open", lambda _: 123456, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(Path, "read_text", lambda _: f"123 (test) S {os.getpid()} 0")
    def denied(*args): raise PermissionError("test denied")
    monkeypatch.setattr(signal, "pidfd_send_signal", denied, raising=False)
    with pytest.raises(PermissionError): scope.signal_adopted(123)
    assert closed == [123456]
