"""测试 CommitTemplate / TemplateManager / build_commit_template 集成"""

import json
import tempfile
from pathlib import Path

import pytest

from backend.core.template_manager import (
    CommitTemplate,
    TemplateManager,
    _BUILTIN_DEFAULT,
    _DEFAULT_HEADER,
    _DEFAULT_BODY,
)
from backend.core.operations.git import build_commit_template
from backend.core.operations.models import CommitInfo
from backend.core.config import ProjectConfig
from backend.models import RepoNode, FileAccess, FileAccessKind


# ── CommitTemplate 数据类 ──────────────────────────────

def test_default_template_matches_builtin():
    t = CommitTemplate()
    assert t.name == "default"
    assert t.header_format == _DEFAULT_HEADER
    assert t.body_format == _DEFAULT_BODY
    assert t.prefix_override is None


def test_custom_template_fields():
    t = CommitTemplate(
        name="feat",
        description="Feature template",
        header_format="feat: {subject}",
        body_format="{commit_list}",
        prefix_override="CUSTOM",
    )
    assert t.name == "feat"
    assert t.prefix_override == "CUSTOM"


# ── TemplateManager load（无文件） ──────────────────────

def test_load_returns_default_when_no_file(monkeypatch):
    monkeypatch.setattr(TemplateManager, "_default_path",
                        lambda: Path(tempfile.gettempdir()) / "nonexistent_commit_config.json")
    templates = TemplateManager.load()
    assert len(templates) == 1
    assert templates[0].name == "default"


# ── TemplateManager save / load 往返 ──────────────────

def test_save_and_load_roundtrip(monkeypatch):
    tmp = Path(tempfile.gettempdir()) / "test_commit_config.json"
    monkeypatch.setattr(TemplateManager, "_default_path", lambda: tmp)

    templates = [
        CommitTemplate(name="default", description="Default"),
        CommitTemplate(name="custom", description="My custom format",
                       header_format="[{prefix}-{number}] {type_str}: {subject}"),
    ]
    path = TemplateManager.save(templates)
    assert path == tmp
    assert tmp.exists()

    loaded = TemplateManager.load()
    assert len(loaded) == 2
    assert loaded[0].name == "default"
    assert loaded[1].name == "custom"
    assert loaded[1].header_format == "[{prefix}-{number}] {type_str}: {subject}"

    tmp.unlink()


# ── TemplateManager.get_template ──────────────────────

def test_get_template_finds_by_name(monkeypatch):
    tmp = Path(tempfile.gettempdir()) / "test_commit_config2.json"
    monkeypatch.setattr(TemplateManager, "_default_path", lambda: tmp)
    TemplateManager.save([
        CommitTemplate(name="a"),
        CommitTemplate(name="b"),
    ])
    assert TemplateManager.get_template("a") is not None
    assert TemplateManager.get_template("b") is not None
    assert TemplateManager.get_template("c") is None
    tmp.unlink()


def test_get_default_returns_first(monkeypatch):
    tmp = Path(tempfile.gettempdir()) / "test_commit_config3.json"
    monkeypatch.setattr(TemplateManager, "_default_path", lambda: tmp)
    TemplateManager.save([
        CommitTemplate(name="first"),
        CommitTemplate(name="second"),
    ])
    t = TemplateManager.get_default()
    assert t.name == "first"
    tmp.unlink()


# ── build_commit_template 向后兼容 ─────────────────────

def _make_project(prefix="TEST"):
    return ProjectConfig(
        name="TestProj",
        workspace=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/ws")),
        release=RepoNode(file_access=FileAccess(kind=FileAccessKind.LOCAL, path="/rel")),
        commit_format={"prefix": prefix, "number_start": 0, "template_name": "default"},
    )


def test_backward_compat_default_template():
    """使用默认模板的输出应与旧硬编码格式一致"""
    project = _make_project()
    commits = [
        CommitInfo(hash="abc", type="feat", scope="core", subject="Add login"),
    ]
    result = build_commit_template(commits, project)
    assert "[TEST-" in result
    assert "feat(core): Add login" in result
    assert "Project: TestProj" in result
    assert "Synced from 1 workspace commit(s)" in result


def test_multi_commit_template():
    project = _make_project()
    commits = [
        CommitInfo(hash="aaa", type="feat", scope="api", subject="Endpoint A"),
        CommitInfo(hash="bbb", type="fix", scope="api", subject="Bug fix B"),
    ]
    result = build_commit_template(commits, project)
    assert "feat/fix" in result
    assert "(api)" in result
    assert "Synced from 2 workspace commit(s)" in result
    assert "1. feat(api): Endpoint A" in result
    assert "2. fix(api): Bug fix B" in result


# ── build_commit_template 自定义模板 ──────────────────

def test_custom_header_format():
    project = _make_project()
    commits = [CommitInfo(hash="x", type="docs", scope="", subject="Update readme")]
    result = build_commit_template(
        commits, project,
        template_name=None,  # 使用项目默认
    )
    # 项目默认 = "default" → 标准输出
    assert result.startswith("[TEST-")


def test_prefix_override_in_template(monkeypatch):
    """模板设置 prefix_override 时应覆盖项目 prefix"""
    tmp = Path(tempfile.gettempdir()) / "test_commit_config_ovr.json"
    monkeypatch.setattr(TemplateManager, "_default_path", lambda: tmp)
    TemplateManager.save([
        CommitTemplate(
            name="ovr",
            description="override test",
            header_format="[{prefix}-{number}] {type_str}: {subject}",
            body_format="{commit_list}",
            prefix_override="OVERRIDE",
        ),
    ])

    project = _make_project(prefix="ORIGINAL")
    commits = [CommitInfo(hash="x", type="feat", scope="", subject="Test")]
    result = build_commit_template(commits, project, template_name="ovr")
    assert "[OVERRIDE-" in result
    assert "[ORIGINAL-" not in result

    tmp.unlink()


# ── 格式变量正确注入 ──────────────────────────────────

def test_all_format_variables():
    project = _make_project(prefix="ANBM")
    commits = [
        CommitInfo(hash="h1", type="feat", scope="cli", subject="Add export"),
        CommitInfo(hash="h2", type="feat", scope="core", subject="Refactor sync"),
    ]
    result = build_commit_template(commits, project)

    assert "{prefix}" not in result
    assert "{number}" not in result
    assert "{type_str}" not in result
    assert "{scope_str}" not in result
    assert "{subject}" not in result
    assert "{project_name}" not in result
    assert "{commit_count}" not in result
    assert "{commit_list}" not in result


# ── 错误恢复 ──────────────────────────────────────────

def test_corrupt_json_returns_default(monkeypatch):
    tmp = Path(tempfile.gettempdir()) / "test_corrupt_commit_config.json"
    tmp.write_text("not valid json {{", encoding="utf-8")
    monkeypatch.setattr(TemplateManager, "_default_path", lambda: tmp)
    templates = TemplateManager.load()
    assert len(templates) == 1
    assert templates[0].name == "default"
    tmp.unlink()


def test_empty_templates_array_returns_default(monkeypatch):
    tmp = Path(tempfile.gettempdir()) / "test_empty_commit_config.json"
    tmp.write_text('{"templates": []}', encoding="utf-8")
    monkeypatch.setattr(TemplateManager, "_default_path", lambda: tmp)
    templates = TemplateManager.load()
    assert len(templates) == 1
    assert templates[0].name == "default"
    tmp.unlink()
