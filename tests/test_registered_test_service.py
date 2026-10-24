"""Registered tests cannot publish files or their own authoritative manifest."""
from pathlib import Path
from types import SimpleNamespace
from backend.core.tools.registrations import _run_test_isolated


def test_native_test_helper_never_writes_authoritative_manifest(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "test_sample.py").write_text("def test_ok(): assert True\n", encoding="utf8")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="1 passed", stderr=""))
    result = _run_test_isolated({"_workspace": str(ws), "test_id": "unit:sample", "target": "test_sample.py"})
    assert result["seed_results"][0]["exit_code"] == 0
    assert not (ws / ".gitgo/test_manifest.json").exists()


import json
import hashlib
import pytest
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.test_manifest import TestManifest
from backend.core.tools.catalog import build_workspace_tools
from tests.test_file_feedback_contract import context
from tests.test_alpha3_native_source import source_runtime
from tests.test_native_sandbox import isolated_python
from tests.native_source_runtime import workspace_native_launcher


def call(ws, ctx=None, **overrides):
    ctx = ctx or context(ws, ["run_test"])
    ctx.cancellation = ctx.process.cancellation_event
    tool = build_workspace_tools(ws)["run_test"]
    tool.timeout = 35  # Bound the real OS regression, including CreateProcess.
    args = {"test_id": "unit:sample", "target": "test_sample.py", "seeds": [11, 22], **overrides}
    result = ToolPipeline().execute({"name": tool.name, "args": args}, tool, ctx, "registered-test")
    return ctx, result


def protocol(monkeypatch, action=None, output=None):
    def invoke(self, name, args, timeout, **kw):
        assert name == "run_test"
        if action:
            action(Path(args["_workspace"]), args)
        kw["runtime_metadata"].update(launch_state="started", exit_code=0, cleanup={"complete": True, "mode": "protocol-fixture"})
        return output if output is not None else {"seed_results": [{"seed": seed, "exit_code": 0, "duration_ms": 1.0, "output_tail": "passed"} for seed in args["seeds"]]}
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.prepare", lambda self: self)
    monkeypatch.setattr("backend.core.sandbox_access.WorkspaceAccess.close", lambda self: None)


def test_service_discards_test_files_and_only_parent_records_manifest(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "test_sample.py").write_text("def test_ok(): assert True\n", encoding="utf8")
    def write(stage, args):
        assert stage != ws and not stage.is_relative_to(ws)
        assert not (stage / ".gitgo/test_manifest.json").exists()
        (stage / "transient.txt").write_text("discard")
    protocol(monkeypatch, write)
    _, result = call(ws)
    assert not result.is_error, result.formatted
    assert result.receipt["test_completed"] and result.receipt["test_passed"]
    assert result.receipt["test_seeds"] == [11, 22]
    assert result.receipt["runtime_cleanup"]["complete"]
    assert not (ws / "transient.txt").exists()
    assert TestManifest.load(ws).evaluate_required(["unit:sample"])[0]


def test_private_source_rewrite_cannot_claim_current_validation(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "test_sample.py").write_text("def test_bad(): assert False\n", encoding="utf8")
    protocol(monkeypatch, lambda stage, _: (stage / "test_sample.py").write_text("def test_good(): assert True\n"))
    _, result = call(ws)
    assert result.is_error and result.diagnostics["code"] == "FILE_CHANGED", result.formatted
    assert result.receipt["effect_state"] == "not_committed"
    assert not TestManifest.load(ws).records


def test_external_input_change_keeps_exit_but_invalidates_version(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "test_sample.py").write_text("def test_ok(): assert True\n", encoding="utf8")
    protocol(monkeypatch, lambda stage, _: (ws / "test_sample.py").write_text("external change\n"))
    _, result = call(ws)
    assert not result.is_error and result.data["passed"]
    assert result.data["verification_state"] == "needs_recheck"
    assert not TestManifest.load(ws).evaluate_required(["unit:sample"])[0]


def test_malformed_seed_protocol_never_registers_result(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "test_sample.py").write_text("def test_ok(): assert True\n", encoding="utf8")
    protocol(monkeypatch, output={"seed_results": [{"seed": 99, "exit_code": 0, "duration_ms": 1, "output_tail": "forged seed"}]})
    _, result = call(ws)
    assert result.is_error and not result.receipt.get("test_completed")
    assert result.diagnostics["error_info"]["catalog_id"]=="GITGO-E3816"
    assert result.receipt["effect_state"] == "not_committed"
    assert not TestManifest.load(ws).records


