"""Bounded read-only tool text views over the original immutable Trace CAS."""
from .request_input import text_source_page
from .tool_summary import tool_argument_preview


def read_record_view(read_detail, reference, *, source_index=None, offset=0):
    original = read_detail(reference)
    if not isinstance(original, dict):
        raise ValueError("TRACE_RECORD_UNAVAILABLE")
    detail = original.get("legacy_detail", original)
    if not isinstance(detail, dict):
        raise ValueError("TRACE_RECORD_UNAVAILABLE")
    data = detail.get("data") or {}
    data = data if isinstance(data, dict) else {}
    fields = []
    if isinstance(data.get("content"), str):
        fields.append(("content", data["content"]))
        for warning in data.get("warnings") or []:
            if isinstance(warning, dict) and isinstance(warning.get("message"), str):
                fields.append(("warning", warning["message"]))
    elif isinstance(detail.get("formatted_result"), str):
        fields.append(("result", detail["formatted_result"]))
    if source_index is None:
        return {"representation": "trace_record_sources",
                "argument_preview": tool_argument_preview(detail.get("arguments") or {}),
                "sources": [{"source_index": index, "kind": kind, "total_chars": len(text)}
                            for index, (kind, text) in enumerate(fields)],
                "has_diff": isinstance(data.get("diff"), str) and bool(data["diff"]),
                "file_excerpt": bool(data.get("truncated")) if fields and fields[0][0] == "content" else False}
    if isinstance(source_index, bool) or not isinstance(source_index, int) or not 0 <= source_index < len(fields):
        raise ValueError("TRACE_RECORD_SOURCE_INVALID")
    return text_source_page(fields[source_index][1], source_index, offset,
                            representation="trace_record_source_page")
