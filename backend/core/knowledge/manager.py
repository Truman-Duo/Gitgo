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
        lessons = cls._load(
            workspace_path, scope="abstract",
            tech_stack=tech_stack or None,
        )
        from backend.core.storage.bindings import get_shared_knowledge_storage
        shared = get_shared_knowledge_storage(cls._runtime(workspace_path).paths.state_home)
        if shared is not None:
            lessons += [Lesson.from_dict(item) for item in shared.load_lesson_records(
                scope="abstract", tech_stack=tech_stack or None,
            )]
        return lessons

    @classmethod
    def share_abstract(cls, workspace_path: Path, lesson_id: str, *,
                       trigger: str, rule: str, tech_stack: str) -> Lesson:
        """Explicitly publish reviewed text, not an automatic copy of private history.

        Sharing never exports evidence file paths, executable checkers or tool
        constraints. Local promotion remains local for compatibility.
        """
        runtime = cls._runtime(workspace_path)
        found = runtime.get_lesson_record(lesson_id)
        if found is None or found[0] != "abstract":
            raise ValueError("only a locally abstracted lesson can be shared")
        if not trigger.strip() or not rule.strip() or not tech_stack.strip():
            raise ValueError("reviewed trigger, rule and applicability tech_stack are required")
        from backend.core.storage.bindings import get_shared_knowledge_storage
        shared = get_shared_knowledge_storage(runtime.paths.state_home, create=True)
        identity = hashlib.sha256(f"{runtime.paths.project_id}:{lesson_id}".encode()).hexdigest()[:24]
        lesson = Lesson(
            id="shared_" + identity, trigger=trigger.strip(), rule=rule.strip(),
            tech_stack=tech_stack.strip(), abstract=True, source="shared", origin="explicit_share",
            category=str(found[1].get("category") or ""), severity=str(found[1].get("severity") or "medium"),
            created_at=datetime.now().isoformat(),
            evidence={"schema_version": 1, "scope": "user_shared",
                      "source_project_id": runtime.paths.project_id, "source_lesson_id": lesson_id,
                      "source_record_digest": hashlib.sha256(json.dumps(found[1], sort_keys=True).encode()).hexdigest()},
        )
        from .review import mark_reviewed
        mark_reviewed(lesson, kind="explicit_share", reference=lesson_id)
        shared.upsert_lesson_record(lesson.to_dict(), scope="abstract",
                                   content_hash=lesson_content_hash(lesson.trigger, lesson.rule))
        return lesson

    @classmethod
    def revoke_shared(cls, workspace_path: Path, lesson_id: str) -> bool:
        from backend.core.storage.bindings import get_shared_knowledge_storage
        runtime = cls._runtime(workspace_path)
        shared = get_shared_knowledge_storage(runtime.paths.state_home)
        if shared is None:
            return False
        found = shared.get_lesson_record(lesson_id)
        if found is None or (found[1].get("evidence") or {}).get("source_project_id") != runtime.paths.project_id:
            return False
        return shared.delete_lesson_record(lesson_id, scopes=("abstract",))

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
        # This internal authoring API is not exposed to model-generated harvest.
        # Legacy imports deliberately bypass it and acquire no review by source name.
        if lesson.source == "manual":
            from .review import mark_reviewed
            mark_reviewed(lesson, kind="author_supplied", reference=lesson.id)
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
        *, user_confirmation: bool = False, expected_version: str = "",
    ) -> Lesson | None:
        cls._migrate_legacy(workspace_path)
        runtime = cls._runtime(workspace_path)
        found = runtime.get_lesson_record(lesson_id)
        if found is None:
            from backend.core.storage.bindings import get_shared_knowledge_storage
            shared = get_shared_knowledge_storage(runtime.paths.state_home)
            if shared is not None:
                found = shared.get_lesson_record(lesson_id)
                if found is not None:
                    return Lesson.from_dict(shared.record_lesson_verification(
                        lesson_id, runtime.paths.project_id, project_name,
                        user_confirmation=user_confirmation, expected_version=expected_version,
                    ))
        if found is None:
            return None
        scope, raw = found
        lesson = Lesson.from_dict(raw)
        if scope in {"pending", "instance"} and project_name:
            if lesson.project_name != project_name:
                return None
        if scope in {"pending", "instance", "abstract"}:
            return Lesson.from_dict(runtime.record_lesson_verification(
                lesson_id, runtime.paths.project_id, project_name,
                user_confirmation=user_confirmation, expected_version=expected_version,
            ))
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
        from .review import can_consume
        if lesson.project_name != project_name or not can_consume(lesson):
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
        project_name: str = "", tech_stack: str = "", *, include_unreviewed: bool = False,
    ) -> list[Lesson]:
        results: list[Lesson] = []
        seen: set[str] = set()
        needle = query.lower()

        def add_matches(lessons: list[Lesson]) -> None:
            from .review import can_consume
            for lesson in lessons:
                if not include_unreviewed and not can_consume(lesson):
                    continue
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
        if found is None:
            from backend.core.storage.bindings import get_shared_knowledge_storage
            shared = get_shared_knowledge_storage(runtime.paths.state_home)
            if shared is not None and lesson.recent_retrievals:
                shared.record_lesson_retrieval(lesson.id, lesson.recent_retrievals[-1])
            return
        if found[0] != "abstract" and found[1].get("project_name") != project_name:
            return
        if lesson.recent_retrievals:
            runtime.record_lesson_retrieval(lesson.id, lesson.recent_retrievals[-1])

    @classmethod
    def pending_count(cls, workspace_path: Path, project_name: str) -> int:
        cls._migrate_legacy(workspace_path)
        return cls._runtime(workspace_path).lesson_count(
            scope="pending", project_name=project_name,
        )
