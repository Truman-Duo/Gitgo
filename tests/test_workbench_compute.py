from decimal import Decimal
import os
from pathlib import Path

import pytest

from backend.core.loop.agent_tool import ApprovalMode, ToolEffect
from backend.core.loop.context_store import ContextObjectStore
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
from backend.core.loop.protocol_tools.registry import build_internal_tools
from backend.core.loop.protocol_tools.workbench import bound_sources
from backend.core.loop.session import AgentSession
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.storage import StorageRuntime
from backend.core.workbench import code_size
from backend.core.workbench.compute_runtime import WorkbenchComputeRuntime
from backend.core.workbench.data import parse_json
from backend.core.workbench.python_compute import execute_python
from tests.native_source_runtime import workspace_native_launcher
from tests.test_native_sandbox import isolated_python


@pytest.fixture
def runtime(tmp_path_factory):
    workspace = tmp_path_factory / "project"
    workspace.mkdir()
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as storage:
        process = AgentProcess(process_id="workbench-compute", role="worker", ring_level=RingLevel.RING_3,
            session=AgentSession(), active_task_id="calculation", runtime_storage=storage,
            worktree_path=str(workspace), tool_registry=ToolRegistry(["workbench_compute", "workbench"]))
        tools = build_internal_tools(process, {}, str(workspace), None, None, None)
        tool = tools["workbench_compute"]
        ctx = ExecutionContext(process, process.session, str(workspace), storage=storage,
                               cancellation=process.cancellation_event)
        pipeline = ToolPipeline()

        def run(args, *, grant=True):
            if grant:
                create_permission_request(process, {"tool_name": tool.name, "arguments": args,
                    "purpose": "Run the exact disposable calculation fixture"}, {tool.name: tool}, str(workspace))
                assert grant_from_decision(process, process.pending_decision, "allow_once")
                process.pending_decision = None
            process.steps_used += 1
            return pipeline.execute({"name": tool.name, "args": args}, tool, ctx,
                                    f"compute-{process.steps_used}", 0)

        yield ctx, tool, run, bound_sources(process, workspace), workspace


def _frame(runtime, result):
    runtime.last_render = {"launch_state": "started", "exit_code": 0, "access_retired": True,
                           "cleanup": {"complete": True}}
    return {"result": result, "producer": runtime.last_render}


def _load(ctx, sources, result):
    assert not result.is_error, result.formatted
    return parse_json(sources._read_result(ctx.storage, result.data["result_locator"], ctx.process.process_id))


def _import(sources, sheet, result, cell_id):
    return sources.import_result(sheet["sheet_id"], sheet["revision"], cell_id=cell_id,
                                 locator=result.data["result_locator"])


def test_named_python_inputs_outputs_scope_and_stale_provenance(runtime, monkeypatch):
    ctx, tool, run, sources, workspace = runtime
    assert tool.effect == ToolEffect.PROCESS
    assert not tool.read_only and tool.approval == ApprovalMode.ASK and tool.approval_per_invocation
    references = ContextObjectStore(workspace)
    source = references.put("example/source", {"rows": [{"kind": "a", "cost": 0.1},
                                                        {"kind": "a", "cost": 0.2},
                                                        {"kind": "b", "cost": 0.5}]})
    sheet = sources.sheets.create("Source", [])
    imported = sources.import_context(sheet["sheet_id"], sheet["revision"], cell_id="rows", source_ref=source["pinned"])
    cell = {k: imported[k] for k in ("sheet_id", "revision", "cell_id")}
    launches = []

    def preview(self, handler, args, timeout, *, copy_inputs, **kwargs):
        assert handler == "workbench_python" and copy_inputs is False
        assert 0 < timeout <= tool.timeout
        launches.append(args)
        return _frame(self, execute_python({**args, "_workspace": str(workspace.parent)}))

    monkeypatch.setattr(WorkbenchComputeRuntime, "preview", preview)
    args = {"operation": "python", "inputs": [{"name": "rows", "cell": cell, "selector": "/rows"}],
            "outputs": [{"name": "groups", "unit": "USD"}, {"name": "estimate"}],
            "code": "from collections import defaultdict\n"
                    "def run(inputs):\n"
                    "    groups = defaultdict(int)\n"
                    "    for row in inputs['rows']:\n"
                    "        groups[row['kind']] += row['cost']\n"
                    "    return {'groups': dict(groups), 'estimate': float(groups['a']) / 7}\n"}
    denied = run(args, grant=False)
    assert not launches and (denied.is_error or denied.suspended)
    first = run(args)
    original = _load(ctx, sources, first)
    assert original["outputs"]["groups"] == {"value": {"a": Decimal("0.3"), "b": Decimal("0.5")},
                                              "unit": "USD", "numeric_modes": ["decimal"]}
    assert original["outputs"]["estimate"]["numeric_modes"] == ["binary_float"]
    next_call = run({"operation": "python",
        "inputs": [{"name": "groups", "locator": first.data["result_locator"], "selector": "/outputs/groups/value"}],
        "outputs": [{"name": "series"}],
        "code": "def run(inputs):\n    return {'series': [inputs['groups']['b'] - inputs['groups']['a']]}\n"})
    assert _load(ctx, sources, next_call)["outputs"]["series"]["value"] == [Decimal("0.2")]
    received = _import(sources, imported, next_call, "transformed")
    derived = sources.aggregate(received["sheet_id"], received["revision"], cell_id="delta",
        source_cell_id="transformed", selector="/outputs/series/value", reducer="sum")
    assert derived["result"]["value"] == "0.2"
    assert derived["applicability"]["state"] == "current"
    references.put("example/source", {"rows": []})
    assert sources.read_result(derived["result_ref"])["applicability"]["reason"] == "source_version_changed"


