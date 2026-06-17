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

    def __init__(self, lessons: list | None = None):
        """lessons 可选注入——loop 已加载时传入，避免重复读文件。"""
        self._lessons = lessons

    def check(self, session: "SyncSession",
              _project: "ProjectConfig") -> list[dict]:
        from backend.core.knowledge.lesson import LessonManager
        import re

        matched = []
        ws = session.workspace_path

        changed_files = [e.rel_path for e in session.entries if e.status != "same"]
        changed_content = ""
        for e in session.entries:
            if e.status != "same":
                try:
                    content = (Path(ws) / e.rel_path).read_text(
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
            trigger = getattr(lesson, 'trigger', '')
            if not trigger:
                continue
            matched_trigger = False
            check = getattr(lesson, 'check', None)

            if check and isinstance(check, dict) and check.get("pattern"):
                if re.search(check["pattern"], changed_content):
                    matched_trigger = True
            else:
                for f in changed_files:
                    if trigger.lower() in f.lower():
                        matched_trigger = True
                        break
                if not matched_trigger and trigger.lower() in changed_content.lower():
                    matched_trigger = True

            if matched_trigger:
                matched.append({
                    "lesson_id": getattr(lesson, 'id', ''),
                    "trigger": trigger,
                    "rule": getattr(lesson, 'rule', ''),
                    "severity": getattr(lesson, 'severity', 'medium'),
                    "category": getattr(lesson, 'category', ''),
                    "has_check": bool(check and check.get("pattern")),
                })

        return matched
