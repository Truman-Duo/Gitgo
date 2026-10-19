
import json
import hashlib
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from backend.core.history import HistoryManager
from .models import Lesson
from .manager import LessonManager


def harvest_lessons(workspace_path: Path, project_name: str, tech_stack: str = "") -> list[Lesson]:
    """Legacy discovery records observations, never saves or promotes lessons."""
    with HistoryManager.workspace_scope(str(workspace_path)):
        _harvest_from_git_log(workspace_path, project_name, tech_stack)
        _harvest_from_claude_md(workspace_path, project_name, tech_stack)
        _harvest_from_scan_history(project_name, tech_stack)
        _harvest_from_governance_signals(workspace_path, project_name, tech_stack)
    return []


def _observe_history(project_name: str, operations=None) -> list[Lesson]:
    for entry in HistoryManager.load()[-50:]:
        if entry.project_name != project_name:
            continue
        if entry.operation.startswith(("harvest", "knowledge_")) or entry.operation == "unprocessed_signal":
            continue
        if operations is not None and entry.operation not in operations:
            continue
        raw = {"operation": entry.operation, "status": entry.status,
               "timestamp": entry.timestamp, "detail": entry.detail}
        identity = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
        capture_signal("history_observation", {"source_record": raw, "information_kind": "unclassified"},
                       project_name, source_event_id=identity)
    return []


def _harvest_from_governance_signals(workspace_path, project_name, tech_stack=""):
    return _observe_history(project_name)


def _harvest_from_scan_history(project_name, tech_stack=""):
    return _observe_history(project_name, {"scan", "sync"})


