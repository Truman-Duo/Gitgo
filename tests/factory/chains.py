"""链路生成器 —— 一键生成跨子系统的完整测试场景。"""

from __future__ import annotations


class ChainGenerator:
    def __init__(self, factory):
        self.f = factory

    def harvest_chain(self, signal_count: int = 5,
                      lesson_count: int = 2) -> dict:
        """生成完整收割链路：信号 → LLM 总结 → lesson。

        Returns:
            {
                signals: list[dict],        # 未处理信号
                mock_llm_response: list,    # Mock LLM 返回的 lesson JSON
                expected_lessons: list,     # 期望产出的 lesson（门禁过滤后）
                llm_call_count: int,       # LLM 应被调用的次数
            }
        """
        signals = self.f.signals(signal_count)
        mock_response = self.f.knowledge.mock_llm_response(lesson_count)
        return {
            "signals": signals,
            "mock_llm_response": mock_response,
            "expected_lesson_count": lesson_count,
            "llm_call_count": 1,
        }

    def recall_chain(self, lesson_count: int = 10,
                     query_count: int = 3) -> dict:
        """生成完整检索→持久化→热冷分类链路。

        Returns:
            {
                lessons: list[Lesson],
                queries: list[str],         # 每次检索的查询词
                expected_hot: list[str],   # 预期变 hot 的 lesson ID
                expected_sticky: list[str], # 预期在 sticky list 中的 ID
            }
        """
        lessons = self.f.lessons(lesson_count)
        queries = self.f._pick_n(pools.SEARCH_QUERIES, query_count)

        # 选 2 条 lesson 作为"会被反复检索的"
        expected_hot = []
        for i in range(min(2, len(lessons))):
            # 确保 query 匹配 lesson trigger
            l = lessons[i]
            queries.append(l.trigger.split("/")[-1].split(".")[0])
            expected_hot.append(l.id)

        return {
            "lessons": lessons,
            "queries": queries,
            "expected_hot": expected_hot,
        }

    def e2e_knowledge_chain(self, rounds: int = 3) -> dict:
        """生成完整端到端知识链路：多轮信号→收割→检索→回收。

        Returns:
            {
                rounds: [{
                    signals: [...],          # 本轮信号
                    mock_llm: [...],         # 本轮 LLM 响应
                    recall_queries: [...],   # 本轮检索词
                }, ...],
                all_lessons: list[Lesson],   # 最终全部 lesson
                expected_hot_count: int,
            }
        """
        round_data = []
        all_lessons = []
        for _ in range(rounds):
            chain = self.harvest_chain(
                signal_count=self.f._int(3, 8),
                lesson_count=self.f._int(1, 3),
            )
            round_data.append({
                "signals": chain["signals"],
                "mock_llm": chain["mock_llm_response"],
                "recall_queries": self.f._pick_n(
                    pools.SEARCH_QUERIES, self.f._int(1, 3),
                ),
            })

        all_lessons = self.f.lessons(8)

        return {
            "rounds": round_data,
            "all_lessons": all_lessons,
            "expected_hot_count": 1,
        }

    def pending_digest_chain(self, total: int = 10) -> dict:
        """生成 pending 消化管线数据。

        Returns:
            {
                good_lessons: list[Lesson],    # 应该被保留的
                short_rule_lessons: [...],     # rule 过短，应被 discard
                nonexistent_trigger: [...],    # trigger 不存在，应被 discard
                expected_remaining: int,       # 消化后剩余数
            }
        """
        good = self.f.lessons(5)
        short = [
            self.f.lesson(id="short_1", rule="too short", trigger="auth.py"),
            self.f.lesson(id="short_2", rule="x", trigger="login.py"),
        ]
        nonexistent = [
            self.f.lesson(
                id="nonex_1",
                trigger="nonexistent/deleted_file.py",
                rule="if modifying deleted file, then must reconsider approach",
            ),
        ]
        return {
            "good": good,
            "short_rule": short,
            "nonexistent_trigger": nonexistent,
            "expected_remaining": len(good),
            "expected_discarded": len(short) + len(nonexistent),
        }


    def sync_chain(self, file_count: int = 10) -> dict:
        """生成完整 scan → formalize → sync 链路数据。

        Returns:
            {
                scan: {entries, commits, changed_count},
                formal: {message, number, prefix},
                expected_synced_count: int,
            }
        """
        scan = self.f.sync.scan_result(file_count)
        formal = self.f.sync.formal_commit()
        changed = [e for e in scan["entries"] if e.status != "same"]
        return {
            "scan": scan,
            "formal": formal,
            "changed_files": changed,
            "expected_changed_count": len(changed),
        }


# Late import for pools (used by chains)
from tests.factory import pools
