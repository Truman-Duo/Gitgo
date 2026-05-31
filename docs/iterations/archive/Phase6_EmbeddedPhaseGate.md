# Embedded Phase Gate — Agent Harness 内的认知阶段控制器

> 设计日期：2026-05-16 | 基于 v0.21 源码 | 融合 Design A/B/C 的嵌入架构模式

---

## 定位

Design A/B/C 将 Gitgo 放在 agent 外部——CLI subprocess、daemon socket、bundle API。
它们的共同假设是 **Gitgo 是 agent 调用的一个服务**。

Embedded Phase Gate 将 Gitgo 的 phase 模型放在 agent harness **内部**——
与 permission checker、context shaper、tool router 同级，在 tool dispatch 之前执行。
它不是 agent 调用的工具，是 **agent loop 的结构本身**。

融合策略：

| 来源 | 保留什么 | 丢弃什么 |
|------|---------|---------|
| Design A (CLI-Loop) | 同步的、确定性的 phase 检查——每次 tool call 前执行，不可绕过 | subprocess 调用开销——改为 import 模块内联调用 |
| Design B (Event-Stream) | 每条 tool call 经过 phase gate 的结果写入审计日志（event log） | pub-sub 订阅机制和 daemon server——不需要异步解耦 |
| Design C (Bundle-Polling) | phase context 作为结构化 JSON 注入 LLM prompt | `updateBundle()`/`getBundle()` 的 pull 模式——改为 push：每次 phase 变更时自动更新 context |

---

## 核心组件：PhaseGate

