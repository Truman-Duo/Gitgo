"""Plugin source binding, mandatory gates and native fixed-tree execution."""
from pathlib import Path

import pytest

from backend.core.config import Config, ConfigManager
from backend.core.publish.state import read_ledger
from backend.core.loop.tool_pipeline import ToolPipeline
from tests.test_publish_commands import setup, git, call
from tests.test_publish_formal import formal_setup, prepare, command, protocol


def install(setup, body, *, package=False):
    _, project, repo = setup
    folder = repo / ".gitgo" / "plugins"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "release_gate.py"
    if package:
        path = folder / "release_gate" / "__init__.py"
        path.parent.mkdir()
    path.write_text("from backend.core.plugin import SyncPlugin\n" + body, encoding="utf8")
    project.commit_format["plugins"] = ["release-gate"]
    ConfigManager.save(Config(projects=[project]))
    return path


GOOD = """class Gate(SyncPlugin):
    def on_commit_message(self, selected, project_config):
        assert selected[0]['hash'] and selected[0]['subject']
        assert 'workspace' not in project_config and 'release' not in project_config
        return f"[ANBM-{project_config['publish_number']}] feat: checked plugin message"
    def on_sync_start(self, entries, message):
        assert any(entry['rel_path'] == 'test_release.py' for entry in entries)
        assert message.endswith('checked plugin message')
plugin_class = Gate
"""


def plugin_protocol(monkeypatch):
    protocol(monkeypatch)
    original = ToolPipeline._execute_isolated
    def invoke(self, name, args, timeout, **kwargs):
        if name != "run_publish_plugins":
            return original(self, name, args, timeout, **kwargs)
        # Explicit protocol fixture; the separate native test covers isolation.
        from backend.core.tools.publish_plugins import run_publish_plugins
        kwargs["runtime_metadata"].update(launch_state="started", exit_code=0, cleanup={"complete": True, "mode": "protocol-fixture"})
        return run_publish_plugins(args)
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)


def test_plugin_message_is_reviewable_bound_and_recovered_without_reexecution(formal_setup, monkeypatch):
    from backend.core.publish.integration import FormalMixin
    from backend.core.loop.publish_runtime import PublishCheckRuntime
    path = install(formal_setup, GOOD)
    item = prepare(formal_setup)
    assert item["plan"]["plugins"][0]["id"] == "release-gate"
    assert "checked plugin message" not in item["plan"]["commit_message"]
    plugin_protocol(monkeypatch)
    original = FormalMixin._finish_check
    monkeypatch.setattr(FormalMixin, "_finish_check", lambda *a, **k: (_ for _ in ()).throw(OSError("lost ack")))
    assert command(formal_setup, "verify", item)["effect_state"] == "ambiguous"
    checking = read_ledger(formal_setup[2])[0]["items"][item["id"]]
    monkeypatch.setattr(FormalMixin, "_finish_check", original)
    monkeypatch.setattr(PublishCheckRuntime, "verify_tree", lambda *a: pytest.fail("must not rerun plugin"))
    checked = command(formal_setup, "reconcile", checking)["item"]
    assert checked["formal_eligible"], [(e["kind"], e["summary"], e.get("detail")) for e in checked["evidence"]]
    assert checked["plan"]["commit_message"].endswith("checked plugin message")
    assert checked["binding"] != item["binding"]
    assert [row["role"] for row in checked["evidence"][-1]["native_producers"]] == ["commit_plugin", "registered_test"]
    assert any(e["kind"] == "outbound_privacy" and not e["current"] for e in checked["evidence"])
    path.write_text(path.read_text() + "\n# changed dependency\n", encoding="utf8")
    assert command(formal_setup, "execute", checked)["error"] == "PUBLISH_PLAN_STALE"


