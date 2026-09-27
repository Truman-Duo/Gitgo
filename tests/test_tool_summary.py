from backend.core.loop.tool_summary import compact_tool_summary


def test_structured_collection_summaries_do_not_expose_json():
    assert compact_tool_summary("list_files", {"files": [{}, {}]}) == "2 files"
    assert compact_tool_summary("context_search", {"results": []}) == "0 results"
    assert compact_tool_summary("search_text", {"matches": [1]}) == "1 matches"


def test_write_and_error_summaries_are_stable():
    assert compact_tool_summary("write_file", {"path": "docs/index.html"}) == "saved index.html"
    assert compact_tool_summary("delete_file", {"path": "docs/index.html"}) == "deleted index.html"
    assert compact_tool_summary(
        "web_search", {"error_info": {"code": "WEB_SEARCH_FAILED"}},
        is_error=True,
    ) == "WEB_SEARCH_FAILED"
