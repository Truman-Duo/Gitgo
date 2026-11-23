from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.core.loop.request_input import INPUT_SOURCE_PAGE_CHARS, read_request_input_view
from backend.core.native_host import NativeHost


def snapshot(text="SAVED RULES"):
    return {"representation": "canonical_provider_input", "messages": [
        {"content": text}, {"content": "UNRELATED CHAT HISTORY" * 100000}],
        "tools": [{"description": "UNRELATED TOOL SCHEMA" * 10000}],
        "input_provenance": {"schema_version": 1, "server_confirmed": False, "sources": [
            {"message_index": 0, "sections": [{"name": "AGENTS.md", "version": "old"}]}]}}


def test_metadata_excludes_request_history_and_body():
    detail = snapshot()
    before = deepcopy(detail)
    result = read_request_input_view(lambda ref: detail, "saved")
    assert len(json.dumps(result)) < 1000
    assert "messages" not in result and "tools" not in result
    assert result["input_provenance"]["sources"][0]["sections"][0]["version"] == "old"
    assert detail == before


def test_source_reads_resolve_only_the_selected_message_through_original_deltas():
    full = snapshot()
    delta = {"representation": "canonical_provider_delta", "base_detail_ref": "full",
             "common_prefix_messages": 2, "appended_messages": [{"content": "LATEST SOURCE"}],
             "input_provenance": {"schema_version": 1, "sources": [{"message_index": 0}, {"message_index": 2}]}}
    calls = []
    def read(ref):
        calls.append(ref)
        return {"full": full, "delta": delta}[ref]
    assert read_request_input_view(read, "delta", source_index=0)["text"] == "SAVED RULES"
    assert calls == ["delta", "full"]
    calls.clear()
    assert read_request_input_view(read, "delta", source_index=1)["text"] == "LATEST SOURCE"
    assert calls == ["delta"]


def test_large_unicode_sources_have_bounded_pages_and_no_reading_cutoff():
    text = ("中文 e\u0301 👨‍👩‍👧‍👦\n" * 3000) + "EXACT FINAL SOURCE"
    detail = snapshot(text)
    pages = []
    offset = 0
    while True:
        page = read_request_input_view(lambda ref: detail, "saved", source_index=0, offset=offset)
        assert len(page["text"]) <= INPUT_SOURCE_PAGE_CHARS
        assert len(page["before"]) <= 64 and len(page["after"]) <= 64
        assert len(json.dumps(page, ensure_ascii=False).encode("utf8")) < 50000
        pages.append(page["text"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert "".join(pages) == text


@pytest.mark.parametrize("options", [{"source_index": True}, {"source_index": -1},
    {"source_index": 2}, {"source_index": 0, "offset": True}, {"source_index": 0, "offset": 100000}])
def test_invalid_source_or_cursor_is_rejected(options):
    with pytest.raises(ValueError, match="TRACE_INPUT_SOURCE_INVALID"):
        read_request_input_view(lambda ref: snapshot(), "saved", **options)


def test_cyclic_or_missing_original_snapshots_are_not_replaced_with_current_input():
    delta = {"representation": "canonical_provider_delta", "base_detail_ref": "same",
             "common_prefix_messages": 1, "appended_messages": [],
             "input_provenance": {"schema_version": 1, "sources": [{"message_index": 0}]}}
    with pytest.raises(ValueError, match="TRACE_INPUT_DELTA_INVALID"):
        read_request_input_view(lambda ref: delta, "same", source_index=0)
    with pytest.raises(ValueError, match="TRACE_INPUT_SNAPSHOT_UNAVAILABLE"):
        read_request_input_view(lambda ref: {"representation": "prepared_runtime_projection"}, "old")


def test_formal_native_trace_dispatch_keeps_the_same_project_boundary(monkeypatch, tmp_path_factory):
    from backend.core.loop import trace
    host = NativeHost.__new__(NativeHost)
    host.services = SimpleNamespace(project_list=lambda: [{"name": "fixture", "workspace": str(tmp_path_factory)}],
                                    project_list_archived=lambda: [])
    calls = []
    def read(workspace, ref):
        calls.append((workspace, ref))
        return snapshot()
    monkeypatch.setattr(trace, "read_trace_detail", read)
    result = host._runtime_trace("fixture", {"action": "detail", "ref": "saved", "view": "input_sources"})
    assert result["detail"]["representation"] == "canonical_provider_input_sources"
    assert calls == [(tmp_path_factory.resolve(), "saved")]
    result = host._runtime_trace("fixture", {"action": "detail", "ref": "saved", "view": "input_source", "source_index": 0})
    assert result["detail"]["text"] == "SAVED RULES"
