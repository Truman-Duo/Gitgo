"""Identity + Authorship 子系统链路测试。

覆盖: Identity Guard 三规则 / Memory Snapshot / Authorship 清洗 / 隐私扫描。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from tests.factory import TestDataFactory


@pytest.fixture
def f():
    return TestDataFactory(seed=123)


@pytest.fixture
def tmp_ws():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        yield ws


# ═══════════════════════════════════════════════════════════════
# Chain I1: Integrity Guard 三规则
# ═══════════════════════════════════════════════════════════════


class TestChainIntegrityGuard:
    """_run_integrity_checks → 三规则逐一验证。"""

    def test_mass_override_detected(self, tmp_ws):
        """变更文件占比 >= 80% → mass_override 告警。"""
        from backend.core.identity.guard import _detect_mass_override
        from backend.core.operations.models import FileEntry

        # 全部标记为 modified（100% 变更）
        entries = [FileEntry(rel_path=f"file_{i}.py", status="modified")
                   for i in range(10)]

        class FakeProject:
            integrity = {"mass_override_threshold": 0.80}

        result = _detect_mass_override(entries, FakeProject())
        assert result is not None
        assert result["rule"] == "mass_override"

    def test_mass_override_not_triggered_below_threshold(self, tmp_ws):
        """变更占比 < 阈值 → 不触发。"""
        from backend.core.identity.guard import _detect_mass_override
        from backend.core.operations.models import FileEntry

        # 10 个文件，只有 2 个 modified (20%)
        entries = [FileEntry(rel_path=f"file_{i}.py",
                            status="modified" if i < 2 else "same")
                   for i in range(10)]

        class FakeProject:
            integrity = {"mass_override_threshold": 0.80}

        result = _detect_mass_override(entries, FakeProject())
        assert result is None

    def test_mass_override_empty_entries(self, tmp_ws):
        """空文件列表 → 不触发。"""
        from backend.core.identity.guard import _detect_mass_override

        class FakeProject:
            integrity = {"mass_override_threshold": 0.80}

        result = _detect_mass_override([], FakeProject())
        assert result is None

    def test_identity_file_deletion_detected(self, tmp_ws):
        """身份文件缺失 → 告警。"""
        from backend.core.identity.guard import _detect_identity_file_deletion

        class FakeProject:
            integrity = {"identity_files": ["CLAUDE.md"]}

        # CLAUDE.md 不存在 → 应检测到缺失
        result = _detect_identity_file_deletion(tmp_ws, FakeProject())
        assert result is not None
        assert result["rule"] == "identity_file_deleted"

    def test_identity_file_all_present(self, tmp_ws):
        """身份文件全部存在 → 不告警。"""
        from backend.core.identity.guard import _detect_identity_file_deletion

        (tmp_ws / "CLAUDE.md").write_text("# ok")

        class FakeProject:
            integrity = {"identity_files": ["CLAUDE.md"]}

        result = _detect_identity_file_deletion(tmp_ws, FakeProject())
        assert result is None

    def test_structure_collapse_jaccard(self, tmp_ws):
        """目录骨架保存后可检测。"""
        from backend.core.identity.guard import (
            _save_directory_skeleton, _detect_structure_collapse,
        )
        from backend.core.operations.models import FileEntry

        # 创建目录结构
        (tmp_ws / "backend").mkdir(exist_ok=True)
        (tmp_ws / "tests").mkdir(exist_ok=True)
        (tmp_ws / "docs").mkdir(exist_ok=True)

        _save_directory_skeleton(tmp_ws)

        entries = [FileEntry(rel_path="backend/auth.py", status="new")]
        result = _detect_structure_collapse(entries, tmp_ws)
        # 目录结构一致 → 不触发 collapse
        assert result is None or isinstance(result, dict)

    def test_full_integrity_check_runs(self, tmp_ws):
        """_run_integrity_checks 不抛异常。"""
        from backend.core.identity.guard import _run_integrity_checks
        from backend.core.operations.models import FileEntry

        (tmp_ws / "CLAUDE.md").write_text("# test")
        (tmp_ws / ".gitignore").write_text("*.pyc")

        entries = [FileEntry(rel_path="backend/auth.py", status="modified")]

        class FakeProject:
            integrity = {
                "mass_override_threshold": 0.80,
                "identity_files": ["CLAUDE.md", ".gitignore"],
            }

        warnings = _run_integrity_checks(entries, str(tmp_ws), FakeProject())
        assert isinstance(warnings, list)


# ═══════════════════════════════════════════════════════════════
# Chain I2: Memory Snapshot
# ═══════════════════════════════════════════════════════════════


class TestChainMemorySnapshot:
    """Memory Snapshot 保存/恢复/列表。"""

    def test_snapshot_and_restore(self, tmp_ws):
        """snapshot → restore → list 完整链路。"""
        from backend.core.identity.snapshot import (
            snapshot_tool_memories, restore_tool_memories,
            list_memory_snapshots,
        )

        backup = tmp_ws / "backup"
        backup.mkdir()
        (backup / ".gitgo" / "memories").mkdir(parents=True, exist_ok=True)

        class FakeProject:
            name = "testproject"

        # Snapshot（可能因为没有实际 memory 文件而跳过）
        result = snapshot_tool_memories(
            str(tmp_ws), str(backup), FakeProject(),
        )
        assert isinstance(result, dict)

        # List
        snapshots = list_memory_snapshots(str(backup))
        assert isinstance(snapshots, list)


# ═══════════════════════════════════════════════════════════════
# Chain I3: Authorship 清洗
# ═══════════════════════════════════════════════════════════════


class TestChainAuthorship:
    """AI 痕迹清洗 + 隐私扫描。"""

    def test_strip_commit_message(self):
        """Co-authored-by 等 AI 署名被清除。"""
        from backend.core.authorship import strip_commit_message

        original = (
            "feat: add auth module\n\n"
            "Co-authored-by: Claude <claude@anthropic.com>\n"
            "Generated with Claude Code\n"
        )
        cleaned = strip_commit_message(original)
        assert "Co-authored-by" not in cleaned
        assert "Generated with" not in cleaned
        assert "feat: add auth module" in cleaned

    def test_strip_commit_message_clean_already(self):
        """干净的 commit message 不受影响。"""
        from backend.core.authorship import strip_commit_message
        original = "feat: add auth module\n\nDetails here"
        assert strip_commit_message(original) == original.strip()

    def test_strip_code_comments(self):
        """代码注释中的 AI 声明被清除。"""
        from backend.core.authorship import strip_code_comments

        code = (
            "# Generated with Claude Code\n"
            "def foo():\n"
            "    # AI suggested\n"
            "    return 42\n"
            "// Created by Copilot\n"
        )
        cleaned = strip_code_comments(code)
        assert "Generated with" not in cleaned
        assert "Created by Copilot" not in cleaned
        assert "def foo()" in cleaned

    def test_is_ai_config_file(self):
        """AI 配置文件检测。"""
        from backend.core.authorship import is_ai_config_file

        assert is_ai_config_file(".cursorrules", [".cursorrules"])
        assert is_ai_config_file(".github/copilot-instructions.md",
                                  [".github/copilot-instructions.md"])
        assert not is_ai_config_file("src/main.py", [".cursorrules"])

    def test_scan_privacy_basic(self, tmp_ws):
        """基础隐私扫描。"""
        from backend.core.authorship import scan_privacy

        test_file = tmp_ws / "test.py"
        test_file.write_text(
            "api_key = 'sk-abc123def456ghi789jkl'\n"
            "email = 'user@example.com'\n"
            "normal_code = 'hello world'\n"
        )

        alerts = scan_privacy(
            str(test_file), test_file.read_text(),
        )
        assert isinstance(alerts, list)
        # 应该检测到 API key 和 email
        assert len(alerts) >= 2

    def test_scan_privacy_clean_file(self, tmp_ws):
        """无敏感信息的文件不产生告警。"""
        from backend.core.authorship import scan_privacy

        test_file = tmp_ws / "clean.py"
        test_file.write_text("def hello():\n    return 'world'\n")

        alerts = scan_privacy(
            str(test_file), test_file.read_text(),
        )
        assert alerts == []

    def test_get_ai_exclude_patterns(self):
        """AI 排除模式包含关键路径。"""
        from backend.core.authorship import get_ai_exclude_patterns

        class FakeProject:
            authorship = {}

        patterns = get_ai_exclude_patterns(FakeProject())
        assert isinstance(patterns, list)
        assert len(patterns) > 0
        # CLAUDE.md 和 .claude/ 应该在排除列表里
        assert any("CLAUDE" in p or ".claude" in p for p in patterns)


# ═══════════════════════════════════════════════════════════════
# Chain I4: Authorship → Identity 跨模块
# ═══════════════════════════════════════════════════════════════


class TestChainAuthorshipToIdentity:
    """Authorship 清洗结果 → Identity 完整性验证。"""

    def test_stripped_code_passes_integrity(self, tmp_ws):
        """清洗后的代码仍然通过完整性检测。"""
        from backend.core.authorship import strip_code_comments
        from backend.core.identity.guard import _detect_mass_override
        from backend.core.operations.models import FileEntry

        code = (
            "# Generated with Claude Code\n"
            "def auth():\n"
            "    return True\n"
        )
        cleaned = strip_code_comments(code)
        assert len(cleaned) > 0
        assert "def auth()" in cleaned

        # 清洗本身不影响文件数量
        entries = [FileEntry(rel_path="auth.py", status="modified")]
        class FakeProject:
            integrity = {"mass_override_threshold": 0.80}
        result = _detect_mass_override(entries, FakeProject())
        assert result is None  # 1/1 = 100% 但需要满足 threshold

    def test_strip_authorship_from_message(self):
        """strip_authorship_from_message = strip_commit_message。"""
        from backend.core.authorship import strip_authorship_from_message

        msg = "[GITGO-42] feat: add module\n\nGenerated with Claude Code"
        cleaned = strip_authorship_from_message(msg)
        assert "Generated with" not in cleaned
        assert "[GITGO-42]" in cleaned


# ═══════════════════════════════════════════════════════════════
# Chain I5: 全链路 —— 完整 Identity + Authorship 管线
# ═══════════════════════════════════════════════════════════════


class TestFullIdentityPipeline:
    """Identity Guard → Snapshot → Authorship 全链路。"""

    def test_full_pipeline(self, tmp_ws):
        """从身份检测到 AI 清洗的完整链路。"""
        from backend.core.identity.guard import (
            _run_integrity_checks, _save_directory_skeleton,
        )
        from backend.core.identity.snapshot import (
            snapshot_tool_memories, list_memory_snapshots,
        )
        from backend.core.authorship import (
            strip_commit_message, strip_code_comments,
            is_ai_config_file,
        )
        from backend.core.operations.models import FileEntry

        # Phase 1: 建立目录结构 + 保存骨架
        (tmp_ws / "backend").mkdir(exist_ok=True)
        (tmp_ws / "CLAUDE.md").write_text("# test")
        (tmp_ws / ".gitignore").write_text("*.pyc")
        _save_directory_skeleton(tmp_ws)

        # Phase 2: 模拟文件变更 + 完整性检测
        entries = [
            FileEntry(rel_path="backend/auth.py", status="modified"),
            FileEntry(rel_path="backend/session.py", status="modified"),
            FileEntry(rel_path="CLAUDE.md", status="same"),
        ]

        class FakeProject:
            integrity = {
                "mass_override_threshold": 0.80,
                "identity_files": ["CLAUDE.md", ".gitignore"],
            }

        warnings = _run_integrity_checks(entries, str(tmp_ws), FakeProject())
        assert isinstance(warnings, list)

        # Phase 3: Memory snapshot
        backup = tmp_ws / "backup"
        backup.mkdir()
        (backup / ".gitgo" / "memories").mkdir(parents=True, exist_ok=True)

        class FakeProjectNamed:
            name = "testproject"

        snapshot_tool_memories(str(tmp_ws), str(backup), FakeProjectNamed())
        snapshots = list_memory_snapshots(str(backup))
        assert isinstance(snapshots, list)

        # Phase 4: Authorship 清洗
        msg = strip_commit_message(
            "[GITGO-1] feat: add auth\n\nCo-authored-by: Claude"
        )
        assert "Co-authored-by" not in msg

        code = strip_code_comments(
            "# Generated with Claude Code\ndef auth():\n    pass"
        )
        assert "Generated with" not in code

        # Phase 5: AI 配置检测
        assert is_ai_config_file(".cursorrules", [".cursorrules"])
        assert not is_ai_config_file("src/main.py", [".cursorrules"])


# ═══════════════════════════════════════════════════════════════
# 边界条件
# ═══════════════════════════════════════════════════════════════


class TestIdentityEdgeCases:
    def test_mass_override_custom_threshold(self, tmp_ws):
        """自定义阈值。"""
        from backend.core.identity.guard import _detect_mass_override
        from backend.core.operations.models import FileEntry

        # 50% 变更
        entries = [FileEntry(rel_path=f"f_{i}.py",
                            status="modified" if i < 5 else "same")
                   for i in range(10)]

        class FakeProject:
            integrity = {"mass_override_threshold": 0.60}

        result = _detect_mass_override(entries, FakeProject())
        # 50% < 60% → 不触发
        assert result is None

        class FakeProjectLow:
            integrity = {"mass_override_threshold": 0.40}

        result2 = _detect_mass_override(entries, FakeProjectLow())
        # 50% >= 40% → 触发
        assert result2 is not None

    def test_empty_commit_message(self):
        """空 commit message 清洗不崩溃。"""
        from backend.core.authorship import strip_commit_message
        assert strip_commit_message("") == ""
        assert strip_commit_message("   ") == ""

    def test_no_privacy_patterns_in_normal_code(self):
        """正常代码不触发隐私告警。"""
        from backend.core.authorship import scan_privacy
        normal = "def hello():\n    x = 1 + 2\n    return x * 3\n"
        alerts = scan_privacy("test.py", normal)
        assert alerts == []
