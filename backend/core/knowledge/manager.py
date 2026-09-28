"""Knowledge persistence through the project SQLite/CAS storage facade."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from backend.core.storage import StorageRuntime, bind_storage, get_storage

from .models import Lesson, lesson_content_hash


KNOWLEDGE_DIR = ".gitgo/knowledge"
MEMORY_SOURCES = [".claude", ".codex", ".codebuddy"]


class LessonManager:
    """Manage pending, project and abstract lessons as one SQLite authority."""

    _migration_locks: dict[str, threading.RLock] = {}
    _migration_guard = threading.Lock()

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

    @classmethod
    def bind_storage(
        cls, workspace_path: Path, storage: StorageRuntime,
    ) -> None:
        bind_storage(workspace_path, storage)
        cls._migrate_legacy(workspace_path, storage=storage)

    @classmethod
    def _runtime(
        cls, workspace_path: Path, storage: StorageRuntime | None = None,
    ) -> StorageRuntime:
        return storage or get_storage(workspace_path)

    @classmethod
    def _lock_for(cls, workspace_path: Path) -> threading.RLock:
        key = str(workspace_path.resolve()).casefold()
        with cls._migration_guard:
            return cls._migration_locks.setdefault(key, threading.RLock())

    @classmethod
    def _migrate_legacy(
        cls, workspace_path: Path, *, storage: StorageRuntime | None = None,
    ) -> None:
        workspace_path = Path(workspace_path).resolve()
        root = workspace_path / KNOWLEDGE_DIR
        if not root.exists():
            return
        with cls._lock_for(workspace_path):
            if not root.exists():
                return
            runtime = cls._runtime(workspace_path, storage)
            sources: list[tuple[str, str, str, Path]] = []
            abstract_dir = root / "abstract"
            if abstract_dir.exists():
                for path in sorted(abstract_dir.glob("*.jsonl")):
                    sources.append(("abstract", "", path.stem, path))
            instances_dir = root / "instances"
            if instances_dir.exists():
                for project_dir in sorted(
                    item for item in instances_dir.iterdir() if item.is_dir()
                ):
                    sources.append((
                        "instance", project_dir.name, "",
                        project_dir / "lessons.jsonl",
                    ))
                    sources.append((
                        "pending", project_dir.name, "",
                        project_dir / "pending.jsonl",
                    ))
            try:
                for scope, project_name, tech_stack, path in sources:
                    if not path.exists():
                        continue
                    for index, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines()
                    ):
                        if not line.strip():
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        lesson = Lesson.from_dict(raw)
                        if scope == "abstract":
                            lesson.abstract = True
                            lesson.project_name = ""
                            lesson.tech_stack = lesson.tech_stack or tech_stack
                        else:
                            lesson.abstract = False
                            lesson.project_name = lesson.project_name or project_name
                        if not lesson.created_at:
                            lesson.created_at = datetime.now().isoformat()
                        if not lesson.id:
                            encoded = json.dumps(
                                raw, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                            lesson.id = "legacy_lesson_" + hashlib.sha256(
                                encoded + b"\0" + str(index).encode("ascii")
                            ).hexdigest()[:24]
                        runtime.upsert_lesson_record(
                            lesson.to_dict(), scope=scope,
                            content_hash=lesson_content_hash(
                                lesson.trigger, lesson.rule,
                            ),
                        )
                suffix = datetime.now().strftime("%Y%m%dT%H%M%S")
                archived = root.with_name(f"knowledge.legacy-imported-{suffix}")
                counter = 1
                while archived.exists():
                    archived = root.with_name(
                        f"knowledge.legacy-imported-{suffix}-{counter}"
                    )
                    counter += 1
                root.replace(archived)
            except Exception:
                raise

    @classmethod
    def _load(
        cls, workspace_path: Path, *, scope: str,
        project_name: str | None = None, tech_stack: str | None = None,
    ) -> list[Lesson]:
        cls._migrate_legacy(workspace_path)
        return [
            Lesson.from_dict(item)
            for item in cls._runtime(workspace_path).load_lesson_records(
                scope=scope, project_name=project_name, tech_stack=tech_stack,
            )
        ]

    @classmethod
    def load_abstract(
        cls, workspace_path: Path, tech_stack: str = "",
    ) -> list[Lesson]:
        return cls._load(
            workspace_path, scope="abstract",
            tech_stack=tech_stack or None,
        )

    @classmethod
    def load_instance(
        cls, workspace_path: Path, project_name: str,
    ) -> list[Lesson]:
        return cls._load(
            workspace_path, scope="instance", project_name=project_name,
        )

    @classmethod
    def load_pending(
        cls, workspace_path: Path, project_name: str,
    ) -> list[Lesson]:
        return cls._load(
            workspace_path, scope="pending", project_name=project_name,
        )

    @classmethod
    def save(cls, workspace_path: Path, lesson: Lesson) -> Path:
        if not lesson.id:
            lesson.id = (
                f"{lesson.tech_stack or 'general'}_"
                f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
            )
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()
        scope = "abstract" if lesson.abstract else "instance"
        cls._migrate_legacy(workspace_path)
        runtime = cls._runtime(workspace_path)
        runtime.upsert_lesson_record(
            lesson.to_dict(), scope=scope,
            content_hash=lesson_content_hash(lesson.trigger, lesson.rule),
        )
        return runtime.paths.state_db

    @classmethod
    def save_pending(cls, workspace_path: Path, lesson: Lesson) -> Path:
        if not lesson.id:
            lesson.id = lesson_content_hash(lesson.trigger, lesson.rule)[:12]
        if not lesson.created_at:
            lesson.created_at = datetime.now().isoformat()
        lesson.source = "auto_harvested"
        lesson.abstract = False
        cls._migrate_legacy(workspace_path)
        runtime = cls._runtime(workspace_path)
        lesson.id = runtime.upsert_lesson_record(
            lesson.to_dict(), scope="pending",
            content_hash=lesson_content_hash(lesson.trigger, lesson.rule),
        )
        return runtime.paths.state_db

    @classmethod
    def verify(
        cls, workspace_path: Path, lesson_id: str, project_name: str = "",
    ) -> Lesson | None:
        cls._migrate_legacy(workspace_path)
        runtime = cls._runtime(workspace_path)
        found = runtime.get_lesson_record(lesson_id)
        if found is None:
            return None
        scope, raw = found
        lesson = Lesson.from_dict(raw)
        if scope in {"pending", "instance"} and project_name:
            if lesson.project_name != project_name:
                return None
        if scope == "pending":
            lesson.verified_at = datetime.now().isoformat()
            lesson.verified_count = 1
            lesson.source = "auto_harvested"
            runtime.upsert_lesson_record(
                lesson.to_dict(), scope="instance",
                content_hash=lesson_content_hash(lesson.trigger, lesson.rule),
            )
            return lesson
        if scope in {"instance", "abstract"}:
            lesson.verified_count += 1
            lesson.verified_at = datetime.now().isoformat()
            lesson.verified_in = (lesson.verified_in or []) + [project_name]
            runtime.upsert_lesson_record(
                lesson.to_dict(), scope=scope,
                content_hash=lesson_content_hash(lesson.trigger, lesson.rule),
            )
            return lesson
        return None

    @classmethod
    def promote_to_abstract(
        cls, workspace_path: Path, lesson_id: str,
        project_name: str, tech_stack: str,
    ) -> Lesson | None:
        cls._migrate_legacy(workspace_path)
        runtime = cls._runtime(workspace_path)
        found = runtime.get_lesson_record(lesson_id)
        if found is None or found[0] != "instance":
            return None
        lesson = Lesson.from_dict(found[1])
        if lesson.project_name != project_name:
            return None
        lesson.abstract = True
        lesson.tech_stack = tech_stack
        lesson.project_name = ""
        runtime.upsert_lesson_record(
            lesson.to_dict(), scope="abstract",
            content_hash=lesson_content_hash(lesson.trigger, lesson.rule),
        )
        return lesson

    @classmethod
    def search(
        cls, workspace_path: Path, query: str,
        project_name: str = "", tech_stack: str = "",
    ) -> list[Lesson]:
        results: list[Lesson] = []
        seen: set[str] = set()
        needle = query.lower()

        def add_matches(lessons: list[Lesson]) -> None:
            for lesson in lessons:
                text = json.dumps(lesson.to_dict(), ensure_ascii=False).lower()
                identity = lesson.id or text
                if needle in text and identity not in seen:
                    seen.add(identity)
                    results.append(lesson)

        add_matches(cls.load_abstract(workspace_path, tech_stack))
        if project_name:
            add_matches(cls.load_instance(workspace_path, project_name))
            add_matches(cls.load_pending(workspace_path, project_name))
        return results

    @classmethod
    def discard_lesson(
        cls, workspace_path: Path, lesson_id: str, project_name: str = "",
    ) -> bool:
        cls._migrate_legacy(workspace_path)
        return cls._runtime(workspace_path).delete_lesson_record(
            lesson_id,
            project_name=project_name or None,
            scopes=("pending", "instance"),
        )

    @classmethod
    def revert_to_pending(
        cls, workspace_path: Path, lesson_id: str, project_name: str,
    ) -> Lesson | None:
        cls._migrate_legacy(workspace_path)
        runtime = cls._runtime(workspace_path)
        found = runtime.get_lesson_record(lesson_id)
        if found is None or found[0] != "instance":
            return None
        lesson = Lesson.from_dict(found[1])
        if lesson.project_name != project_name or lesson.origin != "auto_verify":
            return None
        lesson.origin = "auto_verify_reverted"
        runtime.upsert_lesson_record(
            lesson.to_dict(), scope="pending",
            content_hash=lesson_content_hash(lesson.trigger, lesson.rule),
        )
        return lesson

    @classmethod
    def _save_with_retrieval_update(
        cls, workspace_path: Path, lesson: Lesson, project_name: str,
    ) -> None:
        cls._migrate_legacy(workspace_path)
        runtime = cls._runtime(workspace_path)
        found = runtime.get_lesson_record(lesson.id)
        if found is None or found[0] not in {"instance", "pending"}:
            return
        current = Lesson.from_dict(found[1])
        if current.project_name != project_name:
            return
        runtime.upsert_lesson_record(
            lesson.to_dict(), scope=found[0],
            content_hash=lesson_content_hash(lesson.trigger, lesson.rule),
        )

    @classmethod
    def pending_count(cls, workspace_path: Path, project_name: str) -> int:
        cls._migrate_legacy(workspace_path)
        return cls._runtime(workspace_path).lesson_count(
            scope="pending", project_name=project_name,
        )
