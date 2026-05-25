"""Lesson System — 知识传承

抽象层（跨项目通用）+ 实例层（单项目具体）。
JSONL 格式，一行一条，方便追加和 grep。
自动收割：sync 成功后检测"反复修改→成功"模式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from backend.core.history import HistoryManager

MEMORY_SOURCES = [".claude", ".codex", ".codebuddy"]

# 知识存储根目录（在 workspace 的 .gitgo/knowledge/ 下）
KNOWLEDGE_DIR = ".gitgo/knowledge"


# ── 数据模型 ─────────────────────────────────────────────

@dataclass
class Lesson:
    """一条知识教训。抽象层不含项目名/文件路径/人名。"""
    id: str = ""
    tech_stack: str = ""
    category: str = ""  # api_migration | architecture | dependency | process
    severity: str = "medium"  # low | medium | high | critical

    trigger: str = ""  # 什么情况下触发（LLM 判据）
    rule: str = ""     # 正确做法（LLM 判据）

    # 解析历史（默认截断，节省 token）
    resolution_history: dict | None = None

    # 检查规则（自动执行）
    check: dict | None = None

    # 元数据
    source: str = "manual"  # manual | auto_harvested
    abstract: bool = False  # True=抽象层, False=实例层
    project_name: str = ""
    verified_at: str = ""
    created_at: str = ""
    verified_count: int = 0
    verified_in: list[str] | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != 0}

    @classmethod
    def from_dict(cls, d: dict) -> Lesson:
        return cls(
            id=d.get("id", ""),
            tech_stack=d.get("tech_stack", ""),
            category=d.get("category", ""),
            severity=d.get("severity", "medium"),
            trigger=d.get("trigger", ""),
            rule=d.get("rule", ""),
            resolution_history=d.get("resolution_history"),
            check=d.get("check"),
            source=d.get("source", "manual"),
            abstract=d.get("abstract", False),
            project_name=d.get("project_name", ""),
            verified_at=d.get("verified_at", ""),
            created_at=d.get("created_at", ""),
            verified_count=d.get("verified_count", 0),
            verified_in=d.get("verified_in"),
        )


# ── 管理器 ───────────────────────────────────────────────

class LessonManager:
    """管理知识的读写和搜索。"""

    @staticmethod
    def _abstract_dir(workspace_path: Path) -> Path:
        return workspace_path / KNOWLEDGE_DIR / "abstract"

    @staticmethod
    def _instance_dir(workspace_path: Path, project_name: str) -> Path:
        return workspace_path / KNOWLEDGE_DIR / "instances" / project_name

    @staticmethod
    def _abstract_path(workspace_path: Path, tech_stack: str) -> Path:
        name = tech_stack.replace("/", "_").replace(" ", "_")
        return LessonManager._abstract_dir(workspace_path) / f"{name}.jsonl"

    @staticmethod
    def _instance_path(workspace_path: Path, project_name: str) -> Path:
        return LessonManager._instance_dir(workspace_path, project_name) / "lessons.jsonl"

    @staticmethod
    def _pending_path(workspace_path: Path, project_name: str) -> Path:
        return LessonManager._instance_dir(workspace_path, project_name) / "pending.jsonl"

    # ── 读取 ────────────────────────────────────────────

    @staticmethod
    def load_abstract(workspace_path: Path, tech_stack: str = "") -> list[Lesson]:
        """加载抽象层知识。tech_stack 为空时加载全部。"""
        lessons = []
        ad = LessonManager._abstract_dir(workspace_path)
        if not ad.exists():
            return lessons
        for fp in sorted(ad.glob("*.jsonl")):
            if tech_stack and fp.stem.replace("_", " ") != tech_stack.replace("/", "_").replace(" ", "_"):
                continue
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    lessons.append(Lesson.from_dict(json.loads(line)))
                except json.JSONDecodeError:
                    continue
        return lessons

    @staticmethod
    def load_instance(workspace_path: Path, project_name: str) -> list[Lesson]:
        """加载实例层知识。"""
        fp = LessonManager._instance_path(workspace_path, project_name)
        if not fp.exists():
            return []
        lessons = []
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lessons.append(Lesson.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return lessons

    @staticmethod
    def load_pending(workspace_path: Path, project_name: str) -> list[Lesson]:
        """加载待确认的自动收割草稿。"""
        fp = LessonManager._pending_path(workspace_path, project_name)
        if not fp.exists():
            return []
        lessons = []
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lessons.append(Lesson.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return lessons

    # ── 写入 ────────────────────────────────────────────

    @staticmethod
    def save(workspace_path: Path, lesson: Lesson) -> Path:
        """保存一条知识。根据 abstract 标志决定写入位置。"""
        if lesson.abstract:
            fp = LessonManager._abstract_path(workspace_path, lesson.tech_stack)
        else:
            fp = LessonManager._instance_path(workspace_path, lesson.project_name)
        fp.parent.mkdir(parents=True, exist_ok=True)

        if not lesson.id:
            lesson.id = f"{lesson.tech_stack or 'general'}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()

        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(lesson.to_dict(), ensure_ascii=False) + "\n")
        return fp

    @staticmethod
    def save_pending(workspace_path: Path, lesson: Lesson) -> Path:
        """保存自动收割草稿。"""
        fp = LessonManager._pending_path(workspace_path, lesson.project_name)
        fp.parent.mkdir(parents=True, exist_ok=True)
        if not lesson.id:
            lesson.id = f"pending_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()
        lesson.source = "auto_harvested"
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(lesson.to_dict(), ensure_ascii=False) + "\n")
        return fp

    # ── 操作 ────────────────────────────────────────────

    @staticmethod
    def verify(workspace_path: Path, lesson_id: str, project_name: str = "") -> Lesson | None:
        """确认一条知识（从 pending 转为正式，或增加 verified_count）。"""
        # 先查 pending
        if project_name:
            pending = LessonManager.load_pending(workspace_path, project_name)
            for i, p in enumerate(pending):
                if p.id == lesson_id:
                    pending.pop(i)
                    # 重写 pending 文件
                    pp = LessonManager._pending_path(workspace_path, project_name)
                    pp.write_text("\n".join(
                        json.dumps(l.to_dict(), ensure_ascii=False) for l in pending
                    ) + ("\n" if pending else ""), encoding="utf-8")
                    # 保存到正式
                    p.verified_at = datetime.now().isoformat()
                    p.verified_count = 1
                    p.source = "auto_harvested"
                    LessonManager.save(workspace_path, p)
                    return p

        # 再查实例层
        if project_name:
            lessons = LessonManager.load_instance(workspace_path, project_name)
            for l in lessons:
                if l.id == lesson_id:
                    l.verified_count += 1
                    l.verified_at = datetime.now().isoformat()
                    l.verified_in = (l.verified_in or []) + [project_name]
                    LessonManager.save(workspace_path, l)
                    return l

        # 查抽象层
        abstract = LessonManager.load_abstract(workspace_path)
        for l in abstract:
            if l.id == lesson_id:
                l.verified_count += 1
                l.verified_at = datetime.now().isoformat()
                l.verified_in = (l.verified_in or []) + [project_name]
                LessonManager.save(workspace_path, l)
                return l

        return None

    @staticmethod
    def promote_to_abstract(
        workspace_path: Path, lesson_id: str,
        project_name: str, tech_stack: str,
    ) -> Lesson | None:
        """将实例层知识提升为抽象层。"""
        lessons = LessonManager.load_instance(workspace_path, project_name)
        for l in lessons:
            if l.id == lesson_id:
                l.abstract = True
                l.tech_stack = tech_stack
                l.project_name = ""  # 抽象层不存项目名
                LessonManager.save(workspace_path, l)
                return l
        return None

    @staticmethod
    def search(
        workspace_path: Path,
        query: str,
        project_name: str = "",
        tech_stack: str = "",
    ) -> list[Lesson]:
        """在抽象层和实例层中搜索。"""
        results = []
        q = query.lower()
        for l in LessonManager.load_abstract(workspace_path, tech_stack):
            text = json.dumps(l.to_dict(), ensure_ascii=False).lower()
            if q in text:
                results.append(l)
        if project_name:
            for l in LessonManager.load_instance(workspace_path, project_name):
                text = json.dumps(l.to_dict(), ensure_ascii=False).lower()
                if q in text:
                    results.append(l)
        return results


# ── 自动收割 ─────────────────────────────────────────────

def harvest_lessons(
    workspace_path: Path,
    project_name: str,
    tech_stack: str = "",
) -> list[Lesson]:
    """sync 成功后自动检测本次 session 中值得记录的教训。

    检测逻辑:
    1. 加载最近的操作历史
    2. 找"反复修改→最终成功"的模式
    3. 对有多次修改的文件生成 pending lesson
    """
    harvested = []
    entries = HistoryManager.load()
    project_entries = [e for e in entries if e.project_name == project_name]

    if len(project_entries) < 2:
        return harvested

    # 找到本 session 的 scan 条目
    recent = project_entries[-20:]
    scan_entries = [
        e for e in recent
        if e.operation == "scan" and e.detail and isinstance(e.detail, dict)
    ]

    if not scan_entries:
        return harvested

    # 检查最近的 scan 中是否有反复修改的模式
    # 简单启发式：同一个文件在不同 scan 中反复出现
    file_occurrences: dict[str, list[str]] = {}
    for e in scan_entries:
        detail = e.detail or {}
        entries_list = detail.get("entries", [])
        for entry in entries_list if isinstance(entries_list, list) else []:
            if isinstance(entry, dict):
                path = entry.get("path", "")
                status = entry.get("status", "")
                if status != "same":
                    file_occurrences.setdefault(path, []).append(e.timestamp)

    for path, timestamps in file_occurrences.items():
        if len(timestamps) >= 3:  # 同一个文件在不同 scan 中出现 3+ 次
            lesson = Lesson(
                tech_stack=tech_stack,
                category="process",
                severity="medium",
                trigger=f"文件 {path} 在短时间内被反复修改（{len(timestamps)}次）",
                rule=f"自动收割: 文件 {path} 经历了多次修改后最终通过 sync 确认。"
                      f"请检查是否有值得记录的教训或模式。",
                source="auto_harvested",
                abstract=False,
                project_name=project_name,
                resolution_history={
                    "file": path,
                    "occurrences": timestamps,
                    "show_by_default": False,
                },
            )
            lesson.id = f"harvest_{project_name}_{path.replace('/', '_').replace('.', '_')}"
            LessonManager.save_pending(workspace_path, lesson)
            harvested.append(lesson)

    return harvested
