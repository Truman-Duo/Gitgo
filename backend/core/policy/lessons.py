"""Lesson trigger matching — check if changed files match saved lesson patterns."""

from pathlib import Path
from typing import TYPE_CHECKING
from backend.core.policy.base import PolicyCheck

if TYPE_CHECKING:
    from backend.core.sync_session import SyncSession
    from backend.core.config import ProjectConfig


class LessonTriggerCheck(PolicyCheck):
    name = "lesson_triggers"
    description = "Match changed files against lesson triggers"
    applicable_task_kinds = frozenset({"action", "supervisor", "review"})

    def __init__(self, lessons: list | None = None,
                 changed_files: list[str] | None = None):
        """lessons 可选注入——loop 已加载时传入，避免重复读文件。"""
        self._lessons = lessons
        self._changed_files = self._normalize_changed_files(changed_files)

    @staticmethod
    def _normalize_changed_files(changed_files: list[str] | None) -> list[str]:
        return list(dict.fromkeys(
            str(item).replace("\\", "/").lstrip("./")
            for item in (changed_files or []) if str(item).strip()
        ))

    def set_changed_files(self, changed_files: list[str]) -> None:
        """Attach this run's watcher facts without retaining them globally."""
        self._changed_files = self._normalize_changed_files(changed_files)

    def check(self, session: "SyncSession",
              _project: "ProjectConfig") -> list[dict]:
        from backend.core.knowledge.lesson import LessonManager
        import re

        matched = []
        ws = session.workspace_path

        scanned_files = [e.rel_path for e in session.entries if e.status != "same"]
        # A project can intentionally have no release repository yet.  In that
        # mode SyncSession cannot compute a workspace-vs-release diff, but the
        # daemon watcher still owns an exact changed-file event.  Merge both
        # evidence sources so knowledge matching does not depend on Git setup.
        changed_files = list(dict.fromkeys([
            *self._changed_files,
            *self._normalize_changed_files(scanned_files),
        ]))
        changed_content = ""
        for rel_path in changed_files:
            try:
                content = (Path(ws) / rel_path).read_text(
                    encoding="utf-8", errors="ignore")
                changed_content += content[:2000]
            except OSError:
                pass

        lessons = self._lessons
        if lessons is None:
            lessons = LessonManager.load_abstract(Path(ws))
            if session.project.name:
                lessons += LessonManager.load_instance(Path(ws), session.project.name)
                lessons += LessonManager.load_pending(Path(ws), session.project.name)

        for lesson in lessons:
            from backend.core.knowledge.applicability import assess_lesson
            applicability = assess_lesson(lesson, ws)
            if applicability.get("state") != "current":
                # Stale knowledge remains searchable but cannot create a
                # current governance fact or a tool gate.
                continue
            trigger = getattr(lesson, 'trigger', '')
            if not trigger:
                continue
            matched_trigger = any(
                trigger.lower() in f.lower() for f in changed_files
            ) or trigger.lower() in changed_content.lower()
            match_mode = "lexical_candidate"
            check = getattr(lesson, 'check', None)

            # ``trigger`` answers relevance.  A structured checker answers a
            # different question: whether an observed state is an
            # authoritative violation.  Older harvested records only carried
            # ``{"pattern": ...}``, with no polarity; some providers emitted
            # a success pattern while others emitted a violation pattern.
            # Treating either as the trigger inverted governance in one of the
            # two cases and also hid the lesson when the check was unmet.
            # Only an explicitly registered violation checker may upgrade a
            # lexical match to enforcement authority.
            if (
                matched_trigger
                and isinstance(check, dict)
                and check.get("pattern")
                and check.get("mode") == "violation_pattern"
                and check.get("authority") == "registered"
            ):
                try:
                    if re.search(check["pattern"], changed_content):
                        match_mode = "registered_pattern"
                except re.error:
                    # An invalid registered checker has no enforcement
                    # authority.  Keep the lesson available to retrieval rather
                    # than silently converting it to a broad lexical gate.
                    match_mode = "lexical_candidate"

            if matched_trigger:
                # Track which file triggered this match
                matched_file = ""
                for f in changed_files:
                    if trigger.lower() in f.lower():
                        matched_file = f
                        break
                if not matched_file and trigger.lower() in changed_content.lower():
                    matched_file = changed_files[0] if changed_files else ""

                matched.append({
                    "lesson_id": getattr(lesson, 'id', ''),
                    "trigger": trigger,
                    "rule": getattr(lesson, 'rule', ''),
                    "severity": getattr(lesson, 'severity', 'medium'),
                    "category": getattr(lesson, 'category', ''),
                    "has_check": bool(check and check.get("pattern")),
                    "match_mode": match_mode,
                    "file": matched_file,
                    "dangerous_tools": getattr(lesson, 'dangerous_tools', None) or [],
                    "prerequisite_tools": getattr(lesson, 'prerequisite_tools', None) or [],
                    "required_tools": getattr(lesson, 'required_tools', None) or [],
                })

        return matched
