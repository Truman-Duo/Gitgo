"""AgentSession — B-level Agent 的独立会话。

每个 B Agent 拥有独立的 message history，不与其他 Agent 共享。
A Agent 通过 ContextBuilder 注入治理简报作为 system prompt。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class AgentSession:
    """B-level Agent 独立会话。"""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[dict] = field(default_factory=list)

    def append(self, role: str, content: str,
               referenced_files: list[str] | None = None,
               message_type: str = "") -> None:
        """追加消息。v0.36: 支持结构化元数据。

        referenced_files: 消息涉及的文件路径列表（产生时打，不消费时反解）。
        message_type: "conversation" | "tool_result" | "governance_nudge" | "compact_transcript"
        """
        msg = {"role": role, "content": content}
        if referenced_files:
            msg["referenced_files"] = referenced_files
        if message_type:
            msg["message_type"] = message_type
        self.messages.append(msg)

    def append_system(self, content: str, **kwargs) -> None:
        self.messages.append({"role": "system", "content": content, **kwargs})

    def append_user(self, content: str,
                    referenced_files: list[str] | None = None,
                    message_type: str = "") -> None:
        msg = {"role": "user", "content": content}
        if referenced_files:
            msg["referenced_files"] = referenced_files
        if message_type:
            msg["message_type"] = message_type
        self.messages.append(msg)

    def append_assistant(self, content: str, **kwargs) -> None:
        self.messages.append({"role": "assistant", "content": content, **kwargs})

    def estimate_tokens(self) -> int:
        """字符数 / 4 估算 token 数（保守估计，覆盖中英文混合）。"""
        total = sum(len(m.get("content", "")) for m in self.messages)
        return max(1, total // 4)

    def last_assistant_at(self) -> float:
        """最近一条 assistant 消息的插入时间（Unix timestamp），用于 cache TTL 判断。"""
        import time
        return time.time()

    def inject_governance_brief(self, brief: dict) -> None:
        """注入治理上下文作为 system prompt（在 session 创建时调用一次）。

        兼容新旧两种格式:
        - 新格式: {"signals": [GovernanceSignal, ...], "brief": "..."}
        - 旧格式: {"phase_brief": "...", "contract_summary": "...", ...}
        """
        parts = [f"你是项目治理执行 Agent (ring 3)。"]

        # 新格式：直接使用 brief 文本
        brief_text = brief.get("brief")
        if brief_text is not None:
            parts.append(brief_text)
        else:
            # 旧格式兼容
            if brief.get("phase_brief"):
                parts.append(f"## 近期工具调用\n{brief['phase_brief']}")
            if brief.get("contract_summary"):
                parts.append(f"## 合约摘要\n{brief['contract_summary']}")
            if brief.get("lesson_matches"):
                parts.append(f"## 匹配的治理规则\n{brief['lesson_matches']}")
            if brief.get("rejection_history"):
                parts.append(f"## 近期被拒记录（请避免重复以下错误）\n{brief['rejection_history']}")

        self.append_system("\n\n".join(parts))

    def to_openai_messages(self) -> list[dict]:
        """转换为 OpenAI API 兼容的 messages 格式。"""
        return [{"role": m["role"], "content": m["content"]} for m in self.messages]