def test_cancelled_or_failed_python_does_not_publish(runtime, monkeypatch):
    ctx, _, run, _, workspace = runtime
    args = {"operation": "python", "inputs": [], "outputs": [{"name": "value"}],
            "code": "def run(inputs):\n    return {'value': 1}\n"}

    def fail(self, *args, **kwargs):
        return _frame(self, {"error": "WORKBENCH_INVALID_DATA", "detail": "fixture failure"})

    monkeypatch.setattr(WorkbenchComputeRuntime, "preview", fail)
    failed = run(args)
    assert failed.is_error and "result_locator" not in (failed.data or {})

    def cancel(self, handler, arguments, *args, **kwargs):
        ctx.cancellation.set()
        return _frame(self, execute_python({**arguments, "_workspace": str(workspace.parent)}))

    monkeypatch.setattr(WorkbenchComputeRuntime, "preview", cancel)
    cancelled = run(args)
    assert cancelled.is_error and "result_locator" not in (cancelled.data or {})


def test_real_windows_python_can_only_keep_named_outputs(runtime, workspace_native_launcher):
    ctx, _, run, sources, workspace = runtime
    formal = workspace / "formal.txt"
    formal.write_text("formal project", encoding="utf-8")
    args = {"operation": "python", "inputs": [], "outputs": [{"name": "result"}],
            "code": "from pathlib import Path\n"
                    "def run(inputs):\n"
                    "    Path('private.txt').write_text('temporary')\n"
                    "    try:\n"
                    f"        Path({str(formal)!r}).write_text('forbidden')\n"
                    "    except (PermissionError, FileNotFoundError):\n"
                    "        return {'result': 42}\n"
                    "    raise RuntimeError('formal file was writable')\n"}
    result = run(args)
    assert _load(ctx, sources, result)["outputs"]["result"]["value"] == 42
    assert formal.read_text(encoding="utf-8") == "formal project"
    assert not (workspace / "private.txt").exists()
    assert result.receipt["runtime_cleanup"]["complete"] is True


def test_git_inventory_uses_cached_paths_and_opt_in_nonignored_untracked(monkeypatch, tmp_path_factory):
    from backend.core.loop.worktree import AgentWorktreeManager
    root = tmp_path_factory
    (root / "source.py").write_bytes(b"# working copy")
    (root / "new.py").write_bytes(b"# new")
    (root / "dist").mkdir()
    (root / "dist" / "generated.py").write_bytes(b"# generated")
    calls = []

    def git_bytes(cwd, *args, **kwargs):
        assert cwd == root
        calls.append(args)
        return b"source.py\0missing.py\0dist/generated.py\0" + (b"new.py\0" if "--others" in args else b"")

    monkeypatch.setattr(AgentWorktreeManager, "_git_bytes", git_bytes)
    files, skipped = code_size._capture(root, root, [], True, False, lambda: None)
    assert files["source.py"][1] == b"# working copy"
    assert set(files) == {"source.py"} and skipped == {"missing.py": "missing"}
    files, _ = code_size._capture(root, root, [], True, True, lambda: None)
    assert set(files) == {"source.py", "new.py"} and "--exclude-standard" in calls[-1]


