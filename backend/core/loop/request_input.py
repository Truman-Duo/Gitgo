"""Observation of the immutable request selected by Model Access.

No files are reopened and no source metadata is invented. Full input remains
in the existing access-controlled trace detail, never in the public event.
"""
from copy import deepcopy
import hashlib
import json

from .provider_protocol import deterministic_hash

INPUT_SOURCE_PAGE_CHARS = 8192


def text_source_page(text, source_index, offset, *, representation="canonical_provider_input_source_page"):
    """One shared Unicode page contract for saved request and tool sources."""
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= len(text):
        raise ValueError("TRACE_INPUT_SOURCE_INVALID")
    end = min(len(text), offset + INPUT_SOURCE_PAGE_CHARS)
    return {"representation": representation, "source_index": source_index,
            "offset": offset, "next_offset": end, "total_chars": len(text), "has_more": end < len(text),
            "text": text[offset:end], "before": text[max(0, offset-64):offset], "after": text[end:end+64]}


def read_request_input_view(read_detail, reference, *, source_index=None, offset=0):
    """Read-only projection over immutable trace CAS; never rebuild live input.

    Metadata does not include request history/tools. Body pages resolve only
    the selected message through prefix deltas, not the complete request tree.
    Every transport response is bounded; the Agent/provider path is untouched.
    """
    detail = read_detail(reference)
    if not isinstance(detail, dict):
        raise ValueError("TRACE_INPUT_SNAPSHOT_UNAVAILABLE")
    provenance = detail.get("input_provenance")
    if (detail.get("representation") not in {"canonical_provider_input", "canonical_provider_delta"}
            or not isinstance(provenance, dict) or provenance.get("schema_version") != 1):
        raise ValueError("TRACE_INPUT_SNAPSHOT_UNAVAILABLE")
    sources = provenance.get("sources")
    if not isinstance(sources, list):
        raise ValueError("TRACE_INPUT_SNAPSHOT_UNAVAILABLE")
    if source_index is None:
        return {"representation": "canonical_provider_input_sources", "input_provenance": provenance}
    if (isinstance(source_index, bool) or not isinstance(source_index, int)
            or not 0 <= source_index < len(sources) or not isinstance(sources[source_index], dict)
            or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise ValueError("TRACE_INPUT_SOURCE_INVALID")
    message_index = sources[source_index].get("message_index")
    if isinstance(message_index, bool) or not isinstance(message_index, int) or message_index < 0:
        raise ValueError("TRACE_INPUT_SOURCE_INVALID")
    visited = {reference}
    while detail.get("representation") == "canonical_provider_delta":
        prefix = detail.get("common_prefix_messages")
        appended = detail.get("appended_messages")
        if isinstance(prefix, bool) or not isinstance(prefix, int) or prefix < 0 or not isinstance(appended, list):
            raise ValueError("TRACE_INPUT_DELTA_INVALID")
        if message_index >= prefix:
            messages, message_index = appended, message_index - prefix
            break
        reference = detail.get("base_detail_ref")
        if not isinstance(reference, str) or reference in visited:
            raise ValueError("TRACE_INPUT_DELTA_INVALID")
        visited.add(reference)
        detail = read_detail(reference)
        if not isinstance(detail, dict):
            raise ValueError("TRACE_INPUT_SNAPSHOT_UNAVAILABLE")
    else:
        messages = detail.get("messages")
    if not isinstance(messages, list) or message_index >= len(messages) or not isinstance(messages[message_index], dict):
        raise ValueError("TRACE_INPUT_SOURCE_UNAVAILABLE")
    content = messages[message_index].get("content")
    if content is None:
        raise ValueError("TRACE_INPUT_SOURCE_UNAVAILABLE")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return text_source_page(text, source_index, offset)


def request_input_detail(request, body):
    sources = []
    for index, message in enumerate(request.messages):
        sections = message.get("prompt_sections")
        if not sections and message.get("host_authority") is not True:
            continue
        content = message.get("content", "")
        source = {
            "message_index": index,
            "message_type": message.get("message_type", "unattributed_runtime_input"),
            "content_sha256": hashlib.sha256(content.encode("utf8")).hexdigest()
                if isinstance(content, str) else deterministic_hash(content),
            "prompt_schema_version": message.get("prompt_schema_version"),
            "sections": [
                {key: section.get(key) for key in ("name", "provenance", "version")}
                for section in sections or [] if isinstance(section, dict)
            ],
        }
        # These are already explicit compiler/projection metadata. A digest
        # identifies the exact snapshot/patch; it is not a semantic authority
        # or proof that the provider received or understood the input.
        for field in ("context_seed", "context_snapshot"):
            if field in message:
                source[field + "_sha256"] = deterministic_hash(message[field])
        for field in ("prompt_hash", "steering_type", "steering_version", "steering_sequence", "context_purpose"):
            if field in message:
                source[field] = deepcopy(message[field])
        sources.append(source)
    return {
        "representation": "canonical_provider_input",
        "messages": deepcopy(list(request.messages)),
        "tools": deepcopy(list(request.tools)),
        "input_provenance": {
            "schema_version": 1, "state": "dispatching", "server_confirmed": False,
            "wire_body_sha256": deterministic_hash(body), "sources": sources,
        },
    }
