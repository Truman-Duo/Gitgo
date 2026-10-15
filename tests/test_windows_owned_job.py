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