def _harvest_from_git_log(workspace_path, project_name, tech_stack=""):
    try:
        result = subprocess.run(["git", "log", "-5", "--format=%H %s", "--name-status"],
            cwd=str(workspace_path), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        capture_signal("discovery_unavailable", {"source": "git_log", "error_type": type(exc).__name__},
                       project_name, source_event_id="git_log:" + type(exc).__name__)
        return []
    if result.returncode == 0 and result.stdout.strip():
        raw = result.stdout[:8000]
        capture_signal("vcs_observation", {"changes": raw, "truncated": len(result.stdout) > 8000},
            project_name, source_event_id=hashlib.sha256(raw.encode()).hexdigest())
    return []


def _harvest_from_claude_md(workspace_path, project_name, tech_stack=""):
    path = Path(workspace_path) / "CLAUDE.md"
    if not path.is_file():
        return []
    content = path.read_text(encoding="utf-8", errors="replace")
    capture_signal("document_observation", {"path": "CLAUDE.md", "content": content[:8000],
        "truncated": len(content) > 8000, "source_authority": "project_document"},
        project_name, source_event_id=hashlib.sha256(content.encode()).hexdigest())
    return []


# ── v0.35: 信号捕获 + 调度 + LLM 总结 ──────────────────────

MIN_BATCH_SIZE = 5
DENSITY_WINDOW = 50
DENSITY_THRESHOLD = 2.0
MIN_SOURCES = 2
COOLDOWN_SECONDS = 300
LLM_BATCH_MAX = 30
MAX_HARVEST_RETRY = 5

_last_harvest_time: dict[str, float] = {}
_signal_store_lock = threading.Lock()
_SIGNAL_STORE_VERSION = 1


def _signal_store_path() -> Path:
    return HistoryManager._path().with_name("harvest_signals.json")


def _load_signal_store() -> dict:
    runtime = HistoryManager._storage()
    path = _signal_store_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            records = raw.get("records") if isinstance(raw, dict) else None
            if isinstance(records, dict):
                runtime.save_harvest_signal_records([
                    dict(item) for item in records.values()
                    if isinstance(item, dict)
                ])
                suffix = datetime.now().strftime("%Y%m%dT%H%M%S")
                archived = path.with_name(
                    f"{path.name}.legacy-imported-{suffix}"
                )
                counter = 1
                while archived.exists():
                    archived = path.with_name(
                        f"{path.name}.legacy-imported-{suffix}-{counter}"
                    )
                    counter += 1
                path.replace(archived)
        except (OSError, ValueError, json.JSONDecodeError):
            # Keep the legacy source for a later idempotent retry.
            raise
    records = runtime.load_harvest_signal_records(with_version=True)
    return {
        "version": _SIGNAL_STORE_VERSION,
        "records": {
            str(item["signal_id"]): item for item in records
            if item.get("signal_id")
        },
    }


def _save_signal_store(store: dict) -> None:
    records = store.get("records") if isinstance(store, dict) else None
    if not isinstance(records, dict):
        raise ValueError("invalid harvest signal store")
    HistoryManager._storage().save_harvest_signal_records([
        dict(item) for item in records.values() if isinstance(item, dict)
    ])


def _signal_id(project_name: str, signal_type: str,
               source_event_id: str) -> str:
    if source_event_id:
        payload = f"{project_name}\0{signal_type}\0{source_event_id}".encode("utf-8")
        return "hs_" + hashlib.sha256(payload).hexdigest()[:24]
    return "hs_" + uuid.uuid4().hex


class CaptureAuditIncomplete(RuntimeError):
    """Signal committed; audit failure must not reset its durable lifecycle."""
    def __init__(self, signal_id):
        super().__init__("Harvest observation committed; its audit append failed")
        self.signal_id = signal_id


def capture_signal(signal_type: str, detail: dict, project_name: str, *,
                   source_event_id: str = "", storage=None) -> str:
    """Insert an idempotent pending observation, never reset a durable lifecycle.

    Runtime-bound callers pass storage explicitly; legacy static consumers
    continue using their existing project binding and migration path.
    """
    signal_id = _signal_id(project_name, signal_type, source_event_id)
    now = datetime.now().isoformat()
    runtime = storage if storage is not None else HistoryManager._storage()
    if storage is None:
        with _signal_store_lock:
            _load_signal_store()  # Complete the existing legacy import, if any.
    record = {"signal_id": signal_id, "project_name": project_name,
              "signal_type": signal_type, "detail": dict(detail),
              "source_event_id": source_event_id, "state": "pending",
              "retry_count": 0, "lease_id": "", "lease_until": 0.0,
              "lesson_ids": [], "created_at": now, "updated_at": now}
    inserted = runtime.save_harvest_signal_records([record], create_only=True)
    if inserted:
        try:
            runtime.append_history_record({"timestamp": now, "project_name": project_name,
                "operation": "unprocessed_signal", "status": "recorded",
                "detail": {"signal_id": signal_id, "signal_type": signal_type},
                "correlation_id": signal_id}, event_id="capture_" + signal_id)
        except Exception as exc:
            raise CaptureAuditIncomplete(signal_id) from exc
    return signal_id


def get_unprocessed_signals(project_name: str,
                            signal_type: str | None = None) -> list[dict]:
    """Return pending/retry signals; expired leases become retryable again."""
    now = time.time()
    changed = False
    with _signal_store_lock:
        store = _load_signal_store()
        signals = []
        for record in store["records"].values():
            if record.get("project_name") != project_name:
                continue
            if record.get("state") == "leased" and float(
                record.get("lease_until", 0.0)
            ) <= now:
                record["state"] = "retry"
                record["lease_id"] = ""
                changed = True
            if record.get("state") not in {"pending", "retry"}:
                continue
            if signal_type and record.get("signal_type") != signal_type:
                continue
            detail = dict(record.get("detail") or {})
            signals.append({
                **detail,
                "signal_id": record["signal_id"],
                "timestamp": record.get("created_at", ""),
                "correlation_id": record["signal_id"],
                "signal_type": record.get("signal_type", "unknown"),
                "harvest_retry_count": int(record.get("retry_count", 0)),
            })
        if changed:
            _save_signal_store(store)
    return sorted(signals, key=lambda item: item.get("timestamp", ""))


def harvest_status(project_name: str) -> dict:
    """Return a privacy-safe lifecycle summary without raw signal payloads."""
    with _signal_store_lock:
        store = _load_signal_store()
        rows = [
            record for record in store["records"].values()
            if record.get("project_name") == project_name
        ]
    states: dict[str, int] = {}
    types: dict[str, int] = {}
    for row in rows:
        state = str(row.get("state") or "unknown")
        signal_type = str(row.get("signal_type") or "unknown")
        states[state] = states.get(state, 0) + 1
        types[signal_type] = types.get(signal_type, 0) + 1
    return {
        "total_signals": len(rows),
        "states": states,
        "signal_types": types,
        "awaiting_confirmation": [
            str(row.get("proposal_id")) for row in rows
            if row.get("state") == "awaiting_confirmation" and row.get("proposal_id")
        ],
    }


def task_failure_relations(project_name: str, task_id: str, context: dict, receipt: dict) -> list[dict]:
    from .evidence_context import correlation_hint
    with _signal_store_lock:
        store = _load_signal_store()
        rows = [row for row in store["records"].values()
                if row.get("project_name") == project_name and row.get("signal_type") == "execution_failure"
                and (row.get("detail") or {}).get("task_id") == task_id]
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row["signal_id"])))
        return [{"signal_id": row["signal_id"], **correlation_hint(row.get("detail") or {}, context, receipt)}
                for row in rows[-10:]]


