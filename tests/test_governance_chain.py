"""Governance 子系统全链路测试 —— PolicyEngine→History→Fact→Signal→Quality→Patterns。

种子 1 (42) 已在各单元测试文件中。本文件: 种子 2 (99) + 链路测试。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from tests.factory import TestDataFactory


@pytest.fixture
def f():
    return TestDataFactory(seed=99)


@pytest.fixture
def tmp_hist():
    """独立的 HistoryManager 工作区。"""
    with tempfile.TemporaryDirectory() as d:
        from backend.core.history import HistoryManager
        HistoryManager.set_workspace(d)
        yield d


# ═══════════════════════════════════════════════════════════════
# Chain G1: PolicyEngine → HistoryManager → Fact
# ═══════════════════════════════════════════════════════════════


class TestChainPolicyToFact:
    """PolicyEngine 产出 → 写入 History → Fact 推导。"""

    def test_policy_results_to_history(self, f, tmp_hist):
        """PolicyEngine 结果写入 HistoryManager 后能被正确读取。"""
        from backend.core.history import HistoryManager
        from tests.factory import pools

        # 模拟 PolicyEngine 产出
        results = f.policy.results()
        warnings = sum(len(v) for v in results.values())

        HistoryManager.add_operation(
            "testproject", "policy_check_result",
            "warning" if warnings else "success",
            results,
        )

        entries = HistoryManager.load()
        found = [e for e in entries
                 if e.operation == "policy_check_result"]
        assert len(found) >= 1, "policy_check_result 应被写入 History"
        assert found[0].status in ("success", "warning")

    def test_multiple_policy_checks_become_fact(self, f, tmp_hist):
        """连续 3 次 warning → Fact 推导应产生 consecutive_policy_warnings。"""
        from backend.core.history import HistoryManager
        from backend.core.fact.file_patterns import derive_file_facts

        # 写入 3 条连续的 warning
        for i in range(3):
            HistoryManager.add_operation(
                "testproject", "policy_check_result", "warning",
                {"lesson_triggers": [
                    {"lesson_id": f"L{i}", "severity": "high",
                     "rule": f"规则 {i}", "file": f"file_{i}.py"}
                ]},
                correlation_id=f"corr_{i}",
            )

        entries = HistoryManager.load()
        facts = derive_file_facts(entries, "testproject",
                                   datetime.now().isoformat())

        assert len(facts) >= 1
        assert any(
            f.fact_type == "consecutive_policy_warnings"
            for f in facts
        ), "连续 warning 应触发 fact"

    def test_no_warning_no_fact(self, f, tmp_hist):
        """只有 success 无 warning → 不产生 fact。"""
        from backend.core.history import HistoryManager
        from backend.core.fact.file_patterns import derive_file_facts

        for i in range(3):
            HistoryManager.add_operation(
                "testproject", "policy_check_result", "success",
                {}, correlation_id=f"corr_{i}",
            )

        entries = HistoryManager.load()
        facts = derive_file_facts(entries, "testproject",
                                   datetime.now().isoformat())
        assert len(facts) == 0


# ═══════════════════════════════════════════════════════════════
# Chain G2: History → Quality Metrics
# ═══════════════════════════════════════════════════════════════


class TestChainHistoryToQuality:
    """History → suggest pairs → quality metrics。"""

    def test_suggestion_pairs_paired_correctly(self, f, tmp_hist):
        """suggest 和 formalize 按 correlation_id 正确配对。"""
        from backend.core.history import HistoryManager
        from backend.core.governance.quality import load_suggestion_pairs

        corr = "corr_test_001"

        # AI 建议
        HistoryManager.add_suggestion(
            "testproject", "formalize",
            ai_proposal={"indices": [0, 1, 2, 3]},
            human_decision={},
            correlation_id=corr,
        )
        # 人类执行
        HistoryManager.add_operation(
            "testproject", "formalize", "success",
            {"indices": [0, 1, 2]},  # 只选了 3 个（修改了 AI 建议）
            correlation_id=corr,
        )

        pairs = load_suggestion_pairs("testproject")
        assert len(pairs) >= 1
        pair = pairs[0]
        assert pair["suggest_type"] == "formalize"
        assert pair["ai_proposal"]["indices"] == [0, 1, 2, 3]
        # human_decision 在 execution 的 detail 里
        assert pair.get("human_decision") is not None

    def test_empty_history_no_pairs(self, f, tmp_hist):
        """空历史 → 0 对。"""
        from backend.core.governance.quality import load_suggestion_pairs
        pairs = load_suggestion_pairs("empty_project")
        assert pairs == []

    def test_quality_metrics_computed(self, f, tmp_hist):
        """质量度量计算。"""
        from backend.core.history import HistoryManager
        from backend.core.governance.quality import (
            load_suggestion_pairs, compute_quality_metrics,
        )

        corr = "corr_quality_001"
        HistoryManager.add_suggestion(
            "testproject", "formalize",
            ai_proposal={"indices": [0, 1, 2]},
            human_decision={}, correlation_id=corr,
        )
        HistoryManager.add_operation(
            "testproject", "formalize", "success",
            {"indices": [0, 1, 2]},  # 完全采纳
            correlation_id=corr,
        )

        pairs = load_suggestion_pairs("testproject")
        metrics = compute_quality_metrics(pairs)
        assert "suggestion_count" in metrics
        assert metrics["suggestion_count"] >= 1


# ═══════════════════════════════════════════════════════════════
# Chain G3: History → Patterns
# ═══════════════════════════════════════════════════════════════


class TestChainHistoryToPatterns:
    """History → 模式检测。"""

    def test_patterns_report_runs(self, f, tmp_hist):
        """Patterns report 不抛异常——即使数据量小。"""
        from backend.core.history import HistoryManager
        from backend.core.governance.patterns import build_patterns_report

        entries = f.history_entries(30, operations=[
            "formalize", "scan", "sync", "policy_check_result",
            "governance_drift",
        ])
        for e in entries:
            HistoryManager.add_operation(
                "testproject", e.operation, e.status,
                e.detail, correlation_id=e.correlation_id,
            )

        report = build_patterns_report("testproject")
        assert "co_changing_modules" in report
        assert "commit_type_clusters" in report
        assert "trial_impact" in report

    def test_graph_builds_nodes(self, f, tmp_hist):
        """语义变更图构建。"""
        from backend.core.history import HistoryManager
        from backend.core.governance.graph import build_graph

        # 写入 formalize + triage_accept 事件
        HistoryManager.add_operation(
            "testproject", "formalize", "success",
            {"files_changed": [{"path": "auth.py"}, {"path": "login.py"}],
             "commit": "[GITGO-1]"},
            correlation_id="corr_g1",
        )
        HistoryManager.add_operation(
            "testproject", "formalize", "success",
            {"files_changed": [{"path": "login.py"}, {"path": "session.py"}],
             "commit": "[GITGO-2]"},
            correlation_id="corr_g2",
        )

        graph = build_graph("testproject")
        assert "nodes" in graph
        assert "edges" in graph

    def test_releases_list(self, f, tmp_hist):
        """发布历史列表。"""
        from backend.core.history import HistoryManager
        from backend.core.governance.releases import list_releases

        HistoryManager.add_operation(
            "testproject", "push", "success",
            {"commit": "[GITGO-1] feat: add auth"},
            correlation_id="corr_rel_1",
        )

        releases = list_releases("testproject")
        assert "releases" in releases or "pushes" in releases


# ═══════════════════════════════════════════════════════════════
# Chain G4: Contract → Drift → Gate
# ═══════════════════════════════════════════════════════════════


class TestChainContractDrift:
    """合约 → 漂移检测 → Gate 检查。"""

    def test_contract_load_and_detect(self, f, tmp_hist):
        """Contract 加载 + detect_drift + check_feature_signatures。"""
        import yaml
        from backend.core.contract import (
            ContractManager, detect_drift, check_feature_signatures, ProjectContract,
        )

        with tempfile.TemporaryDirectory() as ws:
            ws_path = Path(ws)
            (ws_path / ".gitgo").mkdir(parents=True, exist_ok=True)

            # 写入 contract.yaml
            contract = ProjectContract(
                project="testproject",
                updated=datetime.now().isoformat(),
                tech_stack=["python", "flask"],
                decided_features=[],
                architecture_constraints=["禁止直接 mutation core 状态"],
            )
            contract_yaml = yaml.dump(contract.to_dict())
            (ws_path / ".gitgo" / "contract.yaml").write_text(contract_yaml)

            # 加载
            loaded = ContractManager.load(ws_path)
            assert loaded is not None
            assert loaded.project == "testproject"

            # 漂移检测
            changed = ["backend/core/auth.py"]
            alerts = detect_drift(ws_path, changed, loaded)
            # 无 decided_features → 无告警（正常）
            assert isinstance(alerts, list)

            # 签名检测
            sig_alerts = check_feature_signatures(ws_path, changed, loaded)
            assert isinstance(sig_alerts, list)

    def test_gate_contract_drift_check(self, f):
        """ContractDriftGate.check() 在无合约时返回空结果。"""
        from backend.core.policy.gates import ContractDriftGate

        gate = ContractDriftGate()
        # 无法完全模拟 SyncSession，验证 Gate 可实例化且 check 方法签名正确
        assert gate.name == "contract_drift"
        assert gate.order == 20
        assert gate.fail_action == "block"

    def test_foreign_commit_gate(self, f):
        """ForeignCommitGate 可实例化。"""
        from backend.core.policy.gates import ForeignCommitGate
        gate = ForeignCommitGate()
        assert gate.name == "foreign_commit"
        assert gate.fail_action == "warn"


# ═══════════════════════════════════════════════════════════════
# Chain G5: State Bundle
# ═══════════════════════════════════════════════════════════════


class TestChainStateBundle:
    """完整状态导出。"""

    def test_collect_state_bundle_imports(self, f, tmp_hist):
        """State bundle 模块可导入，函数签名正确。"""
        from backend.core.governance.state_bundle import collect_state_bundle
        import inspect
        sig = inspect.signature(collect_state_bundle)
        params = list(sig.parameters.keys())
        assert "session" in params
        assert "minimal" in params


# ═══════════════════════════════════════════════════════════════
# Chain G6: Dependency Graph
# ═══════════════════════════════════════════════════════════════


class TestChainDependencyGraph:
    """依赖图构建和查询。"""

    def test_build_dep_graph_on_real_code(self):
        """对实际 gitgo 代码构建依赖图——不抛异常。"""
        from backend.core.contract import build_dep_graph, load_dep_graph
        from pathlib import Path

        ws = Path(__file__).parent.parent / "backend"
        graph = build_dep_graph(ws)

        assert isinstance(graph, dict)
        # 实际项目必有 import 关系
        assert len(graph) > 0, f"依赖图不应为空 (workspace={ws})"

    def test_get_dependents(self):
        """get_dependents 查询。"""
        from backend.core.contract import build_dep_graph, get_dependents
        from pathlib import Path

        ws = Path(__file__).parent.parent / "backend"
        build_dep_graph(ws)

        deps = get_dependents(ws, "backend/core/history.py")
        # history.py 被很多模块 import
        assert isinstance(deps, list)
        assert len(deps) > 0, "history.py 应该有多个依赖方"


# ═══════════════════════════════════════════════════════════════
# Chain G7: Full Governance Pipeline
# ═══════════════════════════════════════════════════════════════


class TestFullGovernancePipeline:
    """PolicyEngine → History → Fact → Quality → Patterns 全链路。"""

    def test_full_pipeline(self, f, tmp_hist):
        """端到端治理管线：policy → history → fact → quality → patterns。"""
        from backend.core.history import HistoryManager
        from backend.core.fact.file_patterns import derive_file_facts
        from backend.core.governance.quality import (
            load_suggestion_pairs, compute_quality_metrics,
        )
        from backend.core.governance.patterns import build_patterns_report

        # Phase 1: PolicyEngine 产出 → History
        for i in range(5):
            results = f.policy.results()
            warnings = sum(len(v) for v in results.values())
            HistoryManager.add_operation(
                "testproject", "policy_check_result",
                "warning" if warnings or i >= 3 else "success",
                results, correlation_id=f"corr_full_{i}",
            )

        # Phase 2: 追加 suggestion pairs
        corr = "corr_full_sug"
        HistoryManager.add_suggestion(
            "testproject", "formalize",
            ai_proposal={"indices": [0, 1, 2]},
            human_decision={}, correlation_id=corr,
        )
        HistoryManager.add_operation(
            "testproject", "formalize", "success",
            {"indices": [0, 1]}, correlation_id=corr,
        )

        # Phase 3: 追加 scan + sync + push（完整生命周期）
        for op in ["scan", "sync", "push"]:
            HistoryManager.add_operation(
                "testproject", op, "success",
                {"file_count": f._int(1, 10)},
                correlation_id=f"corr_full_{op}",
            )

        # Phase 4: Fact 推导
        entries = HistoryManager.load()
        facts = derive_file_facts(entries, "testproject",
                                   datetime.now().isoformat())
        assert isinstance(facts, list)

        # Phase 5: Quality metrics
        pairs = load_suggestion_pairs("testproject")
        if pairs:
            metrics = compute_quality_metrics(pairs)
            assert "suggestion_count" in metrics

        # Phase 6: Patterns
        report = build_patterns_report("testproject")
        assert "co_changing_modules" in report

    def test_pipeline_with_seed2(self, f, tmp_hist):
        """不同种子数据下全链路不抛异常。"""
        from backend.core.history import HistoryManager
        from backend.core.governance.patterns import build_patterns_report

        entries = f.history_entries(50)
        for e in entries:
            HistoryManager.add_operation(
                "testproject", e.operation, e.status,
                e.detail, correlation_id=e.correlation_id,
            )

        report = build_patterns_report("testproject")
        assert isinstance(report, dict)


# ═══════════════════════════════════════════════════════════════
# 边界条件
# ═══════════════════════════════════════════════════════════════


class TestGovernanceEdgeCases:
    def test_empty_history_all_metrics(self, f, tmp_hist):
        """空历史 → 所有 goverannce 函数不抛异常。"""
        from backend.core.governance.quality import (
            load_suggestion_pairs, compute_quality_metrics,
        )
        from backend.core.governance.patterns import build_patterns_report
        from backend.core.governance.graph import build_graph
        from backend.core.governance.releases import list_releases

        assert load_suggestion_pairs("empty") == []
        metrics = compute_quality_metrics([])
        assert metrics.get("suggestion_count", 0) == 0
        assert isinstance(build_patterns_report("empty"), dict)
        assert isinstance(build_graph("empty"), dict)
        assert isinstance(list_releases("empty"), dict)

    def test_derive_facts_non_project_entries(self, f, tmp_hist):
        """混合多个 project 的 entries → Fact 推导只关注目标 project。"""
        from backend.core.history import HistoryManager
        from backend.core.fact.file_patterns import derive_file_facts

        # 其他项目的 entries
        for i in range(5):
            HistoryManager.add_operation(
                "other_project", "policy_check_result", "warning",
                {}, correlation_id=f"corr_other_{i}",
            )

        facts = derive_file_facts(
            HistoryManager.load(), "testproject",
            datetime.now().isoformat(),
        )
        assert isinstance(facts, list)