@pytest.fixture
def pytest_native_runtime(workspace_native_launcher):
    import importlib.util, shutil
    runtime = workspace_native_launcher
    for name in ("pytest", "_pytest", "pluggy", "packaging", "iniconfig", "pygments", "colorama", "py"):
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        if spec.submodule_search_locations:
            shutil.copytree(next(iter(spec.submodule_search_locations)), runtime / name,
                dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(spec.origin, runtime / Path(spec.origin).name)
    import os
    if os.environ.get("GITGO_TEST_NATIVE_TRACE") == "1":
        probe = runtime / "backend/core/tools/registered_test.py"
        probe.write_text(probe.read_text(encoding="utf8").replace(
            "    workspace = Path(workspace).resolve()",
            "    import faulthandler; faulthandler.dump_traceback_later(10)\n    workspace = Path(workspace).resolve()", 1).replace('"-q",', '"-q", "--full-trace",').replace('output[-4000:]', 'output[-16000:]'), encoding="utf8")
    return runtime


@pytest.mark.parametrize("temporary_fixture", [False, True])
def test_real_seeded_pytest_cannot_write_formal_workspace(tmp_path_factory, pytest_native_runtime, temporary_fixture):
    ws = tmp_path_factory
    (ws / "input.txt").write_bytes(b"original")
    test = """from pathlib import Path
import os
def test_isolated():
    assert os.environ['GITGO_TEST_SEED'] in ('11','22')
    assert Path('input.txt').read_bytes() == b'original'
    try:
        Path(FORMAL).write_text('forbidden')
    except PermissionError:
        pass
    else:
        raise AssertionError('formal write unexpectedly succeeded')
    assert Path.cwd().parent.is_dir()
    try:
        list(Path.cwd().parent.iterdir())
    except PermissionError:
        pass
    else:
        raise AssertionError('execution parent listing unexpectedly allowed')
    Path('transient.txt').write_text('discard')
    Path('.gitgo/test_manifest.json').write_text('{"tests": [{"test_id": "fake", "passed": true}]}')
""".replace("FORMAL", repr(str(ws / "input.txt")))
    if temporary_fixture:
        test = test.replace("def test_isolated():", "def test_isolated(tmp_path, tmp_path_factory):\n    (tmp_path / 'value.txt').write_text('fixture output')\n    assert tmp_path_factory.getbasetemp().is_relative_to(Path.cwd())\n    tmp_path_factory.mktemp('explicit', numbered=False)\n    try:\n        tmp_path_factory.mktemp('../escape')\n    except ValueError:\n        pass\n    else:\n        raise AssertionError('temp factory escaped its private root')")
    (ws / "test_sample.py").write_text(test, encoding="utf8")
    _, result = call(ws)
    assert not result.is_error, result.formatted + json.dumps(result.receipt.get("native_producers"), ensure_ascii=True)
    assert result.receipt["test_completed"] and result.receipt["test_passed"]
    assert all("private pytest class" not in row["output_tail"] for row in result.data["seed_results"])
    assert (ws / "input.txt").read_bytes() == b"original"
    assert not (ws / "transient.txt").exists()
    assert TestManifest.load(ws).evaluate_required(["unit:sample"])[0]
    assert "fake" not in TestManifest.load(ws).records
    producer = result.receipt["native_producers"][0]
    assert producer["launch_state"] == "started" and producer["access_retired"]


def test_cancel_after_test_never_records_manifest(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "test_sample.py").write_text("def test_ok(): assert True\n", encoding="utf8")
    ctx = context(ws, ["run_test"])
    protocol(monkeypatch, lambda stage, _: ctx.process.cancellation_event.set())
    _, result = call(ws, ctx)
    assert result.is_error and result.data["error"] == "TOOL_CANCELLED", result.formatted
    assert result.receipt["effect_state"] == "not_committed"
    assert not TestManifest.load(ws).records


def test_unknown_test_cleanup_prevents_manifest_record_and_next_native(tmp_path_factory, monkeypatch):
    ws = tmp_path_factory
    (ws / "test_sample.py").write_text("def test_ok(): assert True\n", encoding="utf8")
    stages=[]
    def invoke(self, name, args, timeout, **kw):
        stages.append(Path(args["_workspace"]))
        kw["runtime_metadata"].update(launch_state="started",exit_code=0,cleanup={"complete":False,"mode":"protocol_unknown"})
        return {"seed_results": [{"seed": s,"exit_code":0,"duration_ms":1,"output_tail":"passed"} for s in args["seeds"]]}
    protocol(monkeypatch)
    monkeypatch.setattr(ToolPipeline, "_execute_isolated", invoke)
    ctx=context(ws,["run_test"])
    try:
        _,result=call(ws,ctx)
        assert result.data["error"]=="SANDBOX_CLEANUP_PENDING",result.formatted
        assert not TestManifest.load(ws).records and stages[0].exists()
        assert result.receipt["effect_state"]=="not_committed"
        _,again=call(ws,ctx)
        assert again.is_error and len(stages)==1
    finally:
        # Explicit protocol fault: no native process/ACL was created.
        from backend.core.loop.read_approval import _RETAINED_ADMISSIONS
        for owner in _RETAINED_ADMISSIONS.pop((ctx.process.process_id,"registered-test",0),[]):
            owner.close()
