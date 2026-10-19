"""Lesson diagnostics for exact file candidates; no model reasoning or writes.

Knowledge is read at the execution boundary. A lexical match is guidance;
only a current, explicitly registered checker bound to this operation can deny.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .applicability import assess_lesson
from .models import Lesson


def _load(workspace: Path, args: dict) -> list[Lesson]:
    from .manager import LessonManager
    from backend.core.storage import StorageRuntime
    context = args.get("_lesson_context") or {}
    project = str(context.get("project_name") or "")
    state_home = context.get("state_home")
    if not state_home:
        return [*LessonManager.load_abstract(workspace),
                *(LessonManager.load_instance(workspace, project) if project else []),
                *(LessonManager.load_pending(workspace, project) if project else [])]
    # The child receives the actual store location, not an inherited guess.
    runtime = StorageRuntime(workspace, state_home=state_home)
    try:
        LessonManager._migrate_legacy(workspace, storage=runtime)
        records = runtime.load_lesson_records(scope="abstract")
        if project:
            records += runtime.load_lesson_records(scope="instance", project_name=project)
            records += runtime.load_lesson_records(scope="pending", project_name=project)
        from backend.core.storage.bindings import get_shared_knowledge_storage
        shared = get_shared_knowledge_storage(runtime.paths.state_home)
        if shared is not None:
            records += shared.load_lesson_records(scope="abstract")
        return [Lesson.from_dict(item) for item in records]
    finally:
        runtime.close()


def relevant_diagnostics(workspace: Path, path: Path, before: str, after: str,
                         operation: str, lessons: list[Lesson], *,
                         read_range: tuple[int, int] | None = None, facts: dict | None = None) -> list[dict]:
    try:
        relative = path.relative_to(workspace).as_posix()
    except ValueError:
        relative = str(path)
    diagnostics = []
    def is_registered(lesson):
        return bool(facts.get(lesson.id, {}).get("registered")) if facts is not None else _is_registered(lesson, workspace)
    from .review import can_consume
    for lesson in lessons:
        if not can_consume(lesson):
            continue
        trigger = lesson.trigger.strip().casefold() if isinstance(lesson.trigger, str) else ""
        lexical_match = bool(trigger and any(trigger in value.casefold() for value in (relative, before, after)))
        evidence = lesson.evidence if isinstance(lesson.evidence, dict) else {}
        source_files = evidence.get("workspace_files") or {}
        # Harvested triggers are relevance descriptions, not necessarily exact
        # substrings. Reuse attributed file evidence rather than making the
        # model phrase every trigger as a path or granting semantic authority.
        check=lesson.check if isinstance(lesson.check,dict) else {}
        declared_paths=check.get("paths") or []
        declared_match=any(Path(relative)==Path(item) for item in declared_paths)
        if declared_paths and not declared_match:continue
        source_match = declared_match or isinstance(source_files, dict) and any(
            str(item).replace("\\", "/").casefold() == relative.casefold() for item in source_files)
        if not lexical_match and not source_match:
            continue
        raw_excerpts = evidence.get("workspace_excerpts") or []
        excerpts = [item for item in raw_excerpts if isinstance(item, dict) and item.get("path") == relative] if isinstance(raw_excerpts, list) else []
        if lesson.source == "auto_harvested" and not is_registered(lesson) and excerpts and operation == "read_file" and read_range is not None:
            from .excerpts import excerpt_location
            offset, count = read_range
            spans = [excerpt_location(before, item) for item in excerpts]
            if all(span is not None for span in spans) and not any(
                    count > 0 and span["start_line"] <= offset + count and span["end_line"] > offset
                    for span in spans):
                continue
        if lesson.source == "auto_harvested" and not is_registered(lesson) and excerpts and operation in {"edit_file", "write_file"}:
            from .excerpts import edit_touches_excerpts
            if not edit_touches_excerpts(before, after, excerpts):
                continue  # Other paragraphs are not a match merely because the file path is shared.
        applicability = (dict(facts.get(lesson.id, {}).get("applicability") or {"state": "unknown"})
                         if facts is not None else assess_lesson(lesson, workspace))
        check = lesson.check if isinstance(lesson.check, dict) else {}
        registered = (is_registered(lesson)
                      and check.get("authority") == "registered"
                      and check.get("mode") == "violation_pattern"
                      and operation in lesson.dangerous_tools
                      and applicability.get("state") == "current")
        code, level = "LESSON_ADVISORY", "warning"
        if registered:
            try:
                if not isinstance(check.get("pattern"), str) or not check["pattern"]:
                    raise KeyError("a registered checker requires a nonempty pattern")
                if (str(check["literal"]) in after if "literal" in check else re.search(str(check["pattern"]), after)):
                    code, level = "KNOWN_LESSON_VIOLATION", "error"
            except (KeyError, re.error):
                code, level = "LESSON_CHECK_UNAVAILABLE", "error"
        semantic = lesson.to_dict()
        for field in ("trigger_count", "applied_count", "violated_after_count", "recent_retrievals"):
            semantic.pop(field, None)
        version = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        diagnostics.append({"code": code, "level": level, "lesson_id": lesson.id,
                            "lesson_version": version, "rule": lesson.rule[:2000],
                            "trigger": lesson.trigger, "path": relative,
                            "applicability": applicability, "registered_check": registered,
                            "tech_stack": lesson.tech_stack,
                            "relevance": "lexical_trigger" if lexical_match else "source_file"})
    return diagnostics


class LessonInventoryUnavailable(RuntimeError):
    pass


class MandatoryCheckUnavailable(RuntimeError):
    pass


def _is_registered(lesson, workspace):
    from .review import can_consume
    if not can_consume(lesson):return False
    check=lesson.check if isinstance(lesson.check,dict) else {}
    activation=check.get("activation") or {}
    if lesson.source=="auto_harvested":
        from backend.core.storage import resolve_existing_storage_paths
        paths=resolve_existing_storage_paths(workspace)
        if not paths or activation.get("project_id")!=paths.project_id or activation.get("kind")!="user_permission":return False
    return check.get("authority")=="registered" and check.get("mode")=="violation_pattern"


def check_candidate(candidate, args: dict) -> dict:
    # Loading the full canonical inventory also establishes whether mandatory
    # checks exist. If it fails, absence of mandatory checks is UNKNOWN.
    try:lessons=_load(candidate.workspace,args)
    except Exception as exc:raise LessonInventoryUnavailable(type(exc).__name__) from exc
    registered=[l for l in lessons if _is_registered(l,candidate.workspace) and candidate.operation in l.dangerous_tools]
    try:
        mandatory=relevant_diagnostics(candidate.workspace,candidate.path,candidate.before,candidate.after,
                                       candidate.operation,registered)
    except Exception as exc:raise MandatoryCheckUnavailable(type(exc).__name__) from exc
    # A stale active check must be refreshed/disabled through its actual scope;
    # it cannot silently disappear into ordinary advice.
    for d in mandatory:
        if d.get("applicability",{}).get("state")!="current":
            d.update(code="LESSON_CHECK_UNAVAILABLE",level="error",registered_check=True)
    ordinary=[l for l in lessons if l not in registered]
    lookup={"state":"completed","inventory":"complete","scope":"ordinary_advice","mode":"native_candidate_lookup"}
    try:
        advice=relevant_diagnostics(candidate.workspace,candidate.path,candidate.before,candidate.after,
                                    candidate.operation,ordinary)
        lookup.update(matches=len(advice),empty=not advice)
    except Exception as exc:
        advice=[]
        lookup.update(state="failed",empty=None,error_type=type(exc).__name__)
    ordered=sorted([*mandatory,*advice],key=lambda item:item["level"]!="error")
    override=bool((args.get("_lesson_context") or {}).get("explicit_user_grant"))
    # A user can approve a known violation, but cannot turn a failed or stale
    # mandatory check into a completed check. Unknown coverage never commits.
    unavailable = any(d["code"] == "LESSON_CHECK_UNAVAILABLE" for d in mandatory)
    blocked=unavailable or (any(d["level"]=="error" for d in ordered) and not override)
    from .feedback import identity
    context=args.get("_lesson_context") or {}
    identifier=identity(candidate,context,ordered)
    referenced=str(args.get("lesson_feedback_id") or "")
    continued=(referenced==identifier and identifier in context.get("feedback_in_provider_input",[]))
    needs_feedback=bool(advice) and not blocked and not continued and not override
    return {"phase":"before_write","candidate_sha256":candidate.after_sha256,
            "blocked":blocked,"user_override":override,"diagnostics":ordered[:20],
            "diagnostic_count":len(ordered),"diagnostic_coverage":"complete" if len(ordered)<=20 else "partial",
            "lookup":lookup,"mandatory_check_state":"failed" if any(d['code']=='LESSON_CHECK_UNAVAILABLE' for d in mandatory)
                else "violation" if any(d['level']=='error' for d in mandatory) else "passed" if mandatory else "not_required",
            "feedback_id":identifier if ordered else "",
            "advisory_state":"unavailable" if lookup["state"]=="failed" else "continuation_requested" if continued else "not_returned" if needs_feedback else "not_required",
            "continuation_state":"not_requested" if not referenced else "reference_not_exposed" if referenced not in context.get("feedback_in_provider_input",[]) else "binding_changed" if referenced!=identifier else "same_candidate_requested",
            "needs_feedback":needs_feedback,"model_understanding":"unknown","model_adoption":"unknown"}


def prepare_read_inventory(workspace: Path, args: dict) -> dict:
    """Trusted state lookup; only bounded data crosses the native read boundary."""
    lessons = _load(workspace, args)
    selected, facts, used = [], {}, 0
    for lesson in lessons:
        if not __import__("backend.core.knowledge.review", fromlist=["can_consume"]).can_consume(lesson):
            continue
        record = lesson.to_dict()
        fact = {"registered": _is_registered(lesson, workspace),
                "applicability": assess_lesson(lesson, workspace)}
        size = len(json.dumps([record, fact], ensure_ascii=False).encode("utf-8"))
        if len(selected) >= 128 or used + size > 512_000:
            return {"state": "partial", "lessons": selected, "facts": facts}
        selected.append(record)
        facts[lesson.id] = fact
        used += size
    return {"state": "completed", "lessons": selected, "facts": facts}


def read_advisories(workspace: Path, path: Path, content: str, args: dict, *,
                    read_range: tuple[int, int] | None = None) -> list[dict]:
    inventory = args.get("_lesson_read_inventory")
    if inventory is not None:
        if inventory.get("state") not in {"completed", "partial"}:
            raise LessonInventoryUnavailable("Trusted read advice lookup failed")
        return relevant_diagnostics(workspace, path, content, content, "read_file",
            [Lesson.from_dict(row) for row in inventory["lessons"]],
            read_range=read_range, facts=inventory["facts"])[:10]
    return relevant_diagnostics(workspace, path, content, content, "read_file",
                                _load(workspace, args), read_range=read_range)[:10]