@pytest.mark.parametrize("body,error", [
    ("raise RuntimeError('broken import')\n", "PUBLISH_PLUGIN_LOAD_OR_HOOK_FAILED"),
    ("class Gate(SyncPlugin):\n    def on_sync_start(self, entries, message): return 'review needed'\nplugin_class = Gate\n", "PUBLISH_PLUGIN_BLOCKED"),
    ("class Gate(SyncPlugin):\n    def on_commit_message(self, selected, config): return '[ANBM-99999] feat: wrong number'\nplugin_class = Gate\n", "PUBLISH_COMMIT_FORMAT_INVALID"),
    ("class Gate(SyncPlugin):\n    def on_commit_select(self, commits): return [999]\nplugin_class = Gate\n", "PUBLISH_PLUGIN_PROTOCOL_INVALID"),
    ("class Gate(SyncPlugin):\n    def on_commit_message(self, selected, config): return f\"[ANBM-{config['publish_number']}] feat: ghp_\" + 'x' * 36\nplugin_class = Gate\n", "PUBLISH_PLUGIN_PRIVACY_BLOCKED"),
])
def test_plugin_failures_are_required_failures_not_skipped(formal_setup, monkeypatch, body, error):
    install(formal_setup, body)
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    assert not checked["formal_eligible"]
    assert checked["evidence"][-1]["summary"] == error, checked["evidence"]
    assert all(row["state"] == "not_run" for row in checked["evidence"][-1]["checks"])
    assert command(formal_setup, "execute", checked)["error"] == "PUBLISH_CHECKS_REQUIRED"
    assert "ghp_" + "x" * 36 not in str(read_ledger(formal_setup[2])[0])


def test_prepare_and_queries_do_not_import_plugins(formal_setup):
    marker = formal_setup[2] / "imported-in-host.txt"
    install(formal_setup, f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n" + GOOD)
    item = prepare(formal_setup)
    services, project, _ = formal_setup
    for operation, args in [("publish.items", {}), ("publish.item", {"item_id": item["id"]})]:
        services.invoke(operation, {"project": project.name, **args})
    assert not marker.exists()


def test_plugin_cannot_modify_private_tree_then_claim_a_valid_message(formal_setup, monkeypatch):
    install(formal_setup, GOOD)
    item = prepare(formal_setup)
    plugin_protocol(monkeypatch)
    original = ToolPipeline._execute_isolated
    def invoke(self, name, args, timeout, **kwargs):
        result = original(self, name, args, timeout, **kwargs)
        if name == "run_publish_plugins":
            (Path(args["_workspace"]) / "test_release.py").write_text("forged\n")
        return result
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)
    checked = command(formal_setup, "verify", item)["item"]
    assert not checked["formal_eligible"] and checked["evidence"][-1]["summary"] == "FILE_CHANGED"


from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_registered_test_service import pytest_native_runtime


def test_real_native_package_plugin_gates_message_and_leaves_no_outputs(formal_setup, pytest_native_runtime):
    _, _, repo = formal_setup
    original_head = git(repo, "rev-parse", "HEAD")
    path = install(formal_setup, "from .helper import probe\nprobe()\n" + GOOD, package=True)
    (path.parent / "helper.py").write_text(f"""from pathlib import Path
def probe():
    assert not Path('.git').exists()
    try:
        Path({str(repo / 'test_release.py')!r}).read_text()
    except PermissionError:
        pass
    else:
        raise AssertionError('original workspace readable')
    Path('plugin-private.txt').write_text('discard')
""", encoding="utf8")
    item = prepare(formal_setup)
    checked = command(formal_setup, "verify", item)["item"]
    assert checked["formal_eligible"], [(e["kind"], e["summary"], e.get("detail")) for e in checked["evidence"]]
    assert len(checked["evidence"][-1]["native_producers"]) == 2
    integrated = command(formal_setup, "execute", checked)["item"]
    assert integrated["status"] == "integrated"
    assert git(repo, "show", "-s", "--format=%B", "formal") == checked["plan"]["commit_message"]
    assert git(repo, "rev-parse", "HEAD") == original_head and not (repo / "plugin-private.txt").exists()
    assert "plugin-private.txt" not in git(repo, "ls-tree", "-r", "--name-only", "formal")
