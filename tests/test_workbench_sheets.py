"""Durable scratch revisions, provenance boundaries and actual tool transport."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from backend.core.storage import StorageRuntime
from backend.core.workbench.sheets import SheetStore, RevisionConflict
from backend.core.loop.protocol_tools.workbench import install, invoke


def notes(text="A hypothesis, not verified"):
    return [{"id": "hypothesis", "text": text}]


def store(runtime, session="s", process="p"):
    return SheetStore(runtime, session_id=session, process_id=process)


def test_many_sheets_revisions_and_explicit_historical_continuation_survive_restart(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as runtime:
        sheets = store(runtime)
        a = sheets.create("Investigation", notes("old"))
        b = sheets.revise(a["sheet_id"], a["revision"], notes("new"))
        c = sheets.create("A different line", [{"id": "next", "text": "continue here"}], continuation={
            "sheet_id": a["sheet_id"], "revision": a["revision"],
            "cell_ids": ["hypothesis"], "reason": "retain an earlier hypothesis explicitly"})
        independent = sheets.create("Unrelated", [])
        assert independent["transition"] is None
        assert c["transition"]["revision"] == a["revision"]
        assert sheets.open(c["sheet_id"])["cells"][0]["source"] == "old"
        assert sheets.open(a["sheet_id"], a["revision"])["version_state"] == "historical"
        assert b["sequence"] == 2 and b["parent_revision"] == a["revision"]
        roots = runtime._cas_reference_inventory().require_complete()
        assert {a["revision"].split(":")[1], b["revision"].split(":")[1], c["revision"].split(":")[1]} <= roots
    with StorageRuntime(root, state_home=root / "state") as reopened:
        sheets = store(reopened)
        assert len(sheets.list()["sheets"]) == 3
        assert sheets.open(a["sheet_id"])["revision"] == b["revision"]
        assert sheets.open(a["sheet_id"], a["revision"])["cells"][0]["source"] == "old"


def test_two_connections_cannot_commit_over_the_same_expected_head(tmp_path_factory):
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as left, StorageRuntime(root, state_home=root / "state") as right:
        a = store(left).create("Concurrent", notes())
        barrier = threading.Barrier(2)
        def revise(runtime, text):
            barrier.wait()
            try:
                return store(runtime).revise(a["sheet_id"], a["revision"], notes(text))
            except RevisionConflict:
                return "conflict"
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(revise, left, "left"), pool.submit(revise, right, "right")]
            results = [f.result() for f in futures]
        assert results.count("conflict") == 1
        winner = next(r for r in results if isinstance(r, dict))
        assert store(left).open(a["sheet_id"])["revision"] == winner["revision"]
        assert len(left.list_state_refs("workbench.v1", prefix=store(left).owner + "/revision/")) == 2


def test_atomic_reference_failure_rolls_back_revision_and_head(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        old = runtime.put_blob(b"old")
        new = runtime.put_blob(b"new")
        runtime.put_state_ref("test", "head", old)
        runtime._state.execute("CREATE TEMP TRIGGER reject_head BEFORE UPDATE ON storage_kv "
                               "WHEN NEW.key='head' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            runtime.commit_state_revision("test", "head", new, expected_ref=old, revision_key="revision")
        assert runtime.get_state_ref("test", "head") == old
        assert runtime.get_state_ref("test", "revision") is None
        assert not runtime._state.in_transaction


@pytest.mark.parametrize("other_session,other_process", [("other", "p"), ("s", "child")])
def test_scope_cannot_be_selected_by_model(tmp_path_factory, other_session, other_process):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        a = store(runtime).create("Private", notes())
        other = store(runtime, other_session, other_process)
        assert other.list()["sheets"] == []
        with pytest.raises(KeyError):
            other.open(a["sheet_id"], a["revision"])


@pytest.mark.parametrize("bad", [
    [{"id": "x", "text": "x", "verified": True}],
    [{"id": "x", "text": "x", "cell_type": "code"}],
    [{"id": "x", "text": "x"}, {"id": "x", "text": "y"}],
    [{"id": "../x", "text": "x"}], [{"id": "x", "text": " "}],
])
def test_notes_cannot_inject_authority_execute_code_or_repair_duplicate_ids(tmp_path_factory, bad):
    original = deepcopy(bad)
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        with pytest.raises(ValueError):
            store(runtime).create("Invalid", bad)
        assert store(runtime).list()["sheets"] == []
    assert bad == original


@pytest.mark.parametrize("revision", ["", None, "latest"])
def test_writes_and_continuations_require_explicit_pinned_revision(tmp_path_factory, revision):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets = store(runtime)
        a = sheets.create("Pinned", notes())
        with pytest.raises(ValueError):
            sheets.revise(a["sheet_id"], revision, notes("must not write"))
        with pytest.raises(ValueError):
            sheets.create("Must not continue", [], continuation={"sheet_id": a["sheet_id"],
                "revision": revision, "cell_ids": [], "reason": "test"})
        assert sheets.open(a["sheet_id"])["revision"] == a["revision"]


def test_pagination_and_literal_prefixes(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets = store(runtime)
        for title in ("One", "Two", "Three"):
            sheets.create(title, [{"id": f"n{i}", "text": str(i)} for i in range(4)])
        first = sheets.list(limit=2)
        second = sheets.list(limit=2, after=first["next_after"])
        assert len(first["sheets"]) == 2 and len(second["sheets"]) == 1
        assert not {s["sheet_id"] for s in first["sheets"]} & {s["sheet_id"] for s in second["sheets"]}
        a = first["sheets"][0]
        page = sheets.open(a["sheet_id"], offset=0, limit=2)
        assert page["next_offset"] == 2
        assert sheets.open(a["sheet_id"], offset=2, limit=2)["next_offset"] is None
        runtime.put_state_ref("wild", "a_1", a["revision"])
        runtime.put_state_ref("wild", "ab1", a["revision"])
        assert len(runtime.list_state_refs("wild", prefix="a_")) == 1


def test_tool_conflict_returns_canonical_help_and_preserves_current_notes(tmp_path_factory):
    from backend.core.loop.recovery_guidance import lookup_guidance
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets = store(runtime)
        a = invoke({"operation": "create_sheet", "title": "Test", "notes": notes()}, sheets)
        b = invoke({"operation": "revise_sheet", "sheet_id": a["sheet_id"],
                    "expected_revision": a["revision"], "notes": notes("second")}, sheets)
        conflict = invoke({"operation": "revise_sheet", "sheet_id": a["sheet_id"],
                    "expected_revision": a["revision"], "notes": notes("discard me")}, sheets)
        assert conflict["error"] == "WORKBENCH_REVISION_CONFLICT"
        assert conflict["error_info"]["state_changed"] is False
        assert conflict["error_info"]["details"]["current_revision"] == b["revision"]
        assert lookup_guidance(conflict["error_info"]["catalog_id"])["procedure"] == "workbench"
        assert sheets.open(a["sheet_id"])["cells"][0]["source"] == "second"


def test_arithmetic_available_without_bound_storage():
    tools = {}
    install(SimpleNamespace(tools=tools))
    assert tools["workbench"].execute({"operation": "calculate", "expression": "4*4"})["result"] == 16
    assert tools["workbench"].execute({"operation": "create_sheet", "title": "x"})["error"] == "WORKBENCH_UNAVAILABLE"


def test_tail_references_recover_after_compaction_without_promoting_note_content(tmp_path_factory):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.turn_guidance import _build_current_turn_envelope
    from backend.core.loop.protocol_tools.workbench import bound_sheets
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        process = AgentProcess(process_id="p", role="worker", ring_level=RingLevel.RING_3,
                               session=AgentSession(session_id="s"), runtime_storage=runtime)
        a = bound_sheets(process).create("Title should not be injected", notes("SECRET_NOTE_BODY"))
        tools = [{"type": "function", "function": {"name": "workbench"}}]
        before = _build_current_turn_envelope(process, tools)
        process.session.messages.clear()
        process.session.context_epoch += 1
        after = _build_current_turn_envelope(process, tools)
        for text in (before, after):
            assert a["sheet_id"] in text and a["revision"] in text
            assert "SECRET_NOTE_BODY" not in text and "Title should not be injected" not in text
        assert a["sheet_id"] not in _build_current_turn_envelope(process, [])


def test_real_tool_pipeline_does_not_turn_scratch_writes_into_task_completion(tmp_path_factory):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.completion_protocol import HostCompletionEvidence
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        process = AgentProcess(process_id="pipeline", role="worker", ring_level=RingLevel.RING_3,
            session=AgentSession(), active_task_id="task", runtime_storage=runtime,
            worktree_path=str(tmp_path_factory), tool_registry=ToolRegistry(["workbench"]))
        tool = build_internal_tools(process, {}, str(tmp_path_factory), None, None, None)["workbench"]
        context = ExecutionContext(process=process, session=process.session,
                                   workspace_path=str(tmp_path_factory), storage=runtime)
        result = ToolPipeline().execute({"name": "workbench", "args": {
            "operation": "create_sheet", "title": "Execution is still unproven", "notes": notes()}},
            tool, context, "scratch-write")
        assert not result.is_error
        assert result.data["authority"] == "working_material_only"
        assert result.receipt["effect"] == "read"
        process.tool_receipts.append(result.receipt)
        evidence = HostCompletionEvidence.collect(process)
        assert evidence.action_receipt_ids == () and not evidence.factual_ready
        assert not list(tmp_path_factory.glob("*.ipynb"))
        assert runtime.load_harvest_signal_records() == []


def test_fresh_python_process_restores_exact_revision(tmp_path_factory):
    import subprocess
    import sys
    from pathlib import Path
    root = tmp_path_factory
    with StorageRuntime(root, state_home=root / "state") as runtime:
        a = store(runtime).create("Across restart", notes("survives"))
    code = """import json,sys
