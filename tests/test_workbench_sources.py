"""Bound exact computations to stored inputs without promoting source semantics."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.core.loop.context_store import ContextObjectStore
from backend.core.loop.protocol_tools.workbench import invoke, install
from backend.core.storage import StorageRuntime
from backend.core.workbench.sheets import SheetStore
from backend.core.workbench.sources import SourceWorkbench, InvalidData
from backend.core.workbench.data import aggregate, parse_json, snapshot_json


def setup(root, runtime, context=None):
    sheets = SheetStore(runtime, session_id="sources", process_id="p", owner_scope="main_session")
    contexts = ContextObjectStore(root)
    service = SourceWorkbench(sheets, contexts, lambda: context if context is not None else {})
    sheet = sheets.create("Source investigation", [{"id": "hypothesis", "text": "not a verified fact"}])
    return sheets, contexts, service, sheet


def imported(service, sheet, ref, cell_id="data"):
    return service.import_context(sheet["sheet_id"], sheet["revision"], cell_id=cell_id, source_ref=ref)


def calculated(service, sheet, cell_id="total", source="data", reducer="sum", selector="/rows", field="/amount", **kwargs):
    return service.aggregate(sheet["sheet_id"], sheet["revision"], cell_id=cell_id,
        source_cell_id=source, selector=selector, reducer=reducer, field=field, **kwargs)


def test_exact_recorded_decimal_sum_and_bound_coverage():
    result = aggregate('{"rows":[{"x":0.1},{"x":0.2},{"x":123456789012345678901234567890.123456789}]}',
                       selector="/rows", field="/x", reducer="sum")
    assert result["value"] == "123456789012345678901234567890.423456789"
    assert result["value_type"] == "decimal_string" and result["used_rows"] == 3
    assert "completeness" in result["coverage"]


def test_count_includes_null_and_duplicate_rows():
    result = aggregate('[null,1,1,"1",false]', selector="", reducer="count")
    assert result["value"] == 5 and result["duplicates"] == "retained"


def test_distinct_scalars_use_explicit_types_and_exact_numeric_equality():
    result = aggregate('[1,1.0,"1",true,false,0,-0.0,100,1e2]', selector="", reducer="distinct_count")
    assert result["value"] == 6


def test_null_and_absent_fields_are_not_silently_dropped():
    text = '[{"x":1},{"x":null},{},{"x":2}]'
    with pytest.raises(ValueError, match="null"):
        aggregate(text, selector="", field="/x", reducer="sum")
    result = aggregate(text, selector="", field="/x", reducer="sum", null_policy="skip")
    assert result["value"] == "3"
    assert result["skipped_null"] == 1 and result["skipped_missing"] == 1
    assert result["used_rows"] == 2 and result["input_rows"] == 4


@pytest.mark.parametrize("text,reducer", [('["12"]', "sum"), ('[true]', "min"),
    ('[[1]]', "distinct_count"), ('[{}]', "distinct_count"), ('[]', "max")])
def test_no_implicit_numeric_or_structural_coercion(text, reducer):
    with pytest.raises(ValueError):
        aggregate(text, selector="", reducer=reducer)


def test_pointer_escaping_and_empty_sum_are_explicit():
    result = aggregate('{"a/b":{"~key":[1,2]}}', selector="/a~1b/~0key", reducer="sum")
    assert result["value"] == "3"
    empty = aggregate('[]', selector="", reducer="sum")
    assert empty["value"] == "0" and empty["used_rows"] == 0
    with pytest.raises(ValueError, match="escape"):
        aggregate('[]', selector="", reducer="sum", field="/~bad")


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '[NaN]', '[Infinity]', '[1e101]', '[1e-101]',
    '[10000000000000000000000000000000000000000000000000000000000000000]',
    '[1.000000000000000000000000000001e100]', '[' * 34 + '0' + ']' * 34])
def test_invalid_json_and_numeric_bounds_fail_closed(text):
    with pytest.raises(ValueError):
        parse_json(text)


def test_snapshot_redaction_preserves_exact_nonsecret_numbers():
    text, changed = snapshot_json('{"password":"example-secret","rows":[0.1,0.2],"token":123}')
    assert changed and "example-secret" not in text
    value = json.loads(text)
    assert value["password"] == "[REDACTED]" and value["token"] == "[REDACTED]"
    assert aggregate(text, selector="/rows", reducer="sum")["value"] == "0.3"


def test_source_change_preserves_history_and_requires_explicit_refresh(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        source = contexts.put("test/data", {"rows": [{"amount": 2}, {"amount": 2}]})
        a = imported(service, sheet, source["latest"])
        b = calculated(service, a)
        assert b["result"]["value"] == "4" and b["applicability"]["state"] == "current"
        assert b["input"]["revision"] == a["revision"] and b["input"]["source_ref"] == source["pinned"]
        contexts.put("test/data", {"rows": [{"amount": 6}]})
        previous = sheets.open(b["sheet_id"])
        checked = service.check(b["sheet_id"])
        assert checked["cell_applicability"]["total"]["state"] == "needs_recheck"
        assert checked["cell_applicability"]["total"]["reason"] == "source_version_changed"
        assert sheets.open(b["sheet_id"]) == previous
        historical = calculated(service, b, cell_id="historical_again")
        assert historical["result"]["value"] == "4"
        assert historical["applicability"]["state"] == "needs_recheck"
        fresh = imported(service, historical, source["latest"], "data_new")
        final = calculated(service, fresh, cell_id="fresh_total", source="data_new")
        assert final["result"]["value"] == "6" and final["applicability"]["state"] == "current"
        states = service.check(final["sheet_id"])["cell_applicability"]
        assert states["total"]["state"] == "needs_recheck" and states["fresh_total"]["state"] == "current"
        assert json.loads(sheets.open(b["sheet_id"], b["revision"])["cells"][-1]["source"])["result"]["value"] == "4"


def test_unchanged_payload_with_changed_transitive_dependency_is_not_current(tmp_path_factory):
    context = {"evidence_sources": {"scan": {"digest": "scan1", "depends_on": {"files": "files1"}},
                                    "files": {"digest": "files1"}}}
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime, context)
        source = contexts.put("test/data", {"rows": [{"amount": 4}]},
                              metadata={"producer": "source_probe", "depends_on": {"scan": "scan1"}})
        a = imported(service, sheet, source["latest"])
        b = calculated(service, a)
        assert b["applicability"]["state"] == "current"
        context["evidence_sources"]["files"]["digest"] = "files2"
        states = service.check(b["sheet_id"])["cell_applicability"]
        assert states["total"]["state"] == "needs_recheck"
        assert states["total"]["source"] == "files"


@pytest.mark.parametrize("context,reason", [({}, "source_version_missing"),
    ({"evidence_sources": {"scan": {"digest": "one", "depends_on": {"scan": "one"}}}}, "dependency_cycle")])
def test_unknown_and_cyclic_dependencies_are_visible(tmp_path_factory, context, reason):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        _, contexts, service, sheet = setup(tmp_path_factory, runtime, context)
        ref = contexts.put("test/data", [1, 2], metadata={"depends_on": {"scan": "one"}})
        a = imported(service, sheet, ref["pinned"])
        assert a["applicability"]["state"] == "needs_recheck"
        assert a["applicability"]["reason"] == reason


def test_context_metadata_belongs_to_logical_ref_not_first_blob_writer(tmp_path_factory):
    contexts = ContextObjectStore(tmp_path_factory)
    a = contexts.put("test/a", [1], metadata={"producer": "a", "depends_on": {"alpha": "1"}})
    b = contexts.put("test/b", [1], metadata={"producer": "b", "depends_on": {"beta": "2"}})
    assert a["digest"] == b["digest"]
    info = contexts.reference_info(b["pinned"])
    assert info["producer"] == "b" and info["depends_on"] == {"beta": "2"}
    contexts.put("test/b", [2])
    info = contexts.reference_info(b["pinned"])
    assert info["dependency_metadata_state"] == "unavailable" and info["producer"] == "unknown"


def test_historical_import_cannot_invent_its_dependency_metadata(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        _, contexts, service, sheet = setup(tmp_path_factory, runtime)
        old = contexts.put("test/data", [1])
        contexts.put("test/data", [2])
        a = imported(service, sheet, old["pinned"])
        assert a["applicability"]["reason"] == "historical_dependency_metadata_unavailable"
        value = calculated(service, a, selector="", field="", reducer="sum")
        assert value["result"]["value"] == "1" and value["applicability"]["state"] == "needs_recheck"


def test_snapshot_survives_missing_original_context_for_historical_calculation(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        ref = contexts.put("test/data", [0.1, 0.2])
        a = imported(service, sheet, ref["pinned"])
        (contexts.blobs / (ref["digest"] + ".json")).unlink()
        b = calculated(service, a, selector="", field="")
        assert b["result"]["value"] == "0.3" and b["applicability"]["reason"] == "source_unavailable"
        assert sheets.open(b["sheet_id"])["revision"] == b["revision"]


def test_copying_result_without_its_bound_input_requires_recheck(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        ref = contexts.put("test/data", {"rows": [{"amount": 4}]})
        b = calculated(service, imported(service, sheet, ref["pinned"]))
        child = sheets.create("Result only", [], continuation={"sheet_id": b["sheet_id"],
            "revision": b["revision"], "cell_ids": ["total"], "reason": "Historical conclusion"})
        assert service.check(child["sheet_id"])["cell_applicability"]["total"]["reason"] == "calculation_input_not_present"
        complete = sheets.create("Inputs and result", [], continuation={"sheet_id": b["sheet_id"],
            "revision": b["revision"], "cell_ids": ["data", "total"], "reason": "Carry exact input"})
        assert service.check(complete["sheet_id"])["cell_applicability"]["total"]["state"] == "current"


def test_notes_cannot_be_used_as_observed_input_and_failure_keeps_head(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        with pytest.raises(InvalidData, match="not model notes"):
            calculated(service, sheet, source="hypothesis")
        assert sheets.open(sheet["sheet_id"])["revision"] == sheet["revision"]
        ref = contexts.put("test/data", ["4"])
        a = imported(service, sheet, ref["pinned"])
        invalid = invoke({"operation": "aggregate", "sheet_id": a["sheet_id"], "expected_revision": a["revision"],
            "cell_id": "bad", "source_cell_id": "data", "selector": "", "reducer": "sum"}, sheets, service)
        assert invalid["error"] == "WORKBENCH_INVALID_DATA" and not invalid["error_info"]["state_changed"]
        assert sheets.open(a["sheet_id"])["revision"] == a["revision"]


def test_source_payload_redaction_is_visible_and_keeps_provenance_separate(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        ref = contexts.put("test/data", {"password": "synthetic-secret", "rows": [{"amount": 1}]})
        a = imported(service, sheet, ref["latest"])
        opened = sheets.open(a["sheet_id"])
        raw = json.dumps(opened)
        assert "synthetic-secret" not in raw and a["redacted"] is True
        assert a["snapshot_sha256"] != ref["digest"]
        b = calculated(service, a)
        assert b["input"]["sha256"] == a["snapshot_sha256"] and b["input"]["redacted"] is True


def test_notes_revision_preserves_source_and_derived_cells(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        b = calculated(service, imported(service, sheet, contexts.put("test/data", {"rows": [{"amount": 4}]})["latest"]))
        revised = sheets.revise(b["sheet_id"], b["revision"], [{"id": "new_note", "text": "interpret later"}])
        assert {c["id"] for c in sheets.open(revised["sheet_id"])["cells"]} == {"new_note", "data", "total"}
        assert service.check(revised["sheet_id"])["cell_applicability"]["total"]["state"] == "current"


def test_source_warning_uses_real_pipeline_without_replaying_committed_result(tmp_path_factory):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.loop.completion_protocol import HostCompletionEvidence
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        process = AgentProcess(process_id="p", role="worker", ring_level=RingLevel.RING_3,
            session=AgentSession(session_id="sources"), active_task_id="task", runtime_storage=runtime,
            worktree_path=str(tmp_path_factory), tool_registry=ToolRegistry(["workbench"]))
        tool = build_internal_tools(process, {}, str(tmp_path_factory), None, None, None)["workbench"]
        ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory), storage=runtime)
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        ref = contexts.put("test/data", {"rows": [{"amount": 4}]})
        a = imported(service, sheet, ref["latest"])
        contexts.put("test/data", {"rows": [{"amount": 9}]})
        result = ToolPipeline().execute({"name": "workbench", "args": {"operation": "aggregate",
            "sheet_id": a["sheet_id"], "expected_revision": a["revision"], "cell_id": "total",
            "source_cell_id": "data", "selector": "/rows", "reducer": "sum", "field": "/amount"}},
            tool, ctx, "conditional-computation")
        assert not result.is_error and result.data["result"]["value"] == "4"
        assert result.data["warnings"][0]["catalog_id"] == "GITGO-W7601"
        assert result.data["revision"] != a["revision"]
        process.tool_receipts.append(result.receipt)
        assert not HostCompletionEvidence.collect(process).factual_ready
        assert any(e.get("catalog_id") == "GITGO-W7601" for e in process.session.host_ledger)
        opened = tool.execute({"operation": "open_sheet", "sheet_id": a["sheet_id"], "limit": 1})
        assert opened["cell_applicability"]["total"]["state"] == "needs_recheck"
        assert sheets.open(a["sheet_id"])["revision"] == result.data["revision"]


def test_large_inputs_do_not_get_silently_truncated():
    with pytest.raises(ValueError, match="120000"):
        parse_json('["' + 'x' * 120_000 + '"]')
    with pytest.raises(ValueError, match="10000"):
        aggregate('[' + ','.join('0' for _ in range(10001)) + ']', selector="", reducer="count")
    with pytest.raises(ValueError, match="20000"):
        parse_json('[' + ','.join('[]' for _ in range(20001)) + ']')


def test_same_payload_with_revised_producer_metadata_requires_recheck(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        _, contexts, service, sheet = setup(tmp_path_factory, runtime)
        ref = contexts.put("test/data", [1], metadata={"producer": "first"})
        a = imported(service, sheet, ref["latest"])
        ref2 = contexts.put("test/data", [1], metadata={"producer": "second"})
        assert ref["digest"] == ref2["digest"]
        assert service.check(a["sheet_id"])["cell_applicability"]["data"]["reason"] == "source_metadata_changed"


def test_untrusted_data_flags_are_not_promoted_to_notebook_authority(tmp_path_factory):
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        ref = contexts.put("test/data", {"verified": True, "approved": True, "rows": [1, 2]})
        a = imported(service, sheet, ref["latest"])
        source = sheets.open(a["sheet_id"])["cells"][-1]
        assert "verified" not in source["metadata"]["gitgo"]
        assert json.loads(source["source"])["verified"] is True
        assert a["authority"] == "working_material_only"
        assert runtime.load_harvest_signal_records() == []


def test_late_privacy_transformation_cannot_publish_wrong_input_or_query_identity(tmp_path_factory):
    from backend.core.workbench.data import verify_snapshots
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        sheets, contexts, service, sheet = setup(tmp_path_factory, runtime)
        a = imported(service, sheet, contexts.put("test/data", {"rows": [{"amount": 4}]})["latest"])
        b = calculated(service, a)
        opened = sheets.open(b["sheet_id"])
        cells = deepcopy(opened["cells"])
        cells[-1]["source"] = cells[-1]["source"].replace('"sum"', '"max"')
        with pytest.raises(ValueError, match="bound digest"):
            verify_snapshots({"cells": cells})
        cells = deepcopy(opened["cells"])
        cells[-2]["source"] = '{"rows":[{"amount":9}]}'
        with pytest.raises(ValueError, match="bound digest"):
            verify_snapshots({"cells": cells})
        assert sheets.open(b["sheet_id"])["revision"] == b["revision"]


def test_key_count_handles_objects_without_semantic_classification():
    value = aggregate('{"properties":{"a":{},"b":{},"c":{},"d":{}}}',
                      selector="/properties", reducer="key_count")
    assert value["value"] == 4 and value["collection_type"] == "object"
    assert value["input_members"] == 4 and "classification" in value["coverage"]
    with pytest.raises(ValueError, match="object"):
        aggregate('[1,2]', selector="", reducer="key_count")
    with pytest.raises(ValueError, match="field"):
        aggregate('{}', selector="", reducer="key_count", field="/x")


def test_full_registered_tool_sequence_imports_then_calculates_and_reports_bound_state(tmp_path_factory):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.protocol_tools.registry import build_internal_tools
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    with StorageRuntime(tmp_path_factory, state_home=tmp_path_factory / "state") as runtime:
        process = AgentProcess(process_id="registered", role="worker", ring_level=RingLevel.RING_3,
            session=AgentSession(), active_task_id="task", runtime_storage=runtime,
            worktree_path=str(tmp_path_factory), tool_registry=ToolRegistry(["workbench"]))
        tool = build_internal_tools(process, {}, str(tmp_path_factory), None, None, None)["workbench"]
        ctx = ExecutionContext(process=process, session=process.session, workspace_path=str(tmp_path_factory), storage=runtime)
        pipeline = ToolPipeline()
        def call(index, args):
            result = pipeline.execute({"name": "workbench", "args": args}, tool, ctx, index)
            assert not result.is_error, result.formatted
            return result.data
        record = ContextObjectStore(tmp_path_factory).put("test/record", {"rows": [1, 1, 2]})
        a = call("create", {"operation": "create_sheet", "title": "Sequential"})
        b = call("import", {"operation": "import_context", "sheet_id": a["sheet_id"],
            "expected_revision": a["revision"], "cell_id": "data", "source_ref": record["latest"]})
        c = call("count", {"operation": "aggregate", "sheet_id": b["sheet_id"],
            "expected_revision": b["revision"], "cell_id": "rows", "source_cell_id": "data",
            "selector": "/rows", "reducer": "count"})
        assert c["result"]["value"] == 3 and c["input"]["source_ref"] == record["pinned"]
        d = call("check", {"operation": "check_sources", "sheet_id": c["sheet_id"]})
        assert d["revision"] == c["revision"] and d["cell_applicability"]["rows"]["state"] == "current"
        assert runtime.load_harvest_signal_records() == []
