"""Transcript Builder —— 结构化转录生成器。

v0.36 Phase 2: L7 Task Transcript (XML) + L8 返回转录 (JSON) + Compact 约束提取。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class TaskTranscriptBuilder:
    """B Agent 的任务转录生成器。

    每步工具执行后追加，不进入 session.messages。
    只在 return_context 和 compact 时使用。
    """

    task_id: str = ""
    steps: list[dict] = field(default_factory=list)
    _started_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def append_tool_call(self, step: int, tool_name: str, args: dict,
                         result: dict, duration_ms: float = 0):
        self.steps.append({
            "n": step,
            "type": "tool",
            "tool": tool_name,
            "args_summary": _summarize_args(tool_name, args),
            "result_summary": _summarize_result(tool_name, result),
            "time_ms": int(duration_ms),
        })

    def append_governance_event(self, step: int, reason: str):
        self.steps.append({
            "n": step,
            "type": "governance",
            "reason": reason,
        })

    def append_completion(self, step: int, tools_used: list[str],
                          lessons_triggered: list[str]):
        self.steps.append({
            "n": step,
            "type": "completed",
            "tools_used": tools_used,
            "lessons_triggered": lessons_triggered,
        })

    def to_xml(self) -> str:
        """生成 L7 任务转录（LLM 优化格式）。"""
        lines = [f'<task id="{self.task_id}" started="{self._started_at}">']
        for s in self.steps:
            if s["type"] == "tool":
                lines.append(
                    f'<step n="{s["n"]}" tool="{s["tool"]}" '
                    f'{s["args_summary"]} {s["result_summary"]} '
                    f'time_ms="{s["time_ms"]}"/>'
                )
            elif s["type"] == "governance":
                lines.append(
                    f'<step n="{s["n"]}" governance="blocked" '
                    f'reason="{s["reason"]}"/>'
                )
            elif s["type"] == "completed":
                lines.append(
                    f'<step n="{s["n"]}" status="completed" '
                    f'tools_used="{",".join(s["tools_used"])}" '
                    f'lessons="{",".join(s["lessons_triggered"])}"/>'
                )
        lines.append("</task>")
        return "\n".join(lines)

    def to_return_context(self) -> dict:
        """生成 B→A 的结构化返回转录。"""
        tools = []
        lessons = []
        governance_events = []
        completed = False

        for s in self.steps:
            if s["type"] == "tool" and s["tool"] not in tools:
                tools.append(s["tool"])
            if s["type"] == "governance":
                governance_events.append({"step": s["n"], "reason": s["reason"]})
            if s["type"] == "completed":
                lessons = s.get("lessons_triggered", [])
                completed = True

        return {
            "task": self.task_id,
            "status": "COMPLETED" if completed else "INCOMPLETE",
            "steps": len(self.steps),
            "tools": tools,
            "lessons_triggered": lessons,
            "governance_events": governance_events,
        }

    @staticmethod
    def extract_compact_constraints(session_messages: list[dict],
                                    lessons: list = None) -> list[dict]:
        """Compact JSON Schema: 规则填充 constraints，非 LLM 决定。

        填充源:
        1. 硬规则抓取 (否定句/禁止句)
        2. Lesson 约束继承
        """
        constraints = []

        # 源1: 硬规则抓取
        negation_re = re.compile(
            r'(?:不要|禁止|不能|先别|do not|don\'t|must not|never)\s*(.{5,120})',
            re.I,
        )
        for msg in session_messages:
            content = msg.get("content", "")
            for match in negation_re.findall(content):
                constraints.append({
                    "rule": match.strip(),
                    "scope": "detected",
                    "source": "hard_extract",
                })

        # 源2: Lesson 约束继承
        if lessons:
            for lesson in lessons:
                rule = getattr(lesson, "rule", "")
                if rule:
                    constraints.append({
                        "rule": rule,
                        "scope": "global",
                        "source": f"lesson_{getattr(lesson, 'id', '?')}",
                    })

        return constraints


def _summarize_args(tool_name: str, args: dict) -> str:
    """提取工具参数中的关键字段。"""
    if tool_name == "recall_grep":
        return f'query="{args.get("query", "")[:40]}"'
    elif tool_name == "scan":
        return ""
    elif tool_name == "formalize":
        msg = args.get("message", "")[:60]
        return f'message="{msg}"'
    return ""


def _summarize_result(tool_name: str, result) -> str:
    """提取工具结果中的关键量化字段。"""
    r = result if isinstance(result, dict) else {}
    data = r.get("data", r) if isinstance(r, dict) else {}

    if tool_name == "recall_grep":
        return f'matches="{data.get("total_matches", 0)}"'
    elif tool_name == "scan":
        status = data.get("status_dict", {})
        return (
            f'files="{status.get("entries_total", "?")}" '
            f'changed="{status.get("entries_changed", "?")}"'
        )
    elif tool_name == "test":
        return f'passed="{data.get("passed", "?")}" failed="{data.get("failed", "?")}"'
    return ""