def test_real_cloc_rows_reused_and_changed_scan_is_not_stable(runtime, monkeypatch):
    perl, script = os.environ.get("GITGO_TEST_CLOC_PERL"), os.environ.get("GITGO_TEST_CLOC_SCRIPT")
    if not perl or not script:
        pytest.skip("Requires an explicitly provisioned official cloc 2.10 script and Perl")
    assert code_size.hashlib.sha256(Path(script).read_bytes()).hexdigest() == code_size.CLOC_SHA256
    ctx, _, run, sources, workspace = runtime
    all_counted = workspace.parent / "cloc-all-counted"
    image, mapping = code_size._image({"source.py": (None, b"print(42)\n")})
    all_counted.mkdir()
    image.materialize(all_counted)
    report = code_size._run_cloc([perl, script], all_counted, list(mapping))
    assert report["counts"].keys() == mapping.keys()
    assert report["unprocessed"] == {}

    first_file = workspace / "a 名字.py"
    first_file.write_text('# comment\nprint("#")\n', encoding="utf-8")
    (workspace / "b space.py").write_bytes(first_file.read_bytes())
    (workspace / "source.js").write_text('// comment\nconst text = "//";\n', encoding="utf-8")
    (workspace / "empty.py").touch()
    (workspace / "unknown.zzzz").write_text("unknown\n", encoding="utf-8")
    (workspace / "build").mkdir()
    (workspace / "build" / "generated.py").write_text("generated = True\n", encoding="utf-8")
    previews = []
    monkeypatch.setattr(code_size, "cloc_command", lambda: [perl, script])

    def preview(self, handler, args, timeout, *, copy_inputs, input_image=None, **kwargs):
        if handler == "workbench_python":
            return _frame(self, execute_python({**args, "_workspace": str(workspace.parent)}))
        assert handler == "workbench_cloc" and copy_inputs is False
        directory = workspace.parent / f"cloc-view-{len(previews)}"
        directory.mkdir()
        input_image.materialize(directory)
        outcome = code_size._run_cloc([perl, script], directory, args["staged_paths"])
        previews.append(outcome)
        if len(previews) == 2:
            first_file.write_text("value = 1\n", encoding="utf-8")
        return _frame(self, outcome)

    monkeypatch.setattr(WorkbenchComputeRuntime, "preview", preview)
    args = {"operation": "code_size", "path": "."}
    first = run(args)
    record = _load(ctx, sources, first)
    assert record["snapshot_state"] == "stable"
    assert record["languages"] == {"JavaScript": {"files": 1, "blank": 0, "comment": 1, "code": 1},
                                  "Python": {"files": 2, "blank": 0, "comment": 2, "code": 2}}
    assert {row["path"] for row in record["files"]} == {"a 名字.py", "b space.py", "source.js"}
    assert "zero sized" in record["unprocessed"]["empty.py"]
    assert "language unknown" in record["unprocessed"]["unknown.zzzz"]
    sheet = _import(sources, sources.sheets.create("Code size", []), first, "scan")
    aggregate = sources.aggregate(sheet["sheet_id"], sheet["revision"], cell_id="total",
        source_cell_id="scan", selector="/files", field="/code", reducer="sum")
    assert aggregate["result"]["value"] == "3"
    assert aggregate["applicability"]["state"] == "current"
    changed = _load(ctx, sources, run(args))
    assert changed["snapshot_state"] == "unstable"
    assert changed["changed_during_scan"] == ["a 名字.py"]
    assert sources.read_result(aggregate["result_ref"])["applicability"]["state"] == "needs_recheck"
    latest = run(args)
    rescanned = _load(ctx, sources, latest)
    assert rescanned["snapshot_state"] == "stable"
    assert rescanned["languages"]["Python"]["comment"] == 1
    old = sources.read_result(aggregate["result_ref"])
    assert old["result"]["value"] == "3"
    assert old["applicability"]["reason"] == "source_version_changed"
    delta = run({"operation": "python", "inputs": [
        {"name": "before", "locator": first.data["result_locator"], "basis": "historical",
         "selector": "/languages/Python/comment"},
        {"name": "after", "locator": latest.data["result_locator"], "selector": "/languages/Python/comment"}],
        "outputs": [{"name": "delta", "unit": "lines"}],
        "code": "def run(inputs):\n    return {'delta': [inputs['after'] - inputs['before']]}\n"})
    assert _load(ctx, sources, delta)["outputs"]["delta"]["value"] == [Decimal(-1)]
    assert delta.data["snapshot_state"] == "recorded"
    imported = _import(sources, aggregate, delta, "comment_delta")
    result = sources.aggregate(imported["sheet_id"], imported["revision"], cell_id="net_delta",
        source_cell_id="comment_delta", selector="/outputs/delta/value", reducer="sum")
    from backend.core.workbench.delivery import associate
    claim = associate(ctx.process, sources, scope="Python comment line change",
        result_ref=result["result_ref"], candidate="```text\nPython comment line change: -1\n```")
    assert claim["status"] == "match"
