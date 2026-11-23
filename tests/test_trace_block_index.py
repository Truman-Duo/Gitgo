from __future__ import annotations

from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest

from backend.core.storage import StorageRuntime, get_storage
from backend.core.native_host import NativeHost


def _workspace(tmp_path_factory):
    path = tmp_path_factory / "workspace"
    path.mkdir()
    return path


def _append(storage, seq, event, process="A", **fields):
    result = storage.append_trace_record("trace", {"seq": seq, "event": event, "process_id": process,
        "time": "2026-11-08T00:00:00Z", **fields})
    assert result is not None


def test_index_compacts_only_adjacent_same_source_fragments_without_loading_bodies(tmp_path_factory):
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        _append(storage, 1, "reasoning_delta", delta="FIRST-PRIVATE-" * 200)
        _append(storage, 2, "reasoning_delta", delta="SECOND-PRIVATE-" * 200)
        _append(storage, 3, "reasoning_delta", process="B", delta="CHILD-PRIVATE-" * 200)
        _append(storage, 4, "reasoning_delta", delta="AFTER-CHILD")
        _append(storage, 5, "toolcall_start", tool_name="read_file", tool_call_id="read", accepted=False)
        _append(storage, 6, "reasoning_delta", delta="AFTER-TOOL")
        _append(storage, 7, "tool_result", tool_name="read_file", tool_call_id="read", is_error=True,
                result_preview="FULL-TOOL-BODY-" * 1000, diff="LARGE-DIFF-" * 500, error="failed")
        original = storage.read_trace_events("trace")
        page = storage.read_trace_index("trace")
        assert not page["has_more"]
        assert [(event["seq"], event["body_ref"]["last_seq"]) for event in page["events"]] == [(1, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7)]
        assert page["events"][3]["accepted"] is False
        assert page["events"][-1]["is_error"] is True
        encoded = json.dumps(page)
        for text in ("FIRST-PRIVATE", "SECOND-PRIVATE", "CHILD-PRIVATE", "FULL-TOOL-BODY", "LARGE-DIFF"):
            assert text not in encoded
        assert len(encoded) < 8000
        assert storage.read_trace_events("trace") == original


def test_tool_title_arguments_survive_index_without_reading_full_payload(tmp_path_factory):
    from backend.core.loop.tool_summary import tool_argument_preview
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        arguments={'path':'src/中文.ts','offset':10,'limit':20,'content':'BODY'*100000}
        stored=storage.append_trace_record('trace',{'seq':1,'event':'toolcall_done','process_id':'A',
                'tool_name':'read_file','tool_call_id':'read','argument_preview':tool_argument_preview(arguments)},
                detail={'arguments':arguments})
        assert stored is not None
        entry=storage.read_trace_index('trace')['events'][0]
        assert entry['argument_preview']=={'path':'src/中文.ts','offset':10,'limit':20}
        assert 'BODY' not in json.dumps(entry)


def test_block_body_uses_its_exact_range_and_bounded_cursor_without_losing_unicode(tmp_path_factory):
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        parts = ["第一段 👨‍👩‍👧‍👦", "组合 e\u0301\n", "末尾"]
        for seq, text in enumerate(parts, 1):
            _append(storage, seq, "reasoning_delta", delta=text)
        _append(storage, 4, "reasoning_delta", process="B", delta="OTHER-PROCESS")
        reference = storage.read_trace_index("trace")["events"][0]["body_ref"]
        first = storage.read_trace_block("trace", reference, limit=1)
        second = storage.read_trace_block("trace", reference, after_seq=first["next_seq"], limit=1)
        last = storage.read_trace_block("trace", reference, after_seq=second["next_seq"], limit=1)
        assert first["has_more"] and second["has_more"] and not last["has_more"]
        assert "".join(event["delta"] for page in (first, second, last) for event in page["events"]) == "".join(parts)
        assert all(event["process_id"] == "A" for page in (first, second, last) for event in page["events"])


def test_large_question_keeps_its_identity_in_the_compact_index(tmp_path_factory):
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        _append(storage, 1, "decision_required", decision={"decision_id": "original-question",
                "question": "saved original question" * 200, "options": []})
        event = storage.read_trace_index("trace")["events"][0]
        assert event["decision_id"] == "original-question"
        assert event["decision"] is None
        assert "saved original question" not in json.dumps(event)
        assert storage.read_trace_block("trace", event["body_ref"])["events"][0]["decision"]["decision_id"] == "original-question"


def test_large_promotion_keeps_producer_ids_without_copying_its_body(tmp_path_factory):
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        _append(storage, 1, "agent_started", process="H", role="subagent", temporary_subagent=True)
        promotion = {"process_ids": ["H"], "body": "large promotion result" * 200}
        _append(storage, 2, "worktree_promoted", promotion=promotion)
        helper, applied = storage.read_trace_index("trace")["events"]
        assert helper["role"] == "subagent" and helper["temporary_subagent"] is True
        assert applied["promotion"] == {"process_ids": ["H"]}
        assert storage.read_trace_block("trace", applied["body_ref"])["events"][0]["promotion"] == promotion


