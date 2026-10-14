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