```python
# phase_gate.py — agent harness 的内部组件，约 150 行

from enum import Enum
from dataclasses import dataclass, field
from typing import Callable

class CognitivePhase(Enum):
    """Agent 的认知阶段——从 Gitgo governance state 映射。"""
    WORKSPACE = "workspace"         # 自由探索，无约束
    TRIAL = "trial"                 # 外部输入待审查
    CURATED = "curated"             # trial 已决策，等待整合
    FORMALIZED = "formalized"       # 语义单元已建立，不可回退
    RELEASE_READY = "release_ready" # 已同步，等待发布
    PUBLISHED = "published"         # 已发布，终态

# 每个 phase 允许的 tool 操作
PHASE_PERMISSIONS = {
    CognitivePhase.WORKSPACE: {
        "edit": "allow", "shell": "allow", "scan": "allow",
        "commit": "allow", "formalize": "allow",
        "push": "block", "publish": "block",
    },
    CognitivePhase.TRIAL: {
        "edit": "block", "shell": "block",
        "scan": "allow", "triage": "allow",
        "push": "block", "publish": "block",
    },
    CognitivePhase.FORMALIZED: {
        "edit": "block", "scan": "allow", "sync": "allow",
        "push": "block", "publish": "block",
    },
    CognitivePhase.RELEASE_READY: {
        "edit": "block", "push": "allow", "publish": "allow",
    },
    CognitivePhase.PUBLISHED: {
        "edit": "block", "push": "block",
        "scan": "allow",  # 只读
    },
}

@dataclass
class PhaseGateResult:
    allowed: bool
    current_phase: CognitivePhase
    error_code: str | None = None
    suggested_action: str | None = None
    phase_context: dict = field(default_factory=dict)  # 注入 LLM context 的状态摘要

@dataclass
class AuditEvent:
    """每次 phase gate 检查的审计记录。吸收 Design B 的事件日志思想。"""
    timestamp: str
    tool_name: str
    current_phase: str
    allowed: bool
    error_code: str | None


class PhaseGate:
    """嵌入 agent harness 的认知阶段控制器。
    
    三个职责：
    1. Phase Enforcement (来自 Design A) — 同步检查，不允许则 BLOCK
    2. Audit Trail (来自 Design B) — 每次检查写事件日志
    3. Context Injection (来自 Design C) — 将 phase context 注入 LLM prompt
    """
    
    def __init__(self):
        self._phase = CognitivePhase.WORKSPACE
        self._audit_log: list[AuditEvent] = []
        self._on_phase_change: Callable[[CognitivePhase, CognitivePhase], None] | None = None
    
    # ── Design A 能力：同步 Phase Enforcement ──────────────────
    
    @property
    def phase(self) -> CognitivePhase:
        return self._phase
    
    def check(self, tool_name: str) -> PhaseGateResult:
        """在每次 tool call 之前调用。返回是否允许执行。
        
        这是 harness 中执行的第一个检查——在 permission checker 之前。
        如果 phase 不允许，tool dispatch 不会发生。
        """
        permissions = PHASE_PERMISSIONS.get(self._phase, {})
        allowed = permissions.get(tool_name, "allow") == "allow"
        
        error_code = None
        suggested_action = None
        if not allowed:
            error_code = self._error_code_for(tool_name)
            suggested_action = self._suggested_action_for(error_code)
        
        # Design B: 审计日志
        from datetime import datetime
        self._audit_log.append(AuditEvent(
            timestamp=datetime.now().isoformat(),
            tool_name=tool_name,
            current_phase=self._phase.value,
            allowed=allowed,
            error_code=error_code,
        ))
        
        # Design C: phase context（通过还是阻塞都提供，供 LLM context 使用）
        phase_context = {
            "current_phase": self._phase.value,
            "allowed_actions": [k for k, v in permissions.items() if v == "allow"],
            "blocked_actions": [k for k, v in permissions.items() if v == "block"],
        }
        
        return PhaseGateResult(
            allowed=allowed,
            current_phase=self._phase,
            error_code=error_code,
            suggested_action=suggested_action,
            phase_context=phase_context,
        )
    
    def transition(self, new_phase: CognitivePhase) -> bool:
        """尝试切换 phase。检查是否合法转移（governance state machine）。
        
        返回 True 表示切换成功，False 表示非法转移。
        """
        old = self._phase
        if not self._is_valid_transition(old, new_phase):
            return False
        self._phase = new_phase
        if self._on_phase_change:
            self._on_phase_change(old, new_phase)
        return True
    
    # ── Design B 能力：审计日志 ──────────────────────────────────
    
    @property
    def audit_log(self) -> list[AuditEvent]:
        return list(self._audit_log)
    
    def audit_summary(self) -> dict:
        """供 governance analysis 使用的审计摘要。"""
        if not self._audit_log:
            return {"total_checks": 0}
        blocked = [e for e in self._audit_log if not e.allowed]
        return {
            "total_checks": len(self._audit_log),
            "blocked_count": len(blocked),
            "blocked_by_tool": {e.tool_name for e in blocked},
            "phase_transitions": [
                {"from": self._audit_log[i-1].current_phase,
                 "to": self._audit_log[i].current_phase}
                for i in range(1, len(self._audit_log))
                if self._audit_log[i-1].current_phase != self._audit_log[i].current_phase
            ],
        }
    
    # ── Design C 能力：Context Injection ────────────────────────
    
    def context_for_llm(self) -> str:
        """生成注入 LLM prompt 的 phase context 文本。
        
        在每次 LLM 调用前，harness 将此文本 append 到 system prompt。
        这使 agent 在推理时就知道自己在哪个 phase、能做什么操作。
        """
        permissions = PHASE_PERMISSIONS.get(self._phase, {})
        allowed = [k for k, v in permissions.items() if v == "allow"]
        blocked = [k for k, v in permissions.items() if v == "block"]
        
        return f"""## Current Cognitive Phase: {self._phase.value.upper()}
        
        Allowed operations: {', '.join(allowed) or 'none'}
        Blocked operations: {', '.join(blocked) or 'none'}
        
        Phase rules:
        - WORKSPACE: free exploration, no constraints. Use scan/formalize to progress.
        - FORMALIZED: changes are locked. Use sync to progress to release_ready.
        - RELEASE_READY: ready for publish. Use push to publish (irreversible).
        - PUBLISHED: final state. No further edits allowed.
        """
    
    # ── 内部 ──────────────────────────────────────────────────
    
    def _error_code_for(self, tool_name: str) -> str:
        mapping = {
            "push": {
                CognitivePhase.WORKSPACE: "NO_FORMALIZED_BOUNDARY",
                CognitivePhase.TRIAL: "TRIAL_CANNOT_PUBLISH",
                CognitivePhase.FORMALIZED: "MUST_SYNC_BEFORE_PUBLISH",
                CognitivePhase.PUBLISHED: "ALREADY_PUBLISHED",
            },
            "publish": {
                CognitivePhase.WORKSPACE: "NO_FORMALIZED_BOUNDARY",
                CognitivePhase.TRIAL: "TRIAL_CANNOT_PUBLISH",
                CognitivePhase.FORMALIZED: "MUST_SYNC_BEFORE_PUBLISH",
            },
        }
        return mapping.get(tool_name, {}).get(self._phase, "PHASE_CONSTRAINT")
    
    def _suggested_action_for(self, error_code: str) -> str | None:
        mapping = {
            "NO_FORMALIZED_BOUNDARY": "formalize",
            "TRIAL_CANNOT_PUBLISH": "triage_then_formalize",
            "MUST_SYNC_BEFORE_PUBLISH": "sync",
            "ALREADY_PUBLISHED": None,
        }
        return mapping.get(error_code)
    
    @staticmethod
    def _is_valid_transition(old: CognitivePhase, new: CognitivePhase) -> bool:
        """Governance state machine 的合法转移矩阵。"""
        valid = {
            (CognitivePhase.WORKSPACE, CognitivePhase.FORMALIZED),
            (CognitivePhase.WORKSPACE, CognitivePhase.TRIAL),
            (CognitivePhase.TRIAL, CognitivePhase.CURATED),
            (CognitivePhase.CURATED, CognitivePhase.FORMALIZED),
            (CognitivePhase.FORMALIZED, CognitivePhase.RELEASE_READY),
            (CognitivePhase.RELEASE_READY, CognitivePhase.PUBLISHED),
        }
        return (old, new) in valid
```

