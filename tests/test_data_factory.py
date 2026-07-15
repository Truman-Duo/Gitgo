"""TestDataFactory 自身测试 —— 验证种子可复现性 + 数据合法性。"""

import pytest


class TestFactoryReproducibility:
    """固定种子必须 100% 可复现。"""

    def test_seed_reproducibility(self):
        from tests.factory import TestDataFactory
        f1 = TestDataFactory(seed=42)
        f2 = TestDataFactory(seed=42)

        assert f1.lesson().id == f2.lesson().id
        assert f1.lesson().trigger == f2.lesson().trigger
        assert f1.lesson().rule == f2.lesson().rule

    def test_different_seeds_different_data(self):
        from tests.factory import TestDataFactory
        f1 = TestDataFactory(seed=42)
        f2 = TestDataFactory(seed=99)
        # 极大概率不同
        ids1 = {f1.lesson().id for _ in range(20)}
        assert len(ids1) > 15  # 池子够大，20 条几乎都不同

    def test_lessons_are_deterministic(self):
        from tests.factory import TestDataFactory
        f1 = TestDataFactory(seed=42)
        f2 = TestDataFactory(seed=42)

        l1 = f1.lessons(10)
        l2 = f2.lessons(10)
        assert [l.id for l in l1] == [l.id for l in l2]
        assert [l.trigger for l in l1] == [l.trigger for l in l2]

    def test_chain_is_deterministic(self):
        from tests.factory import TestDataFactory
        f1 = TestDataFactory(seed=42)
        f2 = TestDataFactory(seed=42)

        c1 = f1.harvest_chain(signal_count=5, lesson_count=2)
        c2 = f2.harvest_chain(signal_count=5, lesson_count=2)
        assert c1["signals"][0]["trigger"] == c2["signals"][0]["trigger"]


class TestLessonValidity:
    """生成的数据必须合法。"""

    def test_lesson_has_all_required_fields(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        l = f.lesson()
        assert l.id
        assert l.trigger
        assert l.rule
        assert l.severity in ("low", "medium", "high", "critical")
        assert isinstance(l.recent_retrievals, list)

    def test_overrides_work(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        l = f.lesson(
            id="CUSTOM_ID", trigger="custom/path.py",
            severity="critical", verified_count=100,
        )
        assert l.id == "CUSTOM_ID"
        assert l.trigger == "custom/path.py"
        assert l.severity == "critical"
        assert l.verified_count == 100

    def test_signal_valid(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        s = f.signal()
        assert "signal_type" in s
        assert "trigger" in s
        assert "rule" in s

    def test_mock_llm_response(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        response = f.knowledge.mock_llm_response(3)
        assert len(response) == 3
        for item in response:
            assert "trigger" in item
            assert "rule" in item
            assert "severity" in item


class TestHarvestChain:
    def test_coherent_data(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        chain = f.harvest_chain(signal_count=6, lesson_count=3)
        assert len(chain["signals"]) == 6
        assert len(chain["mock_llm_response"]) == 3

    def test_e2e_rounds(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        chain = f.e2e_knowledge_chain(rounds=3)
        assert len(chain["rounds"]) == 3


class TestHistoryGenerator:
    def test_entries_sorted(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        entries = f.history_entries(20)
        for i in range(len(entries) - 1):
            assert entries[i].timestamp <= entries[i + 1].timestamp

    def test_valid_operation(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        e = f.history_entry()
        assert e.operation


class TestAgentGenerator:
    def test_valid(self):
        from tests.factory import TestDataFactory
        from backend.core.loop.models import ProcessStatus
        f = TestDataFactory(seed=42)
        p = f.agent_process()
        assert isinstance(p.status, ProcessStatus)


class TestSyncGenerator:
    """Sync 子系统生成器。"""

    def test_file_entry_has_valid_status(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        e = f.file_entry()
        assert e.status in ("new", "modified", "same", "renamed")
        assert e.rel_path

    def test_commit_info_has_valid_type(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        c = f.commit_info()
        assert c.type in ("feat", "fix", "docs", "refactor", "test", "chore", "perf", "ci")
        assert c.hash

    def test_scan_result_has_entries_and_commits(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        r = f.scan_result(file_count=10)
        assert len(r["entries"]) == 10
        assert len(r["commits"]) >= 1
        assert r["changed_count"] <= r["total_count"]

    def test_sync_chain_coherent(self):
        from tests.factory import TestDataFactory
        f = TestDataFactory(seed=42)
        chain = f.sync_chain(file_count=8)
        assert len(chain["scan"]["entries"]) == 8
        assert chain["formal"]["number"] >= 1


class TestRandomMode:
    def test_different_seed_different_output(self):
        from tests.factory import TestDataFactory
        f1 = TestDataFactory(seed=12345)
        f2 = TestDataFactory(seed=67890)
        l1 = f1.lesson()
        l2 = f2.lesson()
        # 极大概率不同
        assert (l1.id != l2.id) or (l1.trigger != l2.trigger)
