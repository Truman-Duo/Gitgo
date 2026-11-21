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


@pytest.mark.parametrize("streams", ["all", "stdout", "stderr", "split", "split-stderr", "input"])
def test_detached_session_forwards_terminal_resize_as_sigwinch(tmp_path_factory, streams):
    """A detached session never receives the terminal's SIGWINCH directly.

    The supervisor must observe the pty size itself and forward SIGWINCH to its
    own group, otherwise interactive payloads (the Dashboard) never reflow when
    the window is resized. Reproduce with a real pty; no signal is sent to the
    payload group by the test itself.
    """
    import fcntl
    import pty
    import struct
    import termios
    import json
    from backend.core.process_control import finish_scope
    marker = tmp_path_factory / "resized.jsonl"
    ready = tmp_path_factory / "ready"
    master, slave = pty.openpty()
    input_master, input_slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    fcntl.ioctl(input_slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
    target = 0 if streams == "input" else 2 if streams in {"stderr", "split-stderr"} else 1
    code = (
        "import json,os,pathlib,signal,sys,time\n"
        "def on_winch(*_):\n"
        "    size=os.get_terminal_size(%r)\n"
        "    with open(%r,'a') as f: f.write(json.dumps([size.lines,size.columns])+'\\n')\n"
        "    if (size.lines,size.columns)==(30,120): raise SystemExit(7)\n"
        "signal.signal(signal.SIGWINCH, on_winch)\n"
        "print('stdout 中文',flush=True)\n"
        "print('stderr 中文',file=sys.stderr,flush=True)\n"
        "pathlib.Path(%r).write_text('ready')\n"
        "time.sleep(20)\n"
    ) % (target, str(marker), str(ready))
    stdin = slave if streams in {"all", "input"} else input_slave if streams.startswith("split") else subprocess.DEVNULL
    stdout = slave if streams in {"all", "stdout", "split"} else subprocess.PIPE
    stderr = slave if streams in {"all", "stderr", "split-stderr"} else subprocess.PIPE
    proc = None
    try:
        proc = spawn_managed([sys.executable, "-c", code], stdin=stdin, stdout=stdout, stderr=stderr,
                             text=True, encoding="utf-8")
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        if streams.startswith("split"):
            # Input geometry must not override the terminal rendering output.
            fcntl.ioctl(input_master, termios.TIOCSWINSZ, struct.pack("HHHH", 7, 33, 0, 0))
            time.sleep(0.35)
            assert not marker.exists(), "Input-only resize was forwarded to the output UI"
        # Resize via TIOCSWINSZ only. The kernel would signal the pty's
        # foreground group, which is NOT this detached session.
        for rows, columns in ((12, 50), (30, 120)):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            expected = json.dumps([rows, columns])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if marker.exists() and expected in marker.read_text():
                    break
                time.sleep(0.02)
            assert marker.exists() and expected in marker.read_text()
        proc.wait(timeout=5)
        finish_scope(proc)
        assert proc.returncode == 7
        assert [json.loads(line) for line in marker.read_text().splitlines()] == [[12, 50], [30, 120]]
        for pipe, expected in ((proc.stdout, "stdout 中文"), (proc.stderr, "stderr 中文")):
            if pipe is not None:
                assert pipe.read().strip() == expected
    finally:
        if proc is not None:
            terminate_tree(proc)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None: pipe.close()
        os.close(master)
        os.close(slave)
        os.close(input_master)
        os.close(input_slave)


def test_terminal_monitor_private_copy_survives_closed_stdin():
    import pty
    from backend.core.process_control import finish_scope
    master, slave = pty.openpty()
    code = ("import os,sys; from backend.core.unix_process_scope import terminal_monitor; "
            "os.close(0); fd,size=terminal_monitor(); "
            "assert fd>=3 and not os.get_inheritable(fd); os.close(fd); "
            "print('private-monitor',file=sys.stderr)")
    proc = None
    try:
        proc = spawn_managed([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                             stdout=slave, stderr=subprocess.PIPE, text=True)
        proc.wait(timeout=5)
        finish_scope(proc)
        assert proc.returncode == 0 and proc.stderr.read().strip() == "private-monitor"
    finally:
        if proc is not None:
            terminate_tree(proc)
            if proc.stderr is not None: proc.stderr.close()
        os.close(master)
        os.close(slave)


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
