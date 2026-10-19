"""Actual Windows Job containment, no fake positive cleanup reports."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from backend.core.process_control import spawn_managed, finish_scope, terminate_tree
from backend.core.process_output import collect_output, OutputLimit

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows Job APIs required")


def run(code, *, input=None, limit=4096):
    proc = spawn_managed([sys.executable, "-B", "-c", code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"})
    try:
        result = collect_output(proc, timeout=15, limit=limit, input=input,
            reject_overflow=True, terminate=lambda: terminate_tree(proc))
        assert proc._gitgo_cleanup == {"complete": True, "mode": "windows-owned-job", "issues": []}
        return proc.returncode, result
    finally:
        if proc.poll() is None: terminate_tree(proc)
        finish_scope(proc)


def test_real_worker_exit_status_and_utf8_stdin_are_preserved():
    code, (out, err, truncated) = run("import sys; print(sys.stdin.read()); raise SystemExit(75)", input="测试 scope")
    assert code == 75 and out.strip() == "测试 scope" and not err and not truncated


def test_detached_descendant_is_cleaned_without_killing_foreign_process(tmp_path_factory):
    marker = tmp_path_factory.resolve() / "escaped"
    late = f"import time; from pathlib import Path; time.sleep(3); Path({str(marker)!r}).write_text('bad')"
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        code, _ = run("import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(late) + "], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x208)")
        assert code == 0
        time.sleep(3.2)
        assert not marker.exists() and foreign.poll() is None
    finally:
        foreign.kill(); foreign.wait(timeout=10)


def test_output_overflow_cancels_owned_job_and_cannot_authorize_handoff():
    with pytest.raises(OutputLimit): run("import sys,time; print('x'*100000,flush=True); time.sleep(30)", limit=512)


def test_breakaway_request_is_rejected_before_payload_creation():
    with pytest.raises(ValueError, match="breakaway"):
        spawn_managed([sys.executable, "-c", "raise RuntimeError('must not run')"], creationflags=0x01000000)


@pytest.mark.parametrize("options", [{"shell": True}, {"executable": "cmd.exe"}, {"close_fds": False}])
def test_trampoline_cannot_be_wrapped_or_replaced_before_job_assignment(options):
    with pytest.raises(ValueError):
        spawn_managed([sys.executable, "-c", "raise RuntimeError('must not run')"], **options)


@pytest.mark.parametrize("failure_on", [1, 2])
def test_pipe_allocation_failure_releases_job_and_partial_descriptors(monkeypatch, failure_on):
    from backend.core import windows_job
    api = windows_job._api()
    create, close = api.CreateJobObjectW, api.CloseHandle
    created, closed, descriptors = [], [], []
    def track_create(*args):
        handle = create(*args); created.append(handle); return handle
    def track_close(handle):
        closed.append(handle); return close(handle)
    api.CreateJobObjectW, api.CloseHandle = track_create, track_close
    monkeypatch.setattr(windows_job, "_api", lambda: api)
    pipe, calls = os.pipe, 0
    def interrupted_pipe():
        nonlocal calls
        calls += 1
        if calls == failure_on: raise OSError("Isolated pipe allocation fault")
        values = pipe(); descriptors.extend(values); return values
    with monkeypatch.context() as patch:
        patch.setattr(windows_job.os, "pipe", interrupted_pipe)
        with pytest.raises(OSError): spawn_managed([sys.executable, "-c", "print('must-not-run')"])
    assert len(created) == 1 and closed == created
    for descriptor in descriptors:
        with pytest.raises(OSError): os.fstat(descriptor)


def test_shared_lease_is_retained_until_job_is_empty(tmp_path_factory):
    from backend.core.update import windows_storage as win
    from backend.core.installation_lease import retain_descriptor
    path = tmp_path_factory.resolve() / "private"; win.create_private_directory(path)
    lock = path / "runtime.lock"; os.close(win.open_file(lock, write=True, create=True))
    with win.lock_file(lock, shared=True) as fd, retain_descriptor(fd):
        assert run("print('leased')")[0] == 0
    with win.lock_file(lock): pass


@pytest.mark.parametrize("leased", [False, True])
@pytest.mark.parametrize("failure_at", ["write", "flush", "close"])
def test_post_launch_pipe_failure_cleans_payload_and_guardian(
        tmp_path_factory, monkeypatch, leased, failure_at):
    from contextlib import nullcontext
    from backend.core import windows_job
    from backend.core.installation_lease import retain_descriptor
    from backend.core.update import windows_storage as win

    private = tmp_path_factory.resolve() / "private"
    win.create_private_directory(private)
    lock = private / "runtime.lock"
    os.close(win.open_file(lock, write=True, create=True))
    ready, late = private / "ready", private / "late"
    code = ("import time;from pathlib import Path;"
        f"Path({str(ready)!r}).write_text('started');time.sleep(1);"
        f"Path({str(late)!r}).write_text('escaped');time.sleep(30)")
    processes = []
    popen, fdopen = subprocess.Popen, os.fdopen

    def track(*args, **kwargs):
        proc = popen(*args, **kwargs); processes.append(proc); return proc

    class InterruptedControl:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def fail(self, stage):
            if stage != failure_at: return
            end = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < end: time.sleep(.01)
            assert ready.exists(), "Fault must occur after the real payload starts"
            raise OSError("Injected post-launch " + stage)
        def write(self, raw):
            result = self.stream.write(raw)
            self.stream.flush(); self.fail("write"); return result
        def flush(self):
            self.stream.flush(); self.fail("flush")
        def __exit__(self, *args):
            self.stream.close()
            if args[0] is None: self.fail("close")

    def interrupted_open(descriptor, mode="r", *args, **kwargs):
        stream = fdopen(descriptor, mode, *args, **kwargs)
        return InterruptedControl(stream) if mode == "wb" else stream

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", track)
            patch.setattr(os, "fdopen", interrupted_open)
            with win.lock_file(lock, shared=True) as fd:
                with retain_descriptor(fd) if leased else nullcontext():
                    with pytest.raises(OSError, match="Injected post-launch"):
                        spawn_managed([sys.executable, "-B", "-c", code],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert len(processes) == (2 if leased else 1)
        scope = processes[0]._gitgo_windows_scope
        assert scope.finished and scope not in windows_job._pending_scopes
        assert processes[0]._gitgo_cleanup["complete"] is True
        assert all(proc.poll() is not None for proc in processes)
        with win.lock_file(lock): pass
        time.sleep(1.2)
        assert not late.exists()
    finally:
        for proc in processes:
            if hasattr(proc, "_gitgo_windows_scope"): proc._gitgo_windows_scope.finish(cancel=True)
            proc.wait(timeout=10)


@pytest.mark.parametrize("failure_at", ["TerminateJobObject", "QueryInformationJobObject", "CloseHandle"])
def test_unknown_cleanup_keeps_ownership_blocks_launch_and_can_retry(monkeypatch, failure_at):
    from backend.core import windows_job
    proc = spawn_managed([sys.executable, "-B", "-c", "import time;time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    scope = proc._gitgo_windows_scope
    try:
        with monkeypatch.context() as patch:
            patch.setattr(scope.api, failure_at, lambda *args: 0)
            scope.finish(cancel=True)
            assert not scope.finished and scope in windows_job._pending_scopes
            assert proc._gitgo_cleanup["complete"] is False
            # No new process creation, even when the failed native call repeats.
            def forbidden(*args, **kwargs): pytest.fail("Started behind unresolved cleanup")
            patch.setattr(subprocess, "Popen", forbidden)
            with pytest.raises(OSError, match="cleanup remains unverified"):
                spawn_managed([sys.executable, "-c", "print('must not run')"])
        windows_job.retry_pending_cleanup()
        assert scope.finished and scope not in windows_job._pending_scopes
        assert proc._gitgo_cleanup["complete"] is True
        assert proc.poll() is not None
    finally:
        scope.finish(cancel=True)
        proc.wait(timeout=10)


def test_guardian_readiness_failure_is_owned_and_reaped(tmp_path_factory, monkeypatch):
    from backend.core import windows_job
    from backend.core.installation_lease import retain_descriptor
    from backend.core.update import windows_storage as win
    private = tmp_path_factory.resolve() / "private"
    win.create_private_directory(private)
    lock = private / "runtime.lock"
    os.close(win.open_file(lock, write=True, create=True))
    processes = []
    popen, read = subprocess.Popen, os.read
    def track(*args, **kwargs):
        proc = popen(*args, **kwargs); processes.append(proc); return proc
    def fail_ready(fd, size):
        if size == 1: raise OSError("Injected guardian readiness failure")
        return read(fd, size)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", track)
            patch.setattr(os, "read", fail_ready)
            with win.lock_file(lock, shared=True) as fd, retain_descriptor(fd):
                with pytest.raises(OSError, match="guardian readiness failure"):
                    spawn_managed([sys.executable, "-c", "raise RuntimeError('must not run')"])
        assert len(processes) == 2 and all(proc.poll() is not None for proc in processes)
        assert processes[0]._gitgo_windows_scope.finished
        with win.lock_file(lock): pass
    finally:
        for proc in processes:
            if hasattr(proc, "_gitgo_windows_scope"): proc._gitgo_windows_scope.finish(cancel=True)
            proc.wait(timeout=10)


def test_invalid_command_report_still_cleans_descendants_and_stays_failed(tmp_path_factory, monkeypatch):
    from backend.core import windows_job
    marker = tmp_path_factory.resolve() / "late"
    child = f"import time;from pathlib import Path;time.sleep(1);Path({str(marker)!r}).write_text('escaped')"
    code = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}])"
    proc = spawn_managed([sys.executable, "-B", "-c", code], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    scope = proc._gitgo_windows_scope
    try:
        proc.wait(timeout=10)
        with monkeypatch.context() as patch:
            patch.setattr(os, "read", lambda *args: b"invalid\n")
            scope.finish()
        assert scope.finished
        assert proc._gitgo_cleanup == {"complete": False, "mode": "windows-owned-job",
            "issues": ["job_report_invalid"]}
        windows_job.retry_pending_cleanup()
        assert proc._gitgo_cleanup["complete"] is False
        time.sleep(1.2)
        assert not marker.exists()
    finally:
        scope.finish(cancel=True)


def test_parent_crash_guardian_cleans_before_releasing_inherited_lease(tmp_path_factory):
    from backend.core.update import windows_storage as win
    from backend.core.update.validation import UpdateError
    path = tmp_path_factory.resolve() / "private"; win.create_private_directory(path)
    lock = path / "runtime.lock"; os.close(win.open_file(lock, write=True, create=True))
    marker = path / "must-not-write"
    worker = f"import time; from pathlib import Path; time.sleep(4); Path({str(marker)!r}).write_text('escaped')"
    parent = "\n".join([
        "import os,sys", "from pathlib import Path",
        "from backend.core.update.windows_storage import lock_file",
        "from backend.core.installation_lease import retain_descriptor",
        "from backend.core.process_control import spawn_managed",
        "with lock_file(Path(sys.argv[1]),shared=True) as fd, retain_descriptor(fd):",
        " p=spawn_managed([sys.executable,'-c'," + repr(worker) + "])",
        " os._exit(17)"])
    result = subprocess.run([sys.executable, "-B", "-c", parent, str(lock)], timeout=20)
    assert result.returncode == 17
    end = time.monotonic() + 15
    while True:
        try:
            with win.lock_file(lock): pass
            break
        except UpdateError as error:
            assert error.code == "UPDATE_BUSY" and time.monotonic() < end
            time.sleep(.05)
    time.sleep(4.2)
    assert not marker.exists()