def task_failure_sources(project_name: str, task_id: str) -> list[str]:
    """Compatibility lookup; only temporal candidate observations, not repairs."""
    return [item["signal_id"] for item in task_failure_relations(project_name, task_id, {}, {})]


def lease_harvest_signals(project_name: str, *, limit: int = LLM_BATCH_MAX,
                          lease_seconds: float = 120.0) -> list[dict]:
    """Atomically lease a mixed-source batch for exactly one harvester."""
    candidates = get_unprocessed_signals(project_name)[:limit]
    if not candidates:
        return []
    ids = {item["signal_id"] for item in candidates}
    lease_id = "hl_" + uuid.uuid4().hex
    now = time.time()
    with _signal_store_lock:
        store = _load_signal_store()
        leased_ids = []
        for signal_id in ids:
            record = store["records"].get(signal_id)
            if record is None or record.get("state") not in {"pending", "retry"}:
                continue
            record["state"] = "leased"
            record["lease_id"] = lease_id
            record["lease_until"] = now + max(1.0, lease_seconds)
            record["updated_at"] = datetime.now().isoformat()
            leased_ids.append(signal_id)
        _save_signal_store(store)
    return [item for item in candidates if item["signal_id"] in leased_ids]


def lease_harvest_signal_ids(
    project_name: str, signal_ids: list[str], *, lease_seconds: float = 120.0,
) -> list[dict]:
    """Lease an explicit signal set without consuming the automatic backlog."""
    requested = list(dict.fromkeys(str(item) for item in signal_ids if str(item)))
    if not requested:
        return []
    lease_id = "hl_" + uuid.uuid4().hex
    now = time.time()
    leased_ids: list[str] = []
    with _signal_store_lock:
        store = _load_signal_store()
        for signal_id in requested:
            record = store["records"].get(signal_id)
            if record is None or record.get("project_name") != project_name:
                continue
            if record.get("state") not in {"pending", "retry"}:
                continue
            record["state"] = "leased"
            record["lease_id"] = lease_id
            record["lease_until"] = now + max(1.0, lease_seconds)
            record["updated_at"] = datetime.now().isoformat()
            leased_ids.append(signal_id)
        _save_signal_store(store)
    # get_unprocessed_signals intentionally hides active leases, so rebuild the
    # exact public signal projection from durable records.
    with _signal_store_lock:
        store = _load_signal_store()
        result = []
        for signal_id in leased_ids:
            record = store["records"].get(signal_id) or {}
            detail = dict(record.get("detail") or {})
            result.append({
                **detail,
                "signal_id": signal_id,
                "timestamp": record.get("created_at", ""),
                "correlation_id": signal_id,
                "signal_type": record.get("signal_type", "unknown"),
                "harvest_retry_count": int(record.get("retry_count", 0)),
            })
    return result


def stage_harvest_proposal(
    project_name: str, signal_ids: list[str], lessons: list[Lesson],
    *, summary_report: dict | None = None,
) -> dict:
    """Persist a semantic proposal until the user confirms or dismisses it."""
    proposal_id = "hp_" + uuid.uuid4().hex
    candidates = [lesson.to_dict() for lesson in lessons]
    with _signal_store_lock:
        store = _load_signal_store()
        matched = []
        for signal_id in signal_ids:
            record = store["records"].get(signal_id)
            if record is None or record.get("project_name") != project_name:
                continue
            if record.get("state") != "leased":
                continue
            record["state"] = "awaiting_confirmation"
            record["proposal_id"] = proposal_id
            record["proposal_lessons"] = candidates
            record["lease_id"] = ""
            record["lease_until"] = 0.0
            record["updated_at"] = datetime.now().isoformat()
            matched.append(signal_id)
        if not matched:
            raise ValueError("harvest proposal has no leased source signals")
        _save_signal_store(store)
    from .review import proposal_version
    result = {"proposal_id": proposal_id, "signal_ids": matched, "candidates": candidates,
              "proposal_version": proposal_version(project_name, proposal_id, matched, candidates)}
    try:
        HistoryManager.add_operation(
            project_name, "harvest_proposal", "awaiting_confirmation",
            {"proposal_id": proposal_id, "signal_ids": matched,
             "candidate_count": len(candidates),
             **({"summary": summary_report} if summary_report is not None else {})},
            correlation_id=proposal_id,
        )
    except Exception as exc:
        result["audit_warning"] = type(exc).__name__
    return result


