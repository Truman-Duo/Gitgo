"""Selection proposals change real trees; completion observes actual integration."""
import pytest

from backend.core.publish.state import read_ledger
from backend.core.loop.tool_pipeline import ToolPipeline
from tests.test_publish_commands import setup, git, call
from tests.test_publish_formal import formal_setup, prepare, command
from tests.test_publish_plugins import install, plugin_protocol


def extra(setup):
    repo = setup[2]
    (repo / "omit.txt").write_text("not selected\n", encoding="utf8")
    git(repo, "add", "omit.txt")
    git(repo, "commit", "-m", "feat: optional output")


@pytest.mark.parametrize("hook", [
    "def on_scan_complete(self, entries):\n        return [e for e in entries if e['rel_path'] != 'omit.txt']",
    "def on_commit_select(self, commits):\n        return [0]",
])
def test_selection_rebuilds_fixed_tree_and_requires_another_verification(formal_setup, monkeypatch, hook):
    extra(formal_setup)
    install(formal_setup, "class Gate(SyncPlugin):\n    " + hook + "\nplugin_class = Gate\n")
    original = git(formal_setup[2], "rev-parse", "HEAD")
    target = git(formal_setup[2], "rev-parse", "formal")
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    selected = command(formal_setup, "verify", item)["item"]
    assert selected["binding"] != item["binding"]
    assert selected["plan"]["tree_oid"] != item["plan"]["tree_oid"]
    assert selected["evidence"][-1]["summary"] == "PUBLISH_PLUGIN_SELECTION_REVIEW_REQUIRED"
    assert not selected["formal_eligible"]
    assert all(row["state"] == "not_run" for row in selected["evidence"][-1]["checks"])
    assert "omit.txt" not in git(formal_setup[2], "ls-tree", "-r", "--name-only", selected["plan"]["tree_oid"])
    assert git(formal_setup[2], "rev-parse", "formal") == target
    assert any(not e["current"] for e in selected["evidence"])
    assert command(formal_setup, "execute", selected)["error"] == "PUBLISH_CHECKS_REQUIRED"
    checked = command(formal_setup, "verify", selected)["item"]
    assert checked["formal_eligible"], checked["evidence"][-1]
    integrated = command(formal_setup, "execute", checked)["item"]
    assert integrated["plugin_completion"]["state"] == "passed"
    assert integrated["status"] == "integrated"
    assert "omit.txt" not in git(formal_setup[2], "ls-tree", "-r", "--name-only", "formal")
    assert git(formal_setup[2], "rev-parse", "HEAD") == original
    assert (formal_setup[2] / "omit.txt").exists()


def test_forged_plugin_selection_cannot_change_paths_or_object_identity(formal_setup, monkeypatch):
    install(formal_setup, """class Gate(SyncPlugin):
    def on_scan_complete(self, entries):
        entries[0]['workspace_hash'] = 'forged'
        return entries
plugin_class = Gate
""")
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["binding"] == item["binding"] and not checked["formal_eligible"]
    assert checked["evidence"][-1]["summary"] == "PUBLISH_PLUGIN_PROTOCOL_INVALID"


COMPLETION = """class Gate(SyncPlugin):
    def on_sync_complete(self, result):
        assert result['success'] is True
        assert len(result['commit_hash']) in (40, 64)
        assert result['files_count'] == 1
plugin_class = Gate
"""


def test_completion_after_integration_is_recovered_without_callback_reexecution(formal_setup, monkeypatch):
    from backend.core.publish.plugin_lifecycle import PluginLifecycleMixin
    from backend.core.loop.publish_runtime import PublishCheckRuntime
    install(formal_setup, COMPLETION)
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["formal_eligible"]
    record = PluginLifecycleMixin._record_plugin_completion
    monkeypatch.setattr(PluginLifecycleMixin, "_record_plugin_completion", lambda *a, **k: (_ for _ in ()).throw(OSError("lost ack")))
    result = command(formal_setup, "execute", checked)
    assert result["effect_state"] == "ambiguous"
    pending = read_ledger(formal_setup[2])[0]["items"][item["id"]]
    assert pending["status"] == "executing" and pending["operations"][-1]["kind"] == "plugin_completion"
    result_oid = git(formal_setup[2], "rev-parse", "formal")
    assert pending["plugin_completion"]["git_result_oid"] == result_oid
    monkeypatch.setattr(PluginLifecycleMixin, "_record_plugin_completion", record)
    monkeypatch.setattr(PublishCheckRuntime, "complete_tree", lambda *a: pytest.fail("must not rerun callback"))
    recovered = command(formal_setup, "reconcile", pending)["item"]
    assert recovered["status"] == "integrated"
    assert recovered["plugin_completion"]["state"] == "passed" and recovered["plugin_completion"]["recovered"]
    assert git(formal_setup[2], "rev-parse", "formal") == result_oid


def test_crash_before_completion_intent_records_skipped_hook_without_launch(formal_setup, monkeypatch):
    from backend.core.publish.plugin_lifecycle import PluginLifecycleMixin
    install(formal_setup, COMPLETION)
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    monkeypatch.setattr(PluginLifecycleMixin, "_start_plugin_completion", lambda *a: (_ for _ in ()).throw(OSError("crash before intent")))
    assert command(formal_setup, "execute", checked)["effect_state"] == "ambiguous"
    pending = read_ledger(formal_setup[2])[0]["items"][item["id"]]
    assert pending["status"] == "executing" and pending["operations"][-1]["state"] == "observed"
    recovered = command(formal_setup, "reconcile", pending)["item"]
    assert recovered["status"] == "integrated" and recovered["plugin_completion"]["state"] == "interrupted"
    assert recovered["evidence"][-1]["summary"] == "PUBLISH_PLUGIN_COMPLETION_NOT_STARTED"