---

## Harness 集成方式

PhaseGate 在 agent harness 中的位置：

```python
# agent_harness.py — 集成 PhaseGate 的 harness 示例

from phase_gate import PhaseGate, CognitivePhase

class AgentHarness:
    def __init__(self):
        self.phase_gate = PhaseGate()
        self.context = []
        self.history = []
    
    def run(self, task: str):
        """Cognitive Loop with Deterministic Phase Gates."""
        self.context.append({"role": "system", "content": task})
        
        while not self._task_complete():
            # Design C: 将 phase context 注入 LLM prompt
            phase_context = self.phase_gate.context_for_llm()
            self.context[-1]["content"] += "\n\n" + phase_context
            
            # LLM 推理
            response = self.llm(self.context)
            self.history.append(response)
            
            for action in self._parse_actions(response):
                tool_name = action["tool"]
                
                # Phase Gate: 在执行任何 tool 之前检查
                result = self.phase_gate.check(tool_name)
                
                if not result.allowed:
                    # Block: 将拒绝原因作为 observation 反馈给 LLM
                    self.context.append({
                        "role": "user",
                        "content": f"Action '{tool_name}' blocked in phase "
                                   f"'{result.current_phase.value}'. "
                                   f"Error: {result.error_code}. "
                                   f"Suggested: {result.suggested_action}."
                    })
                    break  # 回到 LLM 推理，不是继续执行
                
                # Allow: 执行 tool
                output = self._dispatch(action)
                self.context.append({"role": "user", "content": output})
                
                # 如果 action 触发了 phase 变更，同步更新
                if tool_name == "formalize":
                    self.phase_gate.transition(CognitivePhase.FORMALIZED)
                elif tool_name == "sync":
                    self.phase_gate.transition(CognitivePhase.RELEASE_READY)
                elif tool_name == "push":
                    self.phase_gate.transition(CognitivePhase.PUBLISHED)
            
            # Compaction（与 Claude Code 类似）
            if self._needs_compaction():
                self._compact()
```

---

## 关键设计决策

### 1. Phase Gate 在 Permission Checker 之前

Claude Code 的 harness 流程是：`parse action → permission check → execute`。

加了 Phase Gate 之后：`parse action → **phase check** → permission check → execute`。

Phase check 在 permission check 之前，因为 phase 约束比权限约束更基础——
即使在 workspace 里用户给了 agent 所有权限，push 也不应该被允许，因为 phase 不匹配。

### 2. Block 不是异常，是结构化 observation

当 Phase Gate 返回 `allowed=False` 时，harness 不抛异常。它构造一条结构化的 observation，
注入 LLM 的 context：

```
Action 'push' blocked in phase 'workspace'.
Error: NO_FORMALIZED_BOUNDARY.
Suggested: formalize.
```