def get_harvest_proposal(project_name: str, proposal_id: str) -> dict | None:
    with _signal_store_lock:
        store = _load_signal_store()
        rows = [
            record for record in store["records"].values()
            if record.get("project_name") == project_name
            and record.get("proposal_id") == proposal_id
            and record.get("state") == "awaiting_confirmation"
        ]
    if not rows:
        return None
    from .review import proposal_version
    return {
        "proposal_id": proposal_id,
        "created_at": min(str(row.get("updated_at") or row.get("created_at") or "") for row in rows),
        "task_ids": sorted({str((row.get("detail") or {}).get("task_id")) for row in rows
                            if (row.get("detail") or {}).get("task_id")}),
        "signal_ids": [str(row["signal_id"]) for row in rows],
        "candidates": list(rows[0].get("proposal_lessons") or []),
        "proposal_version": proposal_version(
            project_name, proposal_id, [str(row["signal_id"]) for row in rows],
            list(rows[0].get("proposal_lessons") or [])),
    }



def get_harvest_proposal_context(project_name: str, proposal_id: str, expected_version: str,
                                 candidate_index: int = 0, user_draft: dict | None = None) -> dict:
    """Bounded, authoritative read-only discussion data; never a review receipt."""
    proposal = get_harvest_proposal(project_name, proposal_id)
    if proposal is None:
        raise ValueError("HARVEST_PROPOSAL_NOT_FOUND")
    if not expected_version or expected_version != proposal["proposal_version"]:
        raise ValueError("HARVEST_PROPOSAL_CHANGED")
    candidates = proposal["candidates"]
    if type(candidate_index) is not int or not 0 <= candidate_index < len(candidates):
        raise ValueError("INVALID_HARVEST_CANDIDATE")
    candidate = dict(candidates[candidate_index])
    from .models import QUALIFICATION_KEYS
    qualification = {key: value for key, value in dict(candidate.get("qualification") or {}).items()
                     if key in QUALIFICATION_KEYS}
    truncated = [key for key, value in qualification.items() if len(str(value)) > 600]
    candidate = {"trigger": str(candidate.get("trigger") or "")[:240],
                 "rule": str(candidate.get("rule") or "")[:2000],
                 "qualification": {key: str(value)[:600] for key, value in qualification.items()},
                 "qualification_truncated_fields": truncated}
    draft = None
    if user_draft is not None:
        from .models import normalize_qualification
        if not isinstance(user_draft, dict) or set(user_draft) - {"trigger", "rule", "qualification"}:
            raise ValueError("INVALID_HARVEST_DRAFT")
        if (not isinstance(user_draft.get("trigger"), str) or not 1 <= len(user_draft["trigger"]) <= 240
                or not isinstance(user_draft.get("rule"), str) or not 20 <= len(user_draft["rule"]) <= 2000):
            raise ValueError("INVALID_HARVEST_DRAFT")
        draft = {"trigger": user_draft["trigger"], "rule": user_draft["rule"],
                 "qualification": normalize_qualification(user_draft.get("qualification")),
                 "authority": "user draft; not submitted or accepted"}
    original = candidates[candidate_index]
    original_evidence = original.get("evidence") or {}
    raw_excerpts = original_evidence.get("workspace_excerpts") or []
    excerpts = raw_excerpts if isinstance(raw_excerpts, list) else []
    selected_excerpts = [{key: item.get(key) for key in ("excerpt_id", "signal_id", "path", "start_line", "end_line", "sha256")} |
        {"text": str(item.get("text") or "")[:2000], "truncated": len(str(item.get("text") or "")) > 2000}
        for item in excerpts[:12] if isinstance(item, dict)]
    binding = {"scope": "selected_source_excerpts" if selected_excerpts else
                      "whole_file" if original_evidence.get("workspace_files") else "source_observations",
               "selected_excerpts": selected_excerpts, "excerpts_partial": len(excerpts) > 12,
               "coverage": "Captured post-change text snapshots. Text continuity does not establish semantic validity, dependency coverage or causal repair."}
    refs = set(original_evidence.get("source_signal_ids") or proposal["signal_ids"])
    with _signal_store_lock:
        store = _load_signal_store()
        records = [row for row in store["records"].values()
                   if row.get("project_name") == project_name
                   and row.get("proposal_id") == proposal_id
                   and row.get("state") == "awaiting_confirmation"]
        # Revalidate after the read, including terminal state and content changes.
        from .review import proposal_version
        if (not records or proposal_version(project_name, proposal_id,
                [row["signal_id"] for row in records],
                list(records[0].get("proposal_lessons") or [])) != expected_version):
            raise ValueError("HARVEST_PROPOSAL_CHANGED")
        if any(row.get("proposal_lessons") != records[0].get("proposal_lessons") for row in records):
            raise ValueError("HARVEST_PROPOSAL_INCONSISTENT")
        selected = [row for row in records if row["signal_id"] in refs]
        sources = []
        for row in selected[:12]:
            text = json.dumps(row.get("detail") or {}, ensure_ascii=False, default=str)
            sources.append({"signal_id": row["signal_id"], "signal_type": row["signal_type"],
                "created_at": row.get("created_at"), "source_event_id": row.get("source_event_id"),
                "excerpt": text[:1000], "truncated": len(text) > 1000})
    return {"project": project_name, "proposal_id": proposal_id, "proposal_version": expected_version,
            "candidate_index": candidate_index, "candidate": candidate, "user_draft": draft,
            "sources": sources, "sources_partial": len(selected) > 12, "evidence_binding": binding,
            "coverage": "Selected attributed sources only. Other evidence, prompts, documents and releases have not been exhaustively searched.",
            "authority": "unconfirmed read-only proposal; discussion cannot accept or execute it"}