def test_index_pages_pin_the_snapshot_and_preserve_identity_while_a_trace_grows(tmp_path_factory):
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        _append(storage, 1, "reasoning_delta", delta="old")
        _append(storage, 2, "tool_result", tool_name="read_file", is_error=False)
        first = storage.read_trace_index("trace", limit=1)
        assert first["has_more"]
        _append(storage, 3, "reasoning_delta", delta="new append")
        tail = storage.read_trace_index("trace", after_seq=first["next_seq"], snapshot=first["snapshot"], limit=1)
        assert [event["seq"] for event in tail["events"]] == [2]
        assert tail["snapshot"] == first["snapshot"]
        assert not tail["has_more"]
        fresh = storage.read_trace_index("trace")
        assert fresh["events"][0]["body_ref"] == first["events"][0]["body_ref"] | {"snapshot": fresh["snapshot"]}
        assert fresh["snapshot"]["last_seq"] == 3


def test_index_cursor_cannot_split_a_block_and_scope_mismatch_never_reads_another_actor(tmp_path_factory):
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        _append(storage, 1, "reasoning_delta", delta="one")
        _append(storage, 2, "reasoning_delta", delta="two")
        _append(storage, 3, "reasoning_delta", process="B", delta="child")
        page = storage.read_trace_index("trace", process_id="A")
        with pytest.raises(ValueError, match="CURSOR_SPLIT_BLOCK"):
            storage.read_trace_index("trace", process_id="A", after_seq=1, snapshot=page["snapshot"])
        with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
            storage.read_trace_index("other-trace", process_id="A", snapshot=page["snapshot"])
        reference = deepcopy(page["events"][0]["body_ref"])
        reference["process_id"] = "B"
        with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
            storage.read_trace_block("trace", reference)


def test_expired_source_invalidates_index_and_body_instead_of_reconstructing_history(tmp_path_factory):
    with StorageRuntime(_workspace(tmp_path_factory), state_home=tmp_path_factory / "state") as storage:
        _append(storage, 1, "reasoning_delta", delta="one")
        _append(storage, 2, "tool_result", tool_name="read_file")
        page = storage.read_trace_index("trace", limit=1)
        # Fault injection in this isolated fixture models observability retention.
        with storage._observability_lock:
            storage._observability.execute("DELETE FROM trace_events WHERE trace_id=? AND sequence=?", ("trace", 1))
        with pytest.raises(ValueError, match="SNAPSHOT_EXPIRED"):
            storage.read_trace_index("trace", snapshot=page["snapshot"])
        with pytest.raises(ValueError, match="SNAPSHOT_EXPIRED"):
            storage.read_trace_block("trace", page["events"][0]["body_ref"])


def test_native_host_routes_summary_index_and_lazy_body_through_existing_read_boundary(tmp_path_factory):
    workspace = _workspace(tmp_path_factory)
    storage = get_storage(workspace)
    _append(storage, 1, "reasoning_delta", delta="PRIVATE RECORDED TEXT")
    host = SimpleNamespace(services=SimpleNamespace(project_list=lambda: [{"name": "fixture", "workspace": str(workspace)}], project_list_archived=lambda: []))
    summary = NativeHost._runtime_trace(host, "fixture", {"action": "summary", "trace_id": "trace", "include_recent": False})
    assert summary["event_count"] == 1 and summary["recent_events"] == []
    index = NativeHost._runtime_trace(host, "fixture", {"action": "index", "trace_id": "trace"})
    assert "PRIVATE RECORDED TEXT" not in json.dumps(index)
    detail = NativeHost._runtime_trace(host, "fixture", {"action": "block", "trace_id": "trace", "body_ref": index["events"][0]["body_ref"]})
    assert detail["events"][0]["delta"] == "PRIVATE RECORDED TEXT"
    assert storage.read_trace_events("trace")["events"] == detail["events"]


def test_summary_index_and_body_survive_the_actual_native_request_envelope(tmp_path_factory):
    workspace = _workspace(tmp_path_factory)
    _append(get_storage(workspace), 1, "reasoning_delta", delta="EXACT ORIGINAL 中文")
    output = io.StringIO()

    def no_daemon(*_args, **_kwargs):
        raise AssertionError("History inspection cannot start a runtime")

    host = NativeHost(stdout=output, daemon_factory=no_daemon)
    host.services.project_list = lambda: [{"name": "fixture", "workspace": str(workspace)}]

    def request(identity, **arguments):
        host._handle_request({"request_id": identity, "operation": "runtime.trace",
                              "arguments": {"project": "fixture", "trace_id": "trace", **arguments}})
        return [json.loads(line) for line in output.getvalue().splitlines()
                if json.loads(line).get("request_id") == identity][-1]

    try:
        summary = request("summary", action="summary", include_recent=False)["result"]
        assert summary["event_count"] == 1 and not summary["recent_events"]
        index = request("index", action="index")["result"]
        assert "EXACT ORIGINAL" not in json.dumps(index)
        body = request("body", action="block", body_ref=index["events"][0]["body_ref"])["result"]
        assert body["events"][0]["delta"] == "EXACT ORIGINAL 中文"
        changed = deepcopy(index["events"][0]["body_ref"])
        changed["process_id"] = "other"
        assert request("mismatch", action="block", body_ref=changed)["error"]["code"] == "TRACE_BLOCK_REFERENCE_MISMATCH"
    finally:
        host.close()