def test_completion_failure_keeps_integrated_git_fact(formal_setup, monkeypatch):
    install(formal_setup, COMPLETION.replace("assert result['success'] is True", "raise RuntimeError('callback failed')"))
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    result = command(formal_setup, "execute", checked)
    assert result["effect_state"] == "committed" and result["completion_failed"]
    assert result["item"]["status"] == "integrated" and result["item"]["plugin_completion"]["state"] == "failed"
    assert result["item"]["plugin_completion"]["git_result_oid"] == git(formal_setup[2], "rev-parse", "formal")


def test_completion_cleanup_debt_is_retired_without_replaying_callback(formal_setup, monkeypatch):
    from backend.core.loop.publish_runtime import PublishCheckRuntime
    install(formal_setup, COMPLETION)
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    monkeypatch.setattr(PublishCheckRuntime, "complete_tree", lambda *a: {"cleanup_pending": True})
    result = command(formal_setup, "execute", checked)
    assert result["effect_state"] == "ambiguous" and result["item"]["status"] == "executing", result
    assert result["item"]["plugin_completion"]["state"] == "cleanup_pending"
    monkeypatch.setattr(PublishCheckRuntime, "complete_tree", lambda *a: pytest.fail("callback cannot restart"))
    recovered = command(formal_setup, "reconcile", result["item"])["item"]
    assert recovered["status"] == "integrated" and recovered["plugin_completion"]["state"] == "failed"
    assert recovered["evidence"][-1]["summary"] == "PUBLISH_PLUGIN_COMPLETION_INTERRUPTED"


def test_known_refusal_calls_completion_with_failed_result(formal_setup, monkeypatch):
    from subprocess import CompletedProcess
    from backend.core.publish.git import CandidateGit
    install(formal_setup, COMPLETION.replace("assert result['success'] is True", "assert result['success'] is False")
        .replace("assert len(result['commit_hash']) in (40, 64)", "assert result['commit_hash'] == ''"))
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    before = git(formal_setup[2], "rev-parse", "formal")
    monkeypatch.setattr(CandidateGit, "integrate", lambda *a: CompletedProcess([], 1, "", "refused"))
    result = command(formal_setup, "execute", checked)
    assert result["error"] == "PUBLISH_FORMAL_NOT_APPLIED" and result["integration_failed"]
    assert result["item"]["status"] == "failed" and result["item"]["plugin_completion"]["state"] == "passed"
    assert result["item"]["allowed_actions"] == ["revise"]
    assert result["item"]["plugin_completion"]["integration_succeeded"] is False
    assert git(formal_setup[2], "rev-parse", "formal") == before


def test_timeout_does_not_authorize_failed_completion_or_clear_unknown_effect(formal_setup, monkeypatch):
    from subprocess import CompletedProcess
    from backend.core.publish.git import CandidateGit
    install(formal_setup, COMPLETION)
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    monkeypatch.setattr(CandidateGit, "integrate", lambda *a: CompletedProcess([], -1, "", "timeout"))
    result = command(formal_setup, "execute", checked)
    assert result["effect_state"] == "ambiguous"
    assert result["item"]["status"] == "executing" and result["item"]["operations"][-1]["kind"] == "formal"
    assert not result["item"].get("plugin_completion")


from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def test_all_hooks_run_in_native_boundary_and_completion_cannot_write_workspace(formal_setup, pytest_native_runtime):
    extra(formal_setup)
    original = formal_setup[2] / "native-hook-output.txt"
    install(formal_setup, f"""from pathlib import Path
class Gate(SyncPlugin):
    def on_scan_complete(self, entries):
        assert not Path('.git').exists()
        return [e for e in entries if e['rel_path'] != 'omit.txt']
    def on_commit_select(self, commits):
        return [0]
    def on_commit_message(self, selected, config):
        assert len(selected) == 1
        return f"[ANBM-{{config['publish_number']}}] feat: full lifecycle"
    def on_sync_start(self, entries, message):
        assert [e['rel_path'] for e in entries] == ['test_release.py']
    def on_sync_complete(self, result):
        assert result['success'] and len(result['commit_hash']) in (40, 64)
        assert Path('test_release.py').exists()
        try:
            Path({str(original)!r}).write_text('escaped')
        except PermissionError:
            pass
        else:
            raise AssertionError('workspace writable')
        Path('discard-completion.txt').write_text('private')
plugin_class = Gate
""")
    item = prepare(formal_setup)
    selected = command(formal_setup, "verify", item)["item"]
    assert not selected["formal_eligible"] and selected["binding"] != item["binding"], selected["evidence"][-1]
    checked = command(formal_setup, "verify", selected)["item"]
    assert checked["formal_eligible"], checked["evidence"][-1]
    result = command(formal_setup, "execute", checked)["item"]
    assert result["plugin_completion"]["state"] == "passed", result["evidence"][-1]
    assert result["evidence"][-1]["native_producers"][0]["role"] == "completion_plugin"
    assert not original.exists() and not (formal_setup[2] / "discard-completion.txt").exists()
    assert "omit.txt" not in git(formal_setup[2], "ls-tree", "-r", "--name-only", "formal")
