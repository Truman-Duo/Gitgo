# gitgo Runtime Constitution

> 最后更新：2026-07-24
> 这些是 gitgo Runtime 的宪法级设计原则。任何新功能或系统变更不得违反这些约束。

---

## 1. advisory soft gate 不可信，只有 structural hard gate 才可信

任何依赖 LLM "记得去做"、"想去看"、"应该去检查"的机制都是设计错误。LLM 没有可靠的记忆，只有当前上下文。如果某个约束需要被遵守，必须是编排层在结构上强制执行的——注入到 context、拦截在 tool call 之前、验证在结果返回之后。不给模型留下选择空间。

**实例：** contract drift 从"警告然后人决定"升级成"工具调用层面硬拦截"。agent 间通信从"B2 可选地读 slot-notes"升级成"Scheduler 强制注入上游产出摘要到 B2 的 needed 层"。

## 2. 冲突升级给父级，不是子级之间私聊

改变任务边界的权限只在父级。子级之间没有互相修改对方任务定义的权限。B1 和 B2 之间的任何冲突或协作需求，通过父级 A 协调，不是通过直接的 B1↔B2 通信。这维持了权限模型的清晰性——和 depth ≤ 2 + B 级不能 fork_agent 的既有约束一致。

## 3. 上下文隔离和文件系统共享是正交的

不要把进程隔离和文件系统隔离绑定。每个执行单元有独立的 context window（LLM 注意力不分散），但所有执行单元共享同一个 workspace 文件系统（内容同步不需要通信协议）。这两个维度独立控制——传统的"隔离 = 无法协作"不是物理定律，是架构选择。

## 4. LLM 没有 happens-before 关系

LLM 不知道时间流逝。它只知道上下文里现在有什么。不要依赖时间戳、时钟、或"先发生/后发生"语义做状态决策。分布式系统里的 Lamport clock、vector clock 一类工具在 agent 系统里完全失效。唯一可信的状态是：context 里被显式注入的内容。

## 5. 权限即路由

子单元的能力通过 Capability 四元组（tool_allowlist + ring_level + resources + max_steps）定义，不通过预定义角色名（如 "coder" / "explore" / "plan"）。路由到哪个 Backend、能否执行某个操作——这些由四元组硬约束决定，不靠字符串标签。

## 6. 递归深度可配置

当前默认递归深度 ≤ 2（A → B，B 不能再 fork）。但这个数字不是架构常量——它是从 Reasonix 借鉴的当前合理默认值。未来如果有场景需要更深层次，系统应支持配置化调整。

## 7. Escalate 必须有 Recovery Policy

错误升级不能停在 `print(error)`。每一条 Escalate 路径必须显式定义恢复策略：至少包含默认动作（当前：中止全部子 slot，单一上下文重跑）、可选的有限重试（当前：最多 1 次）、以及人工介入的升级路径。不做"只检测不恢复"的机制。

---

## 适用范围

这些原则约束：
- `backend/core/loop/` 中的所有新模块
- `backend/core/daemon/` 中的 agent 相关逻辑
- 任何涉及多执行单元协调的新功能

不约束：
- 纯数据模型（如 Lesson dataclass）
- 纯工具实现（如 recall_grep）
- Dashboard UI