sys.path[:0]=json.loads(sys.argv[1])
from pathlib import Path
from backend.core.storage import StorageRuntime
from backend.core.workbench.sheets import SheetStore
root=Path(sys.argv[2])
with StorageRuntime(root,state_home=root/'state') as runtime:
    value=SheetStore(runtime,session_id='s',process_id='p').open(sys.argv[3],sys.argv[4])
    print(json.dumps(value))
"""
    completed = subprocess.run([sys.executable, "-c", code, json.dumps(sys.path), str(root),
        a["sheet_id"], a["revision"]], capture_output=True, text=True, timeout=30,
        cwd=str(root))
    assert completed.returncode == 0, completed.stderr
    value = json.loads(completed.stdout)
    assert value["revision"] == a["revision"]
    assert value["cells"][0]["source"] == "survives"


def test_missing_nbformat_fails_explicitly_without_publishing_a_sheet(tmp_path_factory, monkeypatch):
    import backend.core.workbench.sheets as sheets_module
    def unavailable(_):
        raise ImportError("document dependency missing")
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        monkeypatch.setattr(sheets_module, "_validate", unavailable)
        sheets = store(runtime)
        result = invoke({"operation": "create_sheet", "title": "Unavailable", "notes": notes()}, sheets)
        assert result["error"] == "WORKBENCH_UNAVAILABLE"
        assert sheets.list()["sheets"] == []
        assert invoke({"operation": "calculate", "expression": "2+2"}, sheets)["result"] == 4


def test_main_session_keeps_sheets_when_runtime_replaces_the_turn_process(tmp_path_factory):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.turn_guidance import _build_current_turn_envelope
    session = AgentSession(session_id="durable-main-session")
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        def make(process_id, *, parent=None, current_session=session):
            process = AgentProcess(process_id=process_id, role="worker", ring_level=RingLevel.RING_3,
                session=current_session, active_task_id=process_id, runtime_storage=runtime, parent_id=parent)
            tool = build_internal_tools(process, {}, str(tmp_path_factory), None, None, None)["workbench"]
            return process, tool
        first, first_tool = make("first-turn")
        created = first_tool.execute({"operation": "create_sheet", "title": "Across turns",
                                      "notes": [{"id": "finding", "text": "Keep exact earlier evidence"}]})
        second, second_tool = make("second-turn")
        opened = second_tool.execute({"operation": "open_sheet", "sheet_id": created["sheet_id"], "revision": created["revision"]})
        assert opened["cells"][0]["source"] == "Keep exact earlier evidence"
        tail = _build_current_turn_envelope(second, [{"type": "function", "function": {"name": "workbench"}}])
        assert created["sheet_id"] in tail
        # A child cannot adopt the main session's ownership, even if it has
        # the same session id. A different main session remains isolated too.
        _, child = make("child", parent="second-turn")
        assert child.execute({"operation": "open_sheet", "sheet_id": created["sheet_id"]})["error"] == "WORKBENCH_INVALID_INPUT"
        restored_session = AgentSession.from_durable_state({"session_id": session.session_id})
        _, restored = make("after-restart", current_session=restored_session)
        assert restored.execute({"operation": "open_sheet", "sheet_id": created["sheet_id"]})["revision"] == created["revision"]
        _, other = make("other-turn", current_session=AgentSession())
        assert other.execute({"operation": "list_sheets"})["sheets"] == []
