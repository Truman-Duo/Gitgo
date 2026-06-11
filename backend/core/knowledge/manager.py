import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import Lesson

# 知识存储根目录
KNOWLEDGE_DIR = ".gitgo/knowledge"
MEMORY_SOURCES = [".claude", ".codex", ".codebuddy"]


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
        """保存自动收割草稿（去重：相同 id 不重复写入）。"""
        fp = LessonManager._pending_path(workspace_path, lesson.project_name)
        fp.parent.mkdir(parents=True, exist_ok=True)
        if not lesson.id:
            lesson.id = f"pending_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()
        lesson.source = "auto_harvested"

        # 去重：检查相同 id 是否已存在
        existing_ids = set()
        if fp.exists():
            for line in fp.read_text(encoding="utf-8").splitlines():
                try:
                    existing = json.loads(line.strip())
                    existing_ids.add(existing.get("id", ""))
                except json.JSONDecodeError:
                    continue
        if lesson.id in existing_ids:
            return fp  # 跳过重复

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

