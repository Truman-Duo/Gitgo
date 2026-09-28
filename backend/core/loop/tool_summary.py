"""Small, deterministic tool-result projections for the terminal timeline.

The full result remains in Trace/CAS and is available in verbose/detail views.
This module deliberately operates on structured Host data so the frontend
never has to truncate JSON or infer facts with regular expressions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def compact_tool_summary(
    tool_name: str,
    data: dict | None,
    *,
    is_error: bool = False,
    error: str = "",
) -> str:
    payload = data if isinstance(data, dict) else {}
    if is_error:
        info = payload.get("error_info")
        if isinstance(info, dict):
            return str(info.get("code") or info.get("catalog_id") or "failed")[:120]
        return (str(error).strip().splitlines()[0] if error else "failed")[:120]

    for key, noun in (
        ("matches", "matches"),
        ("results", "results"),
        ("files", "files"),
        ("items", "items"),
        ("lessons", "lessons"),
        ("agents", "agents"),
    ):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            return f"{len(value)} {noun}"

    for key, noun in (
        ("count", "results"),
        ("match_count", "matches"),
        ("file_count", "files"),
        ("passed", "passed"),
        ("failed", "failed"),
    ):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value} {noun}"

    if tool_name in {"write_file", "edit_file", "apply_patch"}:
        path = str(payload.get("path") or payload.get("file") or "").strip()
        return f"saved {Path(path).name}" if path else "saved"
    if tool_name == "delete_file":
        path = str(payload.get("path") or "").strip()
        return f"deleted {Path(path).name}" if path else "deleted"
    if tool_name in {"run_command", "shell", "bash", "run_test"}:
        code = payload.get("exit_code", payload.get("returncode"))
        return f"exit {code}" if isinstance(code, int) else "completed"
    if tool_name in {"read_file", "document_open", "tool_result_open"}:
        content: Any = payload.get("content", payload.get("text"))
        if isinstance(content, str):
            return f"{len(content)} chars"
    if payload.get("provider_reachable") is True:
        return "provider reachable"
    return "completed"
