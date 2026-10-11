import json
import subprocess
import sys
import pytest
from backend.core.architecture_monitor import ArchitectureMonitor, source_metrics, BASELINE
from backend.core.process_output import collect_output, OutputLimit


def test_maintenance_debt_baseline_growth_and_visible_failure(tmp_path_factory):
    path = tmp_path_factory / "legacy.py"
    path.write_text("# known\n" * 900)
    metrics, _ = source_metrics(tmp_path_factory)
    baseline = tmp_path_factory / BASELINE
    baseline.write_text(json.dumps({"version": 1, "files": metrics}))
    events = []
    monitor = ArchitectureMonitor(tmp_path_factory, events.append)
    assert monitor.inspect()["finding_count"] == 0
    path.write_text("# growth\n" * 1000)
    monitor.inspect(); monitor.inspect()
    assert len(events) == 1 and events[0]["blocking"] is False
    assert events[0]["findings"][0]["baseline_lines"] == 900
    baseline.write_text("corrupt")
    monitor.interval = .01
    monitor.start()
    monitor.stop_event.wait(.1)
    monitor.stop()
    assert any("failed" in e["message"] for e in events)


def test_real_pipe_flood_is_bounded_without_deadlock():
    proc = subprocess.Popen([sys.executable, "-c", "import sys;sys.stdout.write('x'*500000);sys.stderr.write('y'*500000)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err, truncated = collect_output(proc, timeout=10, limit=8192, terminate=proc.kill)
    assert truncated and len(out) == len(err) == 8192 and proc.returncode == 0


def test_protocol_overflow_terminates_real_process():
    proc = subprocess.Popen([sys.executable, "-c", "import sys,time;sys.stdout.write('x'*500000);sys.stdout.flush();time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def terminate():
        proc.kill(); proc.wait(timeout=5)
    with pytest.raises(OutputLimit):
        collect_output(proc, timeout=10, limit=8192, reject_overflow=True, terminate=terminate)
    assert proc.poll() is not None
