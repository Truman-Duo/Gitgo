"""测试 Lesson 系统 — 知识传承"""

import shutil
import tempfile
from pathlib import Path

from backend.core.knowledge.lesson import (
    Lesson,
    LessonManager,
    harvest_lessons,
)


def _tmp():
    d = tempfile.mkdtemp()
    return Path(d)


def _rm(p: Path):
    shutil.rmtree(str(p), ignore_errors=True)


# ── Lesson 数据模型 ─────────────────────────────────────

def test_lesson_to_dict_minimal():
    l = Lesson(trigger="useEffect double fire", rule="use cleanup properly")
    d = l.to_dict()
    assert d["trigger"] == "useEffect double fire"
    assert d["rule"] == "use cleanup properly"
    assert "id" not in d  # empty fields excluded


def test_lesson_to_dict_full():
    l = Lesson(
        id="qt_001", tech_stack="qt6", category="api_migration",
        severity="high", trigger="Qt5→Qt6 icons", rule="use StandardPixmap",
        source="auto_harvested", abstract=False, project_name="test",
        verified_count=3, verified_in=["a", "b"],
    )
    d = l.to_dict()
    assert d["id"] == "qt_001"
    assert d["verified_count"] == 3
    assert d["verified_in"] == ["a", "b"]


def test_lesson_from_dict():
    d = {"trigger": "x", "rule": "y", "severity": "critical"}
    l = Lesson.from_dict(d)
    assert l.trigger == "x"
    assert l.severity == "critical"
    assert l.source == "manual"  # default


# ── LessonManager save/load ─────────────────────────────

def test_save_and_load_instance():
    p = _tmp()
    try:
        l = Lesson(trigger="test trigger", rule="test rule", project_name="proj")
        LessonManager.save(p, l)
        loaded = LessonManager.load_instance(p, "proj")
        assert len(loaded) == 1
        assert loaded[0].trigger == "test trigger"
    finally:
        _rm(p)


def test_save_and_load_abstract():
    p = _tmp()
    try:
        l = Lesson(trigger="cross project", rule="always do X",
                   abstract=True, tech_stack="python")
        LessonManager.save(p, l)
        loaded = LessonManager.load_abstract(p, "python")
        assert len(loaded) >= 1
        assert any(ll.trigger == "cross project" for ll in loaded)
    finally:
        _rm(p)


def test_save_pending():
    p = _tmp()
    try:
        l = Lesson(trigger="pending test", project_name="proj")
        LessonManager.save_pending(p, l)
        pending = LessonManager.load_pending(p, "proj")
        assert len(pending) == 1
        assert pending[0].source == "auto_harvested"
    finally:
        _rm(p)


def test_load_empty():
    p = _tmp()
    try:
        assert LessonManager.load_instance(p, "noproj") == []
        assert LessonManager.load_abstract(p) == []
        assert LessonManager.load_pending(p, "noproj") == []
    finally:
        _rm(p)


# ── verify ──────────────────────────────────────────────

def test_verify_pending_to_instance():
    p = _tmp()
    try:
        l = Lesson(trigger="verify me", project_name="proj")
        fp = LessonManager.save_pending(p, l)
        # re-read to get assigned ID
        pending = LessonManager.load_pending(p, "proj")
        assert len(pending) == 1
        lid = pending[0].id

        result = LessonManager.verify(p, lid, project_name="proj")
        assert result is not None
        assert result.verified_count == 1
        # pending should be cleared
        assert LessonManager.load_pending(p, "proj") == []
        # instance should have the lesson
        instances = LessonManager.load_instance(p, "proj")
        assert len(instances) == 1
    finally:
        _rm(p)


# ── promote_to_abstract ─────────────────────────────────

def test_promote_to_abstract():
    p = _tmp()
    try:
        l = Lesson(trigger="promote me", project_name="proj")
        LessonManager.save(p, l)
        instances = LessonManager.load_instance(p, "proj")
        lid = instances[0].id

        result = LessonManager.promote_to_abstract(
            p, lid, project_name="proj", tech_stack="python",
        )
        assert result is not None
        assert result.abstract is True
        assert result.project_name == ""

        # Should now appear in abstract
        abstract = LessonManager.load_abstract(p, "python")
        assert any(ll.id == lid for ll in abstract)
    finally:
        _rm(p)


# ── search ──────────────────────────────────────────────

def test_search_finds_in_instance():
    p = _tmp()
    try:
        LessonManager.save(p, Lesson(trigger="unique_keyword_xyz", project_name="proj"))
        results = LessonManager.search(p, "unique_keyword_xyz", project_name="proj")
        assert len(results) == 1
    finally:
        _rm(p)


def test_search_no_match():
    p = _tmp()
    try:
        results = LessonManager.search(p, "nonexistent_99999")
        assert results == []
    finally:
        _rm(p)


# ── harvest_lessons ─────────────────────────────────────

def test_harvest_no_history():
    p = _tmp()
    try:
        result = harvest_lessons(p, "empty_proj")
        assert result == []
    finally:
        _rm(p)


def test_lesson_jsonl_append():
    p = _tmp()
    try:
        LessonManager.save(p, Lesson(trigger="lesson1", project_name="proj"))
        LessonManager.save(p, Lesson(trigger="lesson2", project_name="proj"))
        loaded = LessonManager.load_instance(p, "proj")
        assert len(loaded) == 2
    finally:
        _rm(p)
