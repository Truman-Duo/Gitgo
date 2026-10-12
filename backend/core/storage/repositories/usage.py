"""Usage repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
from .ports import UsagePort
from ..support import _encode_page_cursor, _decode_page_cursor

def read_task_usage(self: UsagePort, *, limit: int = 100, cursor: str = "") -> dict:
    """Return authoritative root-task usage without reading verbose traces."""
    self._ensure_open()
    bounded = max(1, min(int(limit), 1000))
    page_after = _decode_page_cursor(str(cursor or ""))
    with self._state_lock:
        totals = self._state.execute(
            """SELECT COUNT(*), COALESCE(SUM(provider_calls), 0),
                      COALESCE(SUM(input_tokens), 0),
                      COALESCE(SUM(output_tokens), 0),
                      COALESCE(SUM(cache_read_tokens), 0),
                      COALESCE(SUM(cache_write_tokens), 0),
                      COALESCE(SUM(duration_ms), 0),
                      COALESCE(SUM(tool_calls), 0)
               FROM task_usage WHERE parent_process_id=''"""
        ).fetchone()
        rows = self._state.execute(
            """SELECT task_id, process_id, task_kind, status, provider_calls,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_write_tokens, duration_ms, tool_calls,
                      created_at, updated_at
               FROM task_usage WHERE parent_process_id=''
               ORDER BY updated_at DESC, task_id DESC""",
        ).fetchall()
        legacy_rows = self._state.execute(
            """SELECT DISTINCT t.task_id, sp.process_id, t.status,
                      t.outcome_ref, t.created_at, t.updated_at
               FROM tasks t
               JOIN session_processes sp ON sp.task_id=t.task_id
               LEFT JOIN task_usage u ON u.task_id=t.task_id
               WHERE sp.parent_process_id IS NULL
                 AND t.outcome_ref IS NOT NULL AND u.task_id IS NULL
               ORDER BY t.updated_at DESC, t.task_id DESC""",
        ).fetchall()
    keys = (
        "task_count", "provider_calls", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "duration_ms", "tool_calls",
    )
    summary = {
        key: (float(totals[index]) if key == "duration_ms" else int(totals[index]))
        for index, key in enumerate(keys)
    }
    legacy_tasks: list[dict] = []
    for row in legacy_rows:
        outcome = self._read_json_blob(str(row["outcome_ref"]))
        if not isinstance(outcome, dict):
            continue
        metadata = dict(outcome.get("metadata") or {})
        budget = dict(metadata.get("task_tree_budget") or {})
        used = dict(budget.get("used") or {})
        item = {
            "task_id": str(row["task_id"]),
            "process_id": str(row["process_id"]),
            "task_kind": str(metadata.get("task_kind") or ""),
            "status": str(outcome.get("status") or row["status"] or ""),
            "provider_calls": int(used.get("provider_calls", 0) or 0),
            "input_tokens": int(used.get("reported_input_tokens", 0) or 0),
            "output_tokens": int(used.get("reported_output_tokens", 0) or 0),
            "cache_read_tokens": int(used.get("reported_cache_read_tokens", 0) or 0),
            "cache_write_tokens": int(used.get("reported_cache_write_tokens", 0) or 0),
            "duration_ms": float(outcome.get("duration_ms", 0.0) or 0.0),
            "tool_calls": int(outcome.get("tool_calls_executed", 0) or 0),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
        legacy_tasks.append(item)
        summary["task_count"] += 1
        for key in keys[1:]:
            summary[key] += item[key]
    input_tokens = summary["input_tokens"]
    summary["cache_hit_ratio"] = (
        round(summary["cache_read_tokens"] / input_tokens, 4)
        if input_tokens else 0.0
    )
    task_rows = [{key: row[key] for key in row.keys()} for row in rows]
    task_rows.extend(legacy_tasks)
    task_rows.sort(key=lambda item: (
        str(item.get("updated_at") or ""), str(item.get("task_id") or ""),
    ), reverse=True)
    if page_after is not None:
        task_rows = [item for item in task_rows if (
            str(item.get("updated_at") or ""), str(item.get("task_id") or ""),
        ) < page_after]
    page = task_rows[:bounded]
    has_more = len(task_rows) > bounded
    next_cursor = (
        _encode_page_cursor(
            str(page[-1].get("updated_at") or ""),
            str(page[-1].get("task_id") or ""),
        ) if has_more and page else None
    )
    return {
        "summary": summary,
        "tasks": page,
        "page": {
            "limit": bounded, "cursor": str(cursor or ""),
            "next_cursor": next_cursor, "has_more": has_more,
            "scope": "root_tasks_ordered_by_updated_at",
            "summary_scope": "all_authoritative_root_tasks",
        },
    }
