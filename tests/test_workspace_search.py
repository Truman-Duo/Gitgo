"""Search contracts tested against real ripgrep, fallback and the Host pipeline."""
from pathlib import Path
import json
import os
import sys
import time

import pytest

from backend.core.tools import workspace_search as adapter
from backend.core.tools.workspace_tools import search_text, list_files


@pytest.fixture
def rg_available(monkeypatch):
    path = adapter.resolve_ripgrep()
    if not path:
        pytest.skip("real ripgrep is unavailable")
    monkeypatch.setenv("GITGO_RIPGREP_PATH", path)
    return path


def query(root, **args):
    return search_text({"_workspace": str(root), "pattern": "needle", "literal": True, **args})


def test_real_content_context_unicode_and_leading_dash(tmp_path_factory, rg_available):
    root = tmp_path_factory
    (root / "中文.txt").write_text("before\n--needle 中文\nafter\n", encoding="utf-8")
    result = query(root, pattern="--needle", context_lines=1)
    assert result["engine"] == "ripgrep" and result["complete"]
    row = result["matches"][0]
    assert row["file"] == "中文.txt" and row["line"] == 2
    assert row["context_before"] == [{"line": 1, "text": "before"}]
    assert row["context_after"] == [{"line": 3, "text": "after"}]


def test_pagination_looks_ahead_and_exact_limit_is_complete(tmp_path_factory, rg_available):
    root = tmp_path_factory
    (root / "a.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")
    first = query(root, max_results=2)
    assert first["next_offset"] == 2 and first["truncated"]
    second = query(root, max_results=2, offset=2)
    assert [r["line"] for r in second["matches"]] == [3]
    assert second["complete"] and second["next_offset"] is None
    assert query(root, max_results=3)["complete"]
    assert query(root, pattern="absent")["complete"]


def test_file_discovery_and_modes_share_stable_scope(tmp_path_factory, rg_available):
    root = tmp_path_factory
    (root / "b.py").write_text("needle\nneedle\n", encoding="utf-8")
    (root / "a.py").write_text("needle\n", encoding="utf-8")
    (root / "empty.txt").write_text("empty", encoding="utf-8")
    files = query(root, output_mode="files")
    assert [r["path"] for r in files["files"]] == ["a.py", "b.py"]
    counts = query(root, output_mode="count")
    assert counts["counts"] == [{"file": "a.py", "count": 1}, {"file": "b.py", "count": 2}]
    listing = list_files({"_workspace": str(root), "pattern": "*.py", "max_results": 1})
    assert listing["files"][0]["path"] == "a.py" and listing["next_offset"] == 1


def test_ignore_hidden_nested_generated_and_explicit_overrides(tmp_path_factory, rg_available):
    root = tmp_path_factory
    (root / ".git").mkdir()
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    for name in ("visible.txt", "ignored.txt", ".hidden.txt"):
        (root / name).write_text("needle", encoding="utf-8")
    (root / "sub" / "node_modules").mkdir(parents=True)
    (root / "sub" / "node_modules" / "generated.txt").write_text("needle", encoding="utf-8")
    assert [r["file"] for r in query(root)["matches"]] == ["visible.txt"]
    wide = query(root, include_hidden=True, respect_ignore=False)
    assert {r["file"] for r in wide["matches"]} == {"visible.txt", "ignored.txt", ".hidden.txt"}
    explicit = query(root, include=["ignored.txt"])
    assert [r["file"] for r in explicit["matches"]] == ["ignored.txt"]
    assert query(root, include=["*.txt"], exclude=["ignored.txt", "visible.txt"])["matches"] == []


def test_invalid_regex_is_failure_and_never_engine_fallback(tmp_path_factory, rg_available):
    (tmp_path_factory / "a.txt").write_text("needle", encoding="utf-8")
    result = query(tmp_path_factory, pattern="(", literal=False)
    assert result["error"] == "INVALID_REGEX" and result["partial"]
    assert result["error_info"]["catalog_id"] == "GITGO-E3604"
    assert result["engine"] == "ripgrep" and result["warnings"]


