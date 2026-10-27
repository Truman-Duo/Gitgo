"""Check lifecycle faults, durable results, cancellation and exact recovery."""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.test_publish_formal import formal_setup, setup, prepare, command, protocol
from tests.test_publish_commands import call, git
from backend.core.publish.state import read_ledger
from backend.core.storage import get_storage
from backend.core.loop.publish_runtime import PublishCheckRuntime
from backend.core.loop.publish_recovery import read, save


def latest(setup, item):
    return read_ledger(setup[1].workspace_path)[0]["items"][item["id"]]


def test_pause_resume_preserves_plan_and_requires_explicit_next_action(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    paused = command(formal_setup, "pause", item)["item"]
    assert paused["status"] == "paused" and paused["allowed_actions"] == ["resume"]
    assert paused["binding"] == item["binding"] and paused["plan"] == item["plan"]
    assert command(formal_setup, "verify", paused)["error"] == "PUBLISH_ACTION_UNAVAILABLE"
    resumed = command(formal_setup, "resume", paused)["item"]
    assert resumed["status"] == "prepared" and not resumed["formal_eligible"]
    protocol(monkeypatch)
    assert command(formal_setup, "verify", resumed)["item"]["formal_eligible"]


def test_dead_owner_releases_cross_process_lease_before_recovery(formal_setup, monkeypatch, tmp_path_factory):
    import subprocess
    import sys
    import time
    item = prepare(formal_setup)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *a: {"cleanup_pending": True})
    checking = command(formal_setup, "verify", item)["item"]
    storage = get_storage(formal_setup[1].workspace_path)
    lock = storage.paths.project_root / "publish-locks" / item["id"]
    ready = tmp_path_factory / "lease-ready"
    script = tmp_path_factory / "lease-owner.py"
    script.write_text("import sys\nsys.path[:0] = " + repr(sys.path) + "\n"
        "from pathlib import Path\nfrom backend.core.storage.maintenance import StorageLease\nimport time\n"
        "with StorageLease(Path(" + repr(str(lock)) + "), exclusive=True):\n"
        "    Path(" + repr(str(ready)) + ").write_text('owned')\n    time.sleep(30)\n", encoding="utf8")
    process = subprocess.Popen([sys.executable, str(script)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(.05)
        assert ready.exists()
        assert command(formal_setup, "reconcile", checking, "live-owner")["error"] == "PUBLISH_OPERATION_RUNNING"
    finally:
        process.terminate(); process.wait(timeout=10)
    assert command(formal_setup, "reconcile", checking, "dead-owner")["item"]["status"] == "prepared"


def test_running_check_rejects_reconcile_and_pause_cancels_without_cancelling_agent(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    started, release = threading.Event(), threading.Event()
    contexts = []
    def execute(self, image, plan):
        contexts.append(self.ctx)
        started.set()
        assert release.wait(10)
        assert self.ctx.is_cancelled()
        assert not self.ctx.process.cancellation_event.is_set()
        # Even a raced successful result must not turn a paused check green.
        return {"passed": True, "verification_state": "current"}
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", execute)
    with ThreadPoolExecutor(1) as pool:
        work = pool.submit(command, formal_setup, "verify", item)
        try:
            assert started.wait(10)
            checking = latest(formal_setup, item)
            assert command(formal_setup, "reconcile", checking)["error"] == "PUBLISH_OPERATION_RUNNING"
            paused = command(formal_setup, "pause", checking)["item"]
            assert paused["status"] == "pausing" and not paused["formal_eligible"]
        finally:
            release.set()
        result = work.result(timeout=10)["item"]
    assert result["status"] == "paused" and result["evidence"][-1]["summary"] == "PUBLISH_CHECK_PAUSED"
    assert command(formal_setup, "resume", result)["item"]["formal_eligible"] is False


def test_completed_check_result_survives_lost_ledger_ack_without_reexecution(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch)
    from backend.core.publish.integration import FormalMixin
    complete = FormalMixin._finish_check
    monkeypatch.setattr(FormalMixin, "_finish_check", lambda *a, **k: (_ for _ in ()).throw(OSError("lost acknowledgement")))
    failed = command(formal_setup, "verify", item)
    assert failed["effect_state"] == "ambiguous"
    checking = latest(formal_setup, item)
    monkeypatch.setattr(FormalMixin, "_finish_check", complete)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *a: pytest.fail("recovery must not replay tests"))
    recovered = command(formal_setup, "reconcile", checking)["item"]
    assert recovered["formal_eligible"] and recovered["evidence"][-1]["recovered"]
    assert recovered["evidence"][-1]["runtime_owner"] == checking["operations"][-1]["runtime_owner"]


def test_interrupted_before_producer_is_failed_then_explicitly_recheckable(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    def die(*args):
        raise OSError("host stopped before producer allocation")
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", die)
    assert command(formal_setup, "verify", item)["effect_state"] == "ambiguous"
    checking = latest(formal_setup, item)
    recovered = command(formal_setup, "reconcile", checking)["item"]
    assert recovered["status"] == "prepared" and not recovered["formal_eligible"]
    assert recovered["evidence"][-1]["summary"] == "PUBLISH_CHECK_INTERRUPTED"
    assert "verify" in recovered["allowed_actions"]


@pytest.mark.parametrize("native_complete", [True, False])
def test_pending_cleanup_retires_exact_boundary_without_fabricating_pass(formal_setup, monkeypatch, native_complete):
    item = prepare(formal_setup)
    _, project, repo = formal_setup
    original = git(repo, "rev-parse", "formal")
    protocol(monkeypatch)
    from backend.core.sandbox import SandboxDenied
    def pending(self):
        raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "injected retirement failure")
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.close", pending)
    checking = command(formal_setup, "verify", item)["item"]
    operation = checking["operations"][-1]
    storage = get_storage(project.workspace_path)
    record = read(storage, operation)
    producer = record["producers"][0]
    assert producer["phase"] == "cleanup_pending" and Path(producer["stage"]).exists()
    # Explicit protocol fixture: no ACL was changed. Supply its retired access
    # observation rather than treating a missing production journal as success.
    import json
    journal = storage.paths.project_root / "sandbox_access" / (Path(producer["stage"]).name + ".json")
    journal.parent.mkdir(exist_ok=True)
    journal.write_text(json.dumps({"version": 1, "kind": "scoped_workspace_access", "phase": "revoked",
                                   "workspace": producer["stage"]}), encoding="utf8")
    seen = []
    def retire(identity, location):
        seen.append((identity, location))
        return native_complete
    monkeypatch.setattr("backend.core.loop.publish_recovery.retire_native", retire)
    result = command(formal_setup, "reconcile", checking)
    assert seen == [(producer["recovery_id"], producer["location"])]
    if native_complete:
        assert result["item"]["status"] == "prepared" and not result["item"]["formal_eligible"]
        assert not Path(producer["stage"]).exists()
        assert read(storage, operation)["producers"][0]["phase"] == "retired"
    else:
        assert result["error"] == "SANDBOX_CLEANUP_PENDING"
        assert latest(formal_setup, item)["status"] == "checking" and Path(producer["stage"]).exists()
    assert git(repo, "rev-parse", "formal") == original


def test_legacy_or_mismatched_recovery_record_cannot_reset_check(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *a: {"cleanup_pending": True})
    checking = command(formal_setup, "verify", item)["item"]
    storage = get_storage(formal_setup[1].workspace_path)
    operation = checking["operations"][-1]
    record = read(storage, operation)
    record["owner"] = ["different-process", "different-task"]
    save(storage, operation["runtime_key"], record)
    assert command(formal_setup, "reconcile", checking)["error"] == "PUBLISH_RUNTIME_RECORD_UNAVAILABLE"
    assert latest(formal_setup, item)["status"] == "checking"


def test_pause_and_recovery_both_survive_response_loss_and_replay(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *a: {"cleanup_pending": True})
    checking = command(formal_setup, "verify", item)["item"]
    paused = command(formal_setup, "pause", checking)
    assert command(formal_setup, "pause", checking) == paused
    recovered = command(formal_setup, "reconcile", paused["item"])
    assert recovered["item"]["status"] == "paused"
    assert command(formal_setup, "reconcile", paused["item"]) == recovered


from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def test_real_native_access_retirement_recovered_without_rerunning_test(formal_setup, pytest_native_runtime, monkeypatch):
    from backend.core.sandbox_access import WorkspaceAccess
    from backend.core.sandbox import SandboxDenied
    close = WorkspaceAccess.close
    def fail_close(self):
        raise SandboxDenied("SANDBOX_CLEANUP_PENDING", "injected access retirement interruption")
    item = prepare(formal_setup)
    monkeypatch.setattr(WorkspaceAccess, "close", fail_close)
    checking = command(formal_setup, "verify", item)["item"]
    assert checking["status"] == "checking"
    storage = get_storage(formal_setup[1].workspace_path)
    producer = read(storage, checking["operations"][-1])["producers"][0]
    assert producer["phase"] == "cleanup_pending" and producer["observation"]["launch_state"] == "started"
    monkeypatch.setattr(WorkspaceAccess, "close", close)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *a: pytest.fail("must not relaunch"))
    recovered = command(formal_setup, "reconcile", checking)["item"]
    assert recovered["status"] == "prepared" and not recovered["formal_eligible"]
    assert not Path(producer["stage"]).exists()
    import json
    journal = storage.paths.project_root / "sandbox_access" / (Path(producer["stage"]).name + ".json")
    if journal.exists():
        assert json.loads(journal.read_text(encoding="utf8"))["phase"] == "revoked"


def test_real_native_running_check_can_pause_and_resume(formal_setup, pytest_native_runtime):
    import time
    _, project, repo = formal_setup
    (repo / "test_release.py").write_text("from pathlib import Path\nimport time\ndef test_release():\n    Path('started').write_text('running')\n    time.sleep(30)\n", encoding="utf8")
    git(repo, "add", "test_release.py"); git(repo, "commit", "-m", "test: long running check")
    item = prepare(formal_setup)
    storage = get_storage(project.workspace_path)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(command, formal_setup, "verify", item)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                checking = latest(formal_setup, item)
                if checking["status"] == "checking":
                    record = read(storage, checking["operations"][-1])
                    if record["producers"] and (Path(record["producers"][-1]["stage"]) / "started").exists():
                        break
                if future.done():
                    pytest.fail(str(future.result()))
                time.sleep(.1)
            else:
                pytest.fail("native test did not start")
        finally:
            checking = latest(formal_setup, item)
            if checking["status"] == "checking":
                assert command(formal_setup, "pause", checking)["item"]["status"] == "pausing"
        result = future.result(timeout=20)["item"]
    assert result["status"] == "paused" and not result["formal_eligible"]
    assert result["evidence"][-1]["native_producers"][0]["access_retired"] is True
    assert command(formal_setup, "resume", result)["item"]["status"] == "prepared"