LLM 在下一轮推理中看到这条 observation，它的行为不是"重试 push"——它被引导去执行 suggested action（formalize）。
这个引导不是 prompt 的软建议，是 phase gate 的硬约束 + 结构化的错误信息。

### 3. Phase 变更由 tool 执行结果驱动，不由 LLM 决定

LLM 不能说"我现在进入 formalized phase"然后 phase 就变了。Phase 变更只在 tool 执行**成功**之后发生：

```python
if tool_name == "formalize" and result.success:
    self.phase_gate.transition(CognitivePhase.FORMALIZED)
```

这保留了 Design A 的确定性——phase 不是 LLM 的推理结果，是 runtime 的状态变更。

### 4. 审计日志是 Phase Gate 的内置行为

Design B 的 event log 被简化为 `PhaseGate._audit_log`——不是外部 daemon 的 pub-sub，而是内存中的
`list[AuditEvent]`。每次 `check()` 调用自动追加。Agent session 结束时可以 dump 到文件，
供 governance analysis 使用。不需要 event stream infrastructure。

### 5. Context Injection 是 push 模式，不是 poll

Design C 的 `getBundle()` 要求 agent 主动拉取状态。嵌入模式改为 push——
每次 `check()` 调用的返回结果里都有 `phase_context`，LLM prompt 在每轮推理前自动注入。
Agent 不需要记得去"查询当前 phase"，phase 信息始终在 context 里。

---

## 与现有 Gitgo 代码的关系

Embedded Phase Gate 不替代 Gitgo。它复用了 Gitgo 的三个核心资产：

1. **Governance state machine**（`GOVERNANCE_STATE.md` + `SyncSession` 的 `step_*()` 方法）→ 映射为 `PhaseGate._is_valid_transition()` 的转移矩阵
2. **Phase semantics**（workspace/trial/curated/formalized/release_ready/published）→ 映射为 `CognitivePhase` 枚举 + `PHASE_PERMISSIONS` 表
3. **Structured error codes**（`NO_FORMALIZED_BOUNDARY` 等）→ 映射为 `PhaseGateResult.error_code`

PhaseGate 可以作为一个独立的 Python module（`phase_gate.py`，~150 行），零依赖，可以被任何 Python agent harness import。
它不需要 Gitgo 的 `SyncSession`、`ConfigManager`、`adapters/`——它只需要 governance 的语义模型。

同时，现有的 Gitgo CLI/daemon/MCP 仍然可以独立运行——用于直接的项目管理、governance analysis、state bundle 导出。
两者不是替代关系，是**同一种 phase 模型在两种载体上的实现**：Gitgo 在外部（管理 git 仓库），PhaseGate 在内部（管理 agent 认知阶段）。

---

## 嵌入模式 vs 三种集成模式

| 维度 | Design A (CLI-Loop) | Design B (Event-Stream) | Design C (Bundle-Polling) | Embedded Phase Gate |
|------|--------------------|------------------------|---------------------------|---------------------|
| Phase gate 位置 | 外部 subprocess | 外部 daemon | 外部 API | **harness 内部 import** |
| 检查延迟 | 高（subprocess 启动） | 中（网络往返） | 中（API 调用） | **低（函数调用）** |
| 审计日志 | CLI exit code | daemon event log | API response | **内存 list** |
| Context 注入 | agent 手动 `gitgo status` | agent 手动 getState | agent 手动 getBundle | **harness 自动 push** |
| 独立性 | Gitgo 可独立运行 | Gitgo 可独立运行 | Gitgo 可独立运行 | **Gitgo 仍可独立运行** |
| 适用场景 | 实验验证 | 生产级多 agent | 上下文管理 | **agent harness 设计** |

---

## 下一步

Embedded Phase Gate 不是一个独立的 Gitgo 版本——它是从 Gitgo 的 governance model 中提取出来的
架构模式，可以被嵌入到任何 agent harness 中。

最直接的验证路径：

1. 从 Gitgo 现有代码中提取 `phase_gate.py`（~150 行，零外部依赖）
2. 在 `examples/agent_loop.py` 的 harness 中加入 PhaseGate
3. 对比两种 agent loop：纯 while loop vs Phase Gate loop
4. 度量：unrelated edits 占比、rollback 次数、task completion consistency
