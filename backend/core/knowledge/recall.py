"""Recall tools —— Agent 的知识检索工具箱。

v0.35 Phase 2: L0 grep + 轻量排序 / L1 多向量语义搜索 / L2 RAG
设计与注射合并：tool_result 即注射。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from backend.core.knowledge.models import Lesson
from backend.core.knowledge.manager import LessonManager

if TYPE_CHECKING:
    pass

# ── 常量 ──────────────────────────────────────────────────

# L0 轻量排序的常用词黑名单
COMMON_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "or", "not", "but", "if", "then", "else", "this",
    "that", "it", "its", "文件", "需要", "修改", "可以", "应该",
    "的", "了", "是", "在", "不", "和", "也", "都", "要",
}

NOISE_WARNING_THRESHOLD = 0.3
DEFAULT_TOP_K = 10


# ── L0: grep + 轻量排序 ──────────────────────────────────

def severity_rank(severity: str) -> int:
    _map = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return _map.get(severity, 1)


def _sort_key(lesson: Lesson) -> tuple:
    return (
        -lesson.verified_count,
        -severity_rank(lesson.severity),
        -(lesson.project_name != ""),  # 有项目归属的靠前
        -(lesson.verified_at > ""),    # 已验证的靠前
    )


def _is_current_project(lesson: Lesson, current_project: str) -> int:
    if lesson.project_name == current_project:
        return 1
    if current_project in (lesson.verified_in or []):
        return 1
    return 0


def _compute_noise_signal(matches: list[Lesson]) -> str | None:
    """如果 top-1 和 top-2 分差太小 → 建议升级到 L1。"""
    if len(matches) < 2:
        return None
    s1 = _sort_key(matches[0])
    s2 = _sort_key(matches[1])
    # 比较前两个维度
    if s1[:2] == s2[:2]:
        return "L0 结果区分度低，建议使用 recall_semantic"
    return None


def filter_by_relevance(
    lessons: list[Lesson],
    task_description: str,
    threshold: float = 0.5,
) -> list[Lesson]:
    """检索时实时过滤——per-agent scope 的核心。

    优先用 embedding 相似度（如果 EMBEDDING_AVAILABLE）。
    Fallback: 子字符串匹配（去除常用词）。
    """
    # Fallback: 子字符串匹配
    keywords = set(task_description.lower().split()) - COMMON_WORDS
    if not keywords:
        return lessons  # 无法提取有效关键词，不筛选

    relevant = []
    for l in lessons:
        text = (l.rule + " " + l.trigger).lower()
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            relevant.append((l, score))
    relevant.sort(key=lambda x: -x[1])
    return [l for l, _ in relevant]


def record_retrieval(lesson: Lesson) -> None:
    """记录检索时间戳（热/温/冷分层数据源）。"""
    lesson.recent_retrievals.append(datetime.now().isoformat())
    if len(lesson.recent_retrievals) > 10:
        lesson.recent_retrievals = lesson.recent_retrievals[-10:]


def recall_grep(
    query: str,
    project: str,
    top_k: int = DEFAULT_TOP_K,
    agent_context: dict | None = None,
    workspace: str = "",
) -> dict:
    """L0: 硬规则子字符串匹配 + 轻量排序。

    Args:
        query: 搜索关键词
        project: 项目名
        top_k: 返回数量上限
        agent_context: {"task_description": "..."}，用于实时过滤
        workspace: 工作区路径

    Returns:
        {"lessons": [...], "total_matches": N, "text": str,
         "noise_signal": str|None}
    """
    ws = Path(workspace) if workspace else Path(".")
    lessons = LessonManager.load_instance(ws, project)
    lessons += LessonManager.load_pending(ws, project)

    # Per-agent scope: 实时过滤
    if agent_context and agent_context.get("task_description"):
        lessons = filter_by_relevance(
            lessons, agent_context["task_description"],
        )

    # 子字符串匹配
    q = query.lower()
    matches = [l for l in lessons
               if q in l.trigger.lower() or q in l.rule.lower()]

    # 轻量排序
    matches.sort(key=_sort_key)
    result = matches[:top_k]

    # 记录检索（热/温/冷）
    for l in result:
        record_retrieval(l)

    # 格式化输出
    lines = []
    for i, l in enumerate(result):
        lines.append(
            f"## Lesson {i+1} [{l.severity.upper()}] {l.rule[:80]}\n"
            f"  trigger: {l.trigger}\n"
            f"  verified: {l.verified_count}x"
            + (f" in {l.verified_in}" if l.verified_in else "") + "\n"
        )
    if len(matches) > top_k:
        lines.append(f"\n还有 {len(matches) - top_k} 条匹配。使用 top_k 参数增加返回数。")

    return {
        "lessons": [l.to_dict() for l in result],
        "total_matches": len(matches),
        "text": "\n".join(lines),
        "noise_signal": _compute_noise_signal(result),
    }


def recall_semantic(
    query: str,
    project: str,
    top_k: int = DEFAULT_TOP_K,
    agent_context: dict | None = None,
    workspace: str = "",
) -> dict:
    """L1: 多向量语义搜索（trigger + rule 双 embedding）。

    需要 embedding provider 已配置。未配置时 fallback 到 L0。
    """
    # Fallback: embedding 不可用 → L0
    try:
        from backend.core.knowledge.embedding import EmbeddingProvider
        provider = EmbeddingProvider()
        if not provider.available:
            raise RuntimeError("Embedding not configured")
    except (ImportError, RuntimeError):
        return recall_grep(query, project, top_k, agent_context, workspace)

    ws = Path(workspace) if workspace else Path(".")
    lessons = LessonManager.load_instance(ws, project)
    lessons += LessonManager.load_pending(ws, project)

    if agent_context and agent_context.get("task_description"):
        lessons = filter_by_relevance(
            lessons, agent_context["task_description"],
        )

    if not lessons:
        return {"lessons": [], "total_matches": 0, "text": "无匹配结果", "noise_signal": None}

    # 嵌入查询
    query_emb = provider.embed(query)

    # 对每条 lesson 计算 trigger 和 rule 的相似度
    scored = []
    for l in lessons:
        trigger_emb = provider.embed(l.trigger) if l.trigger else None
        rule_emb = provider.embed(l.rule) if l.rule else None

        trigger_sim = _cosine_similarity(query_emb, trigger_emb) if trigger_emb else 0
        rule_sim = _cosine_similarity(query_emb, rule_emb) if rule_emb else 0
        score = max(trigger_sim, rule_sim)
        scored.append((l, score))

    scored.sort(key=lambda x: -x[1])
    result = [l for l, _ in scored[:top_k]]

    for l in result:
        record_retrieval(l)

    lines = []
    for i, (l, score) in enumerate(scored[:top_k]):
        lines.append(
            f"## Lesson {i+1} [{l.severity.upper()}] (相似度: {score:.2f}) {l.rule[:80]}\n"
            f"  trigger: {l.trigger}\n"
            f"  verified: {l.verified_count}x\n"
        )

    return {
        "lessons": [l.to_dict() for l in result],
        "total_matches": len(scored),
        "text": "\n".join(lines),
        "noise_signal": None,
    }


def recall_rag(
    query: str,
    project: str,
    agent_context: dict | None = None,
    workspace: str = "",
) -> dict:
    """L2: RAG —— LLM 带着 L0/L1 检索结果综合思考。

    约束（硬编码，安全默认）：
    - 不能调工具（inner_tool_call_budget = 0）
    - RAG 内部禁止调 recall_rag（Dispatch 层强制）
    """
    # 先跑 L0 做粗筛
    l0_result = recall_grep(query, project, top_k=15,
                            agent_context=agent_context, workspace=workspace)

    # 构建 RAG prompt（纯文本综合，不调工具）
    prompt = (
        "根据以下检索到的项目经验教训，回答用户的查询。\n"
        "综合多条 lesson 的信息，给出一个连贯的答案。\n"
        "如果检索结果不足以回答，请说明。\n\n"
        f"用户查询: {query}\n\n"
        f"检索结果:\n{l0_result['text']}\n\n"
        "综合回答:"
    )

    # 注意：此处 LLM 调用由 agent_step 的 dispatcher 执行，
    # 不在本函数内直接调 LLM。返回 prompt 供上层使用。
    return {
        "lessons": l0_result["lessons"],
        "total_matches": l0_result["total_matches"],
        "text": l0_result["text"],
        "rag_prompt": prompt,
        "noise_signal": None,
    }


# ── 辅助 ──────────────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