def test_literal_fallback_single_file_context_and_visible_limit(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    monkeypatch.setattr(adapter, "resolve_ripgrep", lambda: None)
    (root / "a.txt").write_text("before\nneedle\nafter\nneedle\n", encoding="utf-8")
    result = query(root, path="a.txt", context_lines=1, max_results=1)
    assert result["engine"] == "python" and result["degraded"] and result["warnings"]
    assert result["matches"][0]["context_before"][0]["text"] == "before"
    assert result["truncated"] and result["next_offset"] == 1
    assert query(root, path="a.txt", offset=1)["matches"][0]["line"] == 4
    assert query(root, path="a.txt", output_mode="count")["counts"] == [{"file": "a.txt", "count": 2}]


def test_fallback_refuses_to_forge_regex_or_ignore_coverage(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    monkeypatch.setattr(adapter, "resolve_ripgrep", lambda: None)
    (root / "a.txt").write_text("needle", encoding="utf-8")
    assert query(root, pattern="n.*e", literal=False)["error"] == "SEARCH_ENGINE_REQUIRED"
    (root / ".gitignore").write_text("a.txt", encoding="utf-8")
    result = query(root)
    assert result["error"] == "SEARCH_ENGINE_REQUIRED" and not result["complete"]
    assert result["error_info"]["catalog_id"] == "GITGO-E3601"
    assert query(root, respect_ignore=False)["count"] == 1


def test_unlaunchable_engine_has_explicit_recovery(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(adapter, "resolve_ripgrep", lambda: str(tmp_path_factory / "missing-rg.exe"))
    (tmp_path_factory / "a.txt").write_text("needle", encoding="utf-8")
    result = query(tmp_path_factory, path="a.txt")
    assert result["count"] == 1 and result["engine"] == "python"
    assert any(w["code"] == "RIPGREP_UNAVAILABLE" for w in result["warnings"])


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_binary_and_large_files_have_the_same_declared_exclusions(tmp_path_factory, rg_available, monkeypatch, engine):
    root = tmp_path_factory
    if engine == "python":
        monkeypatch.setattr(adapter, "resolve_ripgrep", lambda: None)
    (root / "binary.dat").write_bytes(b"\0needle\n")
    (root / "large.txt").write_bytes(b"needle\n" + b"x" * adapter.MAX_FILE_BYTES)
    (root / "a.txt").write_text("needle", encoding="utf-8")
    assert [r["file"] for r in query(root)["matches"]] == ["a.txt"]


def test_scope_escape_is_still_a_permission_error(tmp_path_factory):
    with pytest.raises(PermissionError):
        query(tmp_path_factory, path="../outside.txt")


def test_symlinks_do_not_search_external_targets(tmp_path_factory, rg_available):
    root = tmp_path_factory / "project"
    root.mkdir()
    target = tmp_path_factory / "outside.txt"
    target.write_text("needle", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = query(root)
    assert result["matches"] == []


def test_pipe_drain_limits_and_timeout_are_not_empty_success(tmp_path_factory):
    records = []
    outcome = adapter.stream_process([sys.executable, "-c", "import sys; sys.stderr.write('x'*100000); print('row')"],
                                     tmp_path_factory, b"\n", lambda b: records.append(b) or True, time.monotonic() + 5)
    assert outcome.exit_code == 0 and [r.rstrip(b"\r") for r in records] == [b"row"] and len(outcome.stderr) <= 8192
    outcome = adapter.stream_process([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path_factory,
                                     b"\n", lambda b: True, time.monotonic() + .2)
    assert outcome.stopped == "timeout"


def test_stream_stops_early_and_rejects_oversized_record(tmp_path_factory, monkeypatch):
    outcome = adapter.stream_process([sys.executable, "-c", "import time; print('row',flush=True); time.sleep(10)"],
                                     tmp_path_factory, b"\n", lambda b: False, time.monotonic() + 5)
    assert outcome.stopped == "page_limit"
    monkeypatch.setattr(adapter, "MAX_RECORD_BYTES", 1024)
    outcome = adapter.stream_process([sys.executable, "-c", "print('x'*2000)"], tmp_path_factory,
                                     b"\n", lambda b: True, time.monotonic() + 5)
    assert outcome.stopped == "record_budget"


def test_workspace_executable_is_not_implicitly_selected(tmp_path_factory, monkeypatch):
    monkeypatch.delenv("GITGO_RIPGREP_PATH", raising=False)
    monkeypatch.setenv("PATH", "." + os.pathsep + "relative-bin")
    monkeypatch.chdir(tmp_path_factory)
    (tmp_path_factory / "rg.exe").write_bytes(b"not an engine")
    assert adapter.resolve_ripgrep() is None


def test_isolated_runner_returns_the_same_search_protocol(tmp_path_factory, rg_available, runner_transport_only):
    from backend.core.loop.process_tool_runner import ProcessToolRunner
    (tmp_path_factory / "a.txt").write_text("needle", encoding="utf-8")
    result = ProcessToolRunner().run("search_text", {"_workspace": str(tmp_path_factory), "pattern": "needle", "literal": True})
    assert result.success and result.data["engine"] == "ripgrep"
    assert result.data["complete"] and result.data["matches"][0]["file"] == "a.txt"


def test_fallback_notices_and_coverage_survive_the_tool_pipeline(tmp_path_factory, monkeypatch):
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.tools.catalog import build_workspace_tools
    monkeypatch.setattr(adapter, "resolve_ripgrep", lambda: None)
    (tmp_path_factory / "a.txt").write_text("needle", encoding="utf-8")
    p = AgentProcess(process_id="search", role="worker", ring_level=RingLevel.RING_3,
                     active_task_id="task", session=AgentSession(), tool_registry=ToolRegistry(["search_text"]))
    tool = build_workspace_tools(tmp_path_factory)["search_text"]
    tool.isolated = False
    tool.execute = search_text
    bus, notices = EventBus(), []
    bus.subscribe("ToolNotice", lambda e: notices.append(e.data))
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(tmp_path_factory), event_bus=bus)
    result = ToolPipeline().execute({"name": "search_text", "args": {"pattern": "needle", "literal": True}}, tool, ctx, "search")
    assert not result.is_error and result.receipt["search_engine"] == "python"
    assert result.receipt["search_warning_codes"] == ["RIPGREP_UNAVAILABLE", "SEARCH_FALLBACK"]
    assert len(notices) == 2
    assert any(n.get("event") == "tool_notice" for n in p.session.host_ledger)


def test_summary_exposes_partial_and_fallback_without_opening_details():
    from backend.core.loop.tool_summary import compact_tool_summary
    assert compact_tool_summary("search_text", {"matches": [], "partial": True, "degraded": True}) == "0 matches · partial · fallback"


def test_frozen_runtime_prefers_its_bundled_engine_over_path(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    engine = root / ("rg.exe" if os.name == "nt" else "rg")
    engine.write_bytes(b"bundled engine identity")
    monkeypatch.delenv("GITGO_RIPGREP_PATH", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(root / "gitgo-host.exe"))
    assert adapter.resolve_ripgrep() == str(engine)


def test_staging_checks_real_engine_and_records_no_local_identity(tmp_path_factory, rg_available):
    from scripts.stage_search_engine import stage
    import hashlib
    root = tmp_path_factory
    notices = root / "fixture-notices.txt"
    # Fixture exercises transport/integrity, not the content of release notices.
    notices.write_text("test notice fixture", encoding="utf-8")
    destination = root / "internal" / "gitgo-host"
    manifest = stage(Path(rg_available), notices, destination)
    assert manifest["version"].startswith("ripgrep ")
    target = destination / manifest["binary"]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == manifest["sha256"]
    assert str(root) not in json.dumps(manifest)
    staged_notice = (destination / "ripgrep-notices.txt").read_text(encoding="utf-8")
    assert staged_notice == "test notice fixture"
    from scripts.smoke_packaged_runtime import validate_search_bundle
    validate_search_bundle(destination / "gitgo-host.exe")
    target.write_bytes(b"corrupt fixture")
    with pytest.raises(ValueError, match="integrity mismatch"):
        validate_search_bundle(destination / "gitgo-host.exe")


def test_fallback_globs_do_not_widen_a_single_directory_pattern(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    monkeypatch.setattr(adapter, "resolve_ripgrep", lambda: None)
    (root / "src" / "nested").mkdir(parents=True)
    (root / "src" / "a.py").write_text("needle", encoding="utf-8")
    (root / "src" / "nested" / "b.py").write_text("needle", encoding="utf-8")
    assert [r["file"] for r in query(root, include=["src/*.py"])["matches"]] == ["src/a.py"]
    assert len(query(root, include=["src/**/*.py"])["matches"]) == 2
    assert len(query(root, exclude=["nested"])["matches"]) == 1


def test_partial_timeout_never_becomes_empty_success(tmp_path_factory, rg_available, monkeypatch):
    monkeypatch.setattr(adapter, "stream_process", lambda *args: adapter.StreamOutcome(stopped="timeout"))
    result = query(tmp_path_factory)
    assert result["error"] == "SEARCH_INCOMPLETE" and not result["complete"]
    assert result["partial"] and result["next_offset"] is None


def test_untrusted_engine_json_is_rejected_with_recovery(tmp_path_factory, rg_available, monkeypatch):
    def malformed(argv, cwd, separator, consume, deadline):
        assert consume(b'[]') is False
        return adapter.StreamOutcome(stopped="page_limit")
    monkeypatch.setattr(adapter, "stream_process", malformed)
    result = query(tmp_path_factory)
    assert result["error"] == "SEARCH_INCOMPLETE"
    assert any(w["code"] == "SEARCH_RESULT_INVALID" for w in result["warnings"])


def test_agent_loop_sends_fallback_notice_to_public_stream(tmp_path_factory, monkeypatch, runner_transport_only):
    from types import SimpleNamespace
    from backend.core.loop.executor import agent_step
    from backend.core.loop.models import RingLevel
    from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.outcome import TaskOutcome, OutcomeStatus
    from backend.core.loop.provider_protocol import ProviderEvent, ProviderEventType
    from backend.core.tools.catalog import build_workspace_tools
    root = tmp_path_factory
    (root / "a.txt").write_text("needle", encoding="utf-8")
    monkeypatch.setenv("GITGO_RIPGREP_PATH", str(root / "missing-rg.exe"))
    p = AgentRuntimeFactory.create(RuntimeSpec(role="worker", actor_kind="worker",
        capability_profile_id="governance.observe", ring_level=RingLevel.RING_3,
        tool_registry=ToolRegistry(["search_text", "request_user_decision", "request_permission"]),
        task_kind="answer", task_id="search-notice", workspace_path=str(root), max_steps=4))

    class Provider:
        protocol = SimpleNamespace(value="openai_responses")
        capabilities = SimpleNamespace(prompt_cache="automatic")
        context_window = 8192
        calls = 0

        def stream_events(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ProviderEvent(ProviderEventType.TOOL_CALL_STARTED, tool_call_id="search",
                                    tool_name="search_text", output_index=0)
                yield ProviderEvent(ProviderEventType.TOOL_CALL_DONE, tool_call_id="search", tool_name="search_text",
                                    output_index=0, arguments=json.dumps({"path": "a.txt", "pattern": "needle", "literal": True}))
            else:
                yield ProviderEvent(ProviderEventType.TEXT_DELTA, text="Found the line using the fallback.\nTASK_COMPLETE")
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED)

    events = []
    outcome = TaskOutcome.from_dict(agent_step(p, Provider(), instruction="Find needle in a.txt",
        dispatcher=SimpleNamespace(_executors=build_workspace_tools(root)), workspace_path=str(root), on_stream_event=events.append))
    assert outcome.status == OutcomeStatus.COMPLETED, outcome.to_dict()
    assert any(e.get("event") == "progress_summary" and e.get("code") == "SEARCH_FALLBACK" for e in events)
    assert any(r.get("search_engine") == "python" for r in p.tool_receipts)


def test_explicit_generated_workspace_root_is_not_mistaken_for_a_generated_descendant(tmp_path_factory, rg_available):
    root = tmp_path_factory / ".gitgo" / "worktrees" / "task"
    root.mkdir(parents=True)
    (root / "app.py").write_text("needle", encoding="utf-8")
    assert query(root)["matches"][0]["file"] == "app.py"


def test_scope_rejects_a_forged_symlink_result_without_requiring_os_symlink_support(tmp_path_factory, monkeypatch):
    root = tmp_path_factory / "scope"
    root.mkdir()
    outside = tmp_path_factory / "outside.txt"
    outside.write_text("needle", encoding="utf-8")
    page = adapter.SearchPage("matches", 0, 10)
    assert adapter._checked_path(str(outside), root, page) is None
    assert page.warnings["SEARCH_SCOPE_SKIPPED"]


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_windows_directory_junction_does_not_expand_scope(tmp_path_factory, rg_available, monkeypatch, engine):
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows junction test")
    root = tmp_path_factory / "project"
    root.mkdir()
    outside = tmp_path_factory / "external"
    outside.mkdir()
    (outside / "external.txt").write_text("needle", encoding="utf-8")
    link = root / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                            capture_output=True, creationflags=0x08000000, timeout=5)
    if result.returncode:
        pytest.skip("junction creation unavailable")
    if engine == "python":
        monkeypatch.setattr(adapter, "resolve_ripgrep", lambda: None)
    assert query(root)["matches"] == []


def test_external_search_requires_exact_host_permission_and_keeps_absolute_identity(tmp_path_factory, rg_available, runner_transport_only):
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
    from backend.core.tools.catalog import build_workspace_tools
    root = tmp_path_factory / "project"
    outside = tmp_path_factory / "approved-external"
    root.mkdir()
    outside.mkdir()
    target = outside / "a.txt"
    target.write_text("needle", encoding="utf-8")
    p = AgentProcess(process_id="external-search", role="worker", ring_level=RingLevel.RING_3,
                     active_task_id="external-task", session=AgentSession(), worktree_path=str(root),
                     tool_registry=ToolRegistry(["search_text"]))
    catalog = build_workspace_tools(root)
    ctx = ExecutionContext(process=p, session=p.session, workspace_path=str(root), event_bus=EventBus())
    arguments = {"path": str(outside), "pattern": "needle", "literal": True}
    call = {"name": "search_text", "args": arguments}
    pipeline = ToolPipeline()
    denied = pipeline.execute(call, catalog["search_text"], ctx, "denied")
    assert denied.is_error and denied.diagnostics["code"] == "RESOURCE_SCOPE_APPROVAL_REQUIRED"
    decision = create_permission_request(p, {"tool_name": "search_text", "arguments": arguments,
        "resource": str(outside), "purpose": "inspect the declared external directory"}, catalog, str(root))
    assert grant_from_decision(p, decision, "allow_once")
    p.pending_decision = None
    allowed = pipeline.execute(call, catalog["search_text"], ctx, "approved")
    assert not allowed.is_error, allowed.formatted
    assert allowed.data["matches"][0]["file"] == str(target)
    assert allowed.receipt["search_complete"] is True