def resolve_harvest_proposal(project_name: str, proposal_id: str, *, accepted: bool,
                             lesson_ids: list[str] | None = None,
                             candidates: list[dict] | None = None, expected_version: str = "") -> dict:
    """Review admission and terminal source state commit together, not in two steps."""
    proposal = get_harvest_proposal(project_name, proposal_id)
    if proposal is None:
        raise ValueError("HARVEST_PROPOSAL_NOT_FOUND")
    if lesson_ids:
        raise ValueError("Pre-saving knowledge before proposal confirmation is not supported")
    with _signal_store_lock:
        result = HistoryManager._storage().resolve_harvest_proposal_records(
            project_name, proposal_id, accepted=accepted, candidates=candidates,
            expected_version=expected_version or proposal["proposal_version"])
    try:
        HistoryManager.add_operation(project_name, "harvest_proposal", result["status"],
            result, correlation_id=proposal_id)
    except Exception as exc:
        # The content/source transaction is already committed. Never replay it.
        result["audit_warning"] = type(exc).__name__
    return result


def complete_harvest(project_name: str, signal_ids: list[str],
                     lesson_ids: list[str], *, summary_report: dict | None = None) -> None:
    with _signal_store_lock:
        store = _load_signal_store()
        for signal_id in signal_ids:
            record = store["records"].get(signal_id)
            if record is None or record.get("project_name") != project_name or record.get("state") != "leased":
                continue
            record["state"] = "processed"
            record["lesson_ids"] = list(dict.fromkeys(lesson_ids))
            record["lease_id"] = ""
            record["lease_until"] = 0.0
            record["updated_at"] = datetime.now().isoformat()
        _save_signal_store(store)
    HistoryManager.add_operation(
        project_name, "harvest_batch", "success",
        {"signal_ids": list(signal_ids), "lesson_ids": list(lesson_ids),
         **({"summary": summary_report} if summary_report is not None else {})},
    )


def fail_harvest(project_name: str, signal_ids: list[str], error: str, *,
                 summary_report: dict | None = None) -> None:
    with _signal_store_lock:
        store = _load_signal_store()
        dead = []
        for signal_id in signal_ids:
            record = store["records"].get(signal_id)
            if record is None or record.get("project_name") != project_name or record.get("state") != "leased":
                continue
            retries = int(record.get("retry_count", 0)) + 1
            record["retry_count"] = retries
            record["state"] = "dead_letter" if retries >= MAX_HARVEST_RETRY else "retry"
            record["lease_id"] = ""
            record["lease_until"] = 0.0
            record["last_error"] = error[:1000]
            record["updated_at"] = datetime.now().isoformat()
            if record["state"] == "dead_letter":
                dead.append(signal_id)
        _save_signal_store(store)
    HistoryManager.add_operation(
        project_name, "harvest_batch",
        "dead_letter" if dead else "retry",
        {"signal_ids": list(signal_ids), "dead_letter_ids": dead,
         "error": error[:1000],
         **({"summary": summary_report} if summary_report is not None else {})},
    )


def get_signal_baseline(signal_type: str, project_name: str) -> float:
    """Baseline from terminal prior batches, excluding the current backlog."""
    with _signal_store_lock:
        store = _load_signal_store()
        terminal = [
            record for record in store["records"].values()
            if record.get("project_name") == project_name
            and record.get("state") in {"processed", "dead_letter"}
        ][-100:]
    if not terminal:
        return 0.0
    matching = sum(
        1 for record in terminal if record.get("signal_type") == signal_type
    )
    return matching / max(100, len(terminal))


def signal_density(project_name: str, signal_type: str | None = None,
                   window: int = 50) -> float:
    """最近 window 个事件中未处理信号的占比。"""
    entries = HistoryManager.load()
    recent = [e for e in entries[-window:]
              if e.project_name == project_name]
    if not recent:
        return 0.0
    matching = sum(
        1 for e in recent
        if e.operation == "unprocessed_signal"
        and (signal_type is None
             or (e.detail or {}).get("signal_type") == signal_type)
    )
    return matching / len(recent)


def source_diversity(signals: list[dict]) -> int:
    """统计信号不同的来源类型数。"""
    return len(set(s.get("signal_type", "unknown") for s in signals))


def should_trigger_harvest(signal_type: str, project_name: str) -> bool:
    """多维条件调度算法。事件驱动的，不是时间驱动的。"""
    signals = get_unprocessed_signals(project_name)
    if not any(s.get("signal_type") == signal_type for s in signals):
        return False
    if len(signals) < MIN_BATCH_SIZE:
        return False
    if source_diversity(signals) < MIN_SOURCES:
        return False
    now = time.time()
    last = _last_harvest_time.get(project_name, 0)
    if now - last < COOLDOWN_SECONDS:
        return False
    return True


def mark_harvest_triggered(project_name: str) -> None:
    """记录 harvest 时间（冷却期用）。"""
    _last_harvest_time[project_name] = time.time()


def is_testable_proposition(rule: str) -> bool:
    """Validate only the transport shape of a pending lesson candidate.

    Semantic testability cannot be inferred from modal keywords.  Harvested
    lessons remain pending/advisory until independent evidence verifies them,
    so this boundary rejects only empty or unusably short payloads.
    """
    return isinstance(rule, str) and len(rule.strip()) >= 20


def harvest_llm_summary(signals: list[dict], llm_provider,
                        workspace_path: str, project_name: str, *,
                        raise_on_error: bool = False, on_provider_notice=None,
                        summary_report: dict | None = None) -> list[Lesson]:
    """Bounded semantic candidates; durable retries remain caller-owned."""
    if not signals:
        return []

    batch = signals[:LLM_BATCH_MAX]
    try:
        from .summary import summarize
        return summarize(batch, llm_provider, workspace_path, project_name,
                         on_notice=on_provider_notice, report=summary_report)
    except Exception:
        if raise_on_error:
            raise
        # Compatibility for direct callers: expose the attempted retry on the
        # supplied batch, but never append duplicate signal events. Durable
        # retries are owned by fail_harvest().
        for signal in batch:
            signal["harvest_retry_count"] = int(
                signal.get("harvest_retry_count", 0)
            ) + 1
        return []

# ── v0.35: Pending 消化 ─────────────────────────────────────

def auto_discard_invalid(workspace_path: Path, project_name: str) -> int:
    """Preserve legacy content; explicit management owns discard."""
    return 0


def auto_verify_high_confidence(workspace_path: Path, project_name: str) -> int:
    """Statistics never authorize content or expand applicability."""
    return 0
