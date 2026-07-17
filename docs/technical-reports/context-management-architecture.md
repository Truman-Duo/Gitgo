# Gitgo Context Management Architecture

> 设计日期：2026-07-17 | 状态：架构设计

---

## 零、核心原则

1. **Raw 永存，压缩是最后手段。** 原文不删。压缩是穷尽所有轻量算法后的兜底。
2. **多套转录，非原地覆盖。** 从同一份 Raw 生成多份 Transcript，每份一个视角。
3. **储存廉价，算法必要。** 硬盘便宜——Raw 全部保留。算法决定什么进入稀缺的 context window。
4. **推负责保底，拉负责补救。** 系统推送"需要的"，Agent 可以拉取更多。
5. **稳定性 > 相关性。** 相同输入应产生 99% 相同的 Context，不让 Assemble 变成波动源。
6. **复用已有基础设施。** PolicyEngine、Knowledge recall、HistoryManager 已经在做检测和检索——不重复造。

---

## 一、Context 完整组成

每个 Agent 启动时，Runtime 从多个来源组装 Context。以下按 Prompt Cache 稳定性从高到低排列。

### 1.1 九层 Context 结构

```
┌──────────────────────────────────────────────┐
│ Layer 0: System Prompt         (~500 tokens) │ ← 永久固定
├──────────────────────────────────────────────┤
│ Layer 1: Project Identity      (~200 tokens) │ ← 项目级，数月不变
├──────────────────────────────────────────────┤
│ Layer 2: Contract & Rules      (~800 tokens) │ ← 合约级，版本变更时更新
├──────────────────────────────────────────────┤
│ Layer 3: RingGate & Permissions (~300 tokens)│ ← Agent 角色级，Fork 时确定
├──────────────────────────────────────────────┤
│ Layer 4: Tool Registry         (~500 tokens) │ ← Agent 角色级，Fork 时确定
├──────────────────────────────────────────────┤
│ Layer 5: Governance Signals     (可变 tokens) │ ← needed, PolicyEngine 每轮推送
├──────────────────────────────────────────────┤
│ Layer 6: Knowledge Brief        (可变 tokens) │ ← relevant, recall 检索结果
├──────────────────────────────────────────────┤
│ Layer 7: Task Transcript       (可变 tokens) │ ← 当前 task 范围内的对话+工具结果
├──────────────────────────────────────────────┤
│ Layer 8: Conversation Tail     (~4000 tokens)│ ← 最近 N 轮完整对话
└──────────────────────────────────────────────┘

Prompt Cache 命中区: L0-L4 (固定前缀, ~2300 tokens)
Prompt Cache 变化区: L5-L8 (每轮/每 task 可能变化)
```

### 1.2 每层详细定义

**Layer 0: System Prompt**

永久固定。告诉 LLM 它是什么角色、它的行为边界、它的基本能力。

```
你是 gitgo Agent Runtime 中的一个 Agent。
你的行为受 RingGate 权限约束，受 PolicyEngine 治理信号约束。
你必须通过工具与环境交互。不能直接读写文件。
完成任务后回复 TASK_COMPLETE。
```

**Layer 1: Project Identity**

项目级信息。切换项目时更新——同项目内稳定。

| 字段 | 来源 | 示例 |
|------|------|------|
| 项目名 | `ProjectConfig.name` | `"gitgo"` |
| 工作区路径 | `ProjectConfig.workspace_path` | `/home/dev/gitgo` |
| 技术栈 | `contract.tech_stack` | `["python", "typescript"]` |
| 当前分支/HEAD | `git rev-parse HEAD` | `abc123` |

**Layer 2: Contract & Rules**

合约约束 + 架构规则。`contract.yaml` 更新时刷新。

| 字段 | 来源 | 示例 |
|------|------|------|
| Decided Features | `contract.decided_features` | `[{name:"auth", location:"auth.py", signature:"def auth()"}]` |
| Architecture Constraints | `contract.architecture_constraints` | `["不使用绝对定位", "不跳过 git hooks"]` |
| 依赖图摘要 | `load_dep_graph()` → 当前 task 涉及文件的相关节点 | `"auth.py ← login.py, api.py"` |

**Layer 3: RingGate & Permissions**

Agent 的权限边界。Fork 时确定，整个生命周期不变。

| 字段 | 来源 | 示例 |
|------|------|------|
| Ring Level | `AgentProcess.ring_level` | `RING_3` |
| 可用工具列表 | `ToolRegistry.list_all()` | `["scan", "formalize", "recall_grep"]` |
| 被禁止的工具 | RingGate 过滤结果 | `sync, push, promote_lesson` 不可用 |
| 可写/可读的目录 | worktree 路径 | `可写: .gitgo/worktrees/{pid}/` |

**Layer 4: Tool Registry**

每个可用工具的 Schema 定义。Fork 时确定。

| 字段 | 来源 | 示例 |
|------|------|------|
| 工具名 + 描述 | `ToolDispatcher._executors` | `recall_grep: 搜索已知教训...` |
| 参数 Schema | 工具定义的 JSON Schema | `{query: str, top_k: int}` |
| 调用格式 | Function calling 或 XML（当前） | `<tool_call><name>recall_grep</name>...` |

**Layer 5: Governance Signals（needed）**

PolicyEngine 每轮 workspace_dirty 后的产出。不参与裁剪——钉为 never-compact。

| 字段 | 来源 | 示例 |
|------|------|------|
| Lesson Trigger 匹配 | `PolicyEngine.LessonTriggerCheck` | `"修改 auth.py 前必须先 scan"` |
| Contract Drift 告警 | `PolicyEngine.ContractDriftCheck` | `"auth.py 的 authenticate 签名已变更"` |
| Identity Integrity 告警 | `PolicyEngine.IdentityIntegrityCheck` | `"CLAUDE.md 文件丢失"` |
| Dependency 告警 | `DependencyChainCheck`（AST 精确版） | `"auth.authenticate() 被 login.handle_login() 调用"` |
| Rejection 历史 | `HistoryManager` | `"上次被拒绝理由: 登录安全验证不足"` |
| 对应 Lesson 的工具约束 | `Lesson.prerequisite_tools` + `required_tools` | `"完成前必须调用: scan, test"` |

**Layer 6: Knowledge Brief（relevant）**

Agent 主动或被动检索的知识。依赖图打分排序，填满 budget 剩余空间。

| 字段 | 来源 | 检索方式 |
|------|------|---------|
| 匹配的 Lesson | `recall_grep/semantic` → 依赖图打分排序 | Agent 主动调 recall / 系统推送 |
| 相关 Fact | `derive_facts()` → 按 project_name 筛选 | 系统推送 |
| 历史决策记录 | `HistoryManager` → 按 changed_files 筛选 | Agent 主动拉取 |

**Layer 7: Task Transcript**

当前 task 范围内的对话 + 工具调用 + 结果。会随 task 进度增长。

| 字段 | 来源 | 示例 |
|------|------|------|
| Task 描述 | `AgentProcess.task_description` | `"修复 auth 模块的登录安全问题"` |
| 本 task 内的对话 | `AgentSession.messages` | user / assistant / tool_result |
| 本 task 内的工具结果 | `ToolDispatcher.dispatch()` 结果 | `scan: 5 files changed` |
| 本 task 内的 Lesson 检索结果 | `recall_grep` 返回的 lesson 列表 | 作为 tool_result 注入 |

**Layer 8: Conversation Tail**

最近 N 轮完整对话（跨 task 的上下文尾巴）。Prune 时最先被裁剪。

| 字段 | 来源 |
|------|------|
| 最近 3-5 轮 assistant 消息 | `AgentSession.messages[-N:]` |
| 这些轮中调用的工具及结果 | tool_call + tool_result pairs |
| 用户的最新指令 | user message |

### 1.3 与 Claude Code / OpenCode 的对比

| Context 层 | Claude Code | OpenCode | gitgo |
|-----------|-------------|----------|-------|
| System Prompt | ✅ | ✅ (SystemContext) | ✅ |
| 项目信息 | ✅ (CLAUDE.md 多层) | ✅ (AGENTS.md) | ✅ (ProjectConfig + contract) |
| 规则/约束 | ✅ (rules/*.md) | ❌ | ✅ (PolicyEngine) |
| 权限 | ✅ (permission ruleset) | ✅ (permission ruleset) | ✅ (RingGate + ToolRegistry) |
| 工具定义 | ✅ (Tool schemas) | ✅ (Tool schemas) | ✅ (ToolDispatcher) |
| **治理信号** | ❌ | ❌ | ✅ **gitgo 独有** |
| **知识/教训** | ⚠️ (MEMORY.md 文件) | ❌ | ✅ (Knowledge recall) |
| **依赖图** | ❌ | ❌ | ✅ **gitgo 独有** |
| **Task 上下文** | ✅ (conversation) | ✅ (conversation) | ✅ |
| 最近对话 | ✅ | ✅ | ✅ |

**gitgo 独有的三层**：Governance Signals（PolicyEngine 推送）、Knowledge Brief（recall 检索）、Dependency Graph（AST 函数级图）。这三层是 gitgo 作为 Agent Runtime 而非普通 Agent 的核心差异——系统主动告诉 Agent "你需要知道这些"，而不是等 Agent 自己发现。

---

## 二、整体数据流

```
                    ┌──────────────────┐
                    │   Raw Store       │
                    │ (HistoryManager)  │
                    │  append-only      │
                    └────────┬─────────┘
                             │
           ┌─────────────────┼─────────────────┐
           ▼                 ▼                  ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ PolicyEngine │  │  Knowledge   │  │  Dependency  │
    │  (需要的)     │  │  recall      │  │  Graph       │
    │  硬约束检测   │  │  (相关的)     │  │  (相关的)     │
    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
           │                 │                  │
           └─────────────────┼──────────────────┘
                             │
                             ▼
                  ┌──────────────────┐
                  │ Context Assembler │
                  │  两阶段组装       │
                  └────────┬─────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
       ┌──────────┐ ┌──────────┐ ┌──────────┐
       │ needed   │ │ relevant │ │ recent   │
       │(never-   │ │(dep-graph│ │(tail     │
       │ compact) │ │ scored)  │ │ window)  │
       └──────────┘ └──────────┘ └──────────┘
              │            │            │
              └────────────┼────────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │  Agent Context   │
                  │  (context window)│
                  └──────────────────┘
```

---

## 三、Raw Store —— 原文层

**实现**：HistoryManager append-only event log（已有）。

所有信息以 event 形式写入，不可删除。每条 event 有 `timestamp`、`operation`、`detail`、`correlation_id`、`project_name`。

| Raw 类型 | HistoryManager operation | 写入时机 |
|----------|------------------------|---------|
| 对话消息 | `agent_message` | Agent 每轮 LLM 调用后 |
| 工具调用结果 | `tool_executed` | ToolDispatcher.dispatch() |
| PolicyEngine 信号 | `policy_check_result` | daemon workspace_dirty |
| Lesson 检索结果 | `lesson_recalled` | Agent 调 recall 工具 |
| 治理简报 | `governance_brief` | ContextBuilder 构建时 |
| 拒绝记录 | `rejection` | round_complete 拒绝 |
| 交付快照 | `workspace_state_snapshot` | round_complete |

---

## 四、"需要的" vs "相关的" —— 两阶段组装

### Phase 1: "需要的" —— never-compact

从 PolicyEngine + rejection history + 当前 task 的 lesson 提取。不参与裁剪、不参与打分。Agent 必须看到。

| 来源 | 提取内容 | 注入方式 |
|------|---------|---------|
| PolicyEngine.LessonTriggerCheck | 匹配当前变更文件的 lesson → `required_tools` + `prerequisite_tools` + `dangerous_tools` | Harness 信号 → system prompt |
| PolicyEngine.ContractDriftCheck | 合约漂移告警 → 违反了什么规则 | 同上 |
| PolicyEngine.IdentityIntegrityCheck | 身份破坏告警 → 哪些文件异常 | 同上 |
| PolicyEngine.DependencyChainCheck | 变更文件的函数级调用链 → 哪些调用者受影响 | 同上 |
| Rejection history | 上次被拒绝的理由 → 这次不能重复 | CompletionGuard 注入 |
| Contract.decided_features | 当前 task 涉及的文件 → 对应的 feature 合约约束 | system prompt |

**实现路径**：所有 PolicyEngine 产出已经通过 SignalNormalizer → GovernanceSignal → SignalBus.dispatch(context="pre_dispatch") 注入 agent_step。接上已有的 Harness 管道即可。

### Phase 2: "相关的" —— 依赖图打分排序

从 HistoryManager + Knowledge recall 中检索与当前 task 相关的材料。按依赖图打分排序，填满剩余 context budget。

**依赖图打分算法**（替代时间衰减）：

```
score(info) = 
  引用距离分 (0-0.5)   // 在依赖图上离当前变更文件几跳？越近越高
  + 统计记忆分 (0-0.3)  // 这个依赖关系过去被违反过几次？
  + 验证次数分 (0-0.2)  // 对应的 lesson 被 verified 过几次？
```

| 维度 | 来源 | 说明 |
|------|------|------|
| 引用距离 | `get_callers(file, func)` | AST 函数级图：谁调了当前变更的函数 |
| 统计记忆 | `Lesson.trigger_count` + `violated_after_count` | 这条 lesson 被触发过几次、触发后 Agent 仍然违反了几次 |
| 验证次数 | `Lesson.verified_count` | 被人工 verify 过的 lesson 权重更高 |

**实现路径**：`contract.py` 已有 `build_function_graph()` + `get_callers()`（v0.36 新增）。`Lesson` 数据模型已有 `trigger_count`、`applied_count`、`violated_after_count`、`verified_count` 字段。

---

## 五、Transcript 系统 —— 多套转录

### 4.1 转录类型

| 转录 | 提取算法 | 格式 | 用途 |
|------|---------|------|------|
| 治理转录 | PolicyEngine → GovernanceSignal → 结构化 JSON | GovernanceSignal 列表 | Agent 的 Harness 消费 |
| 知识转录 | Knowledge recall → Lesson 列表 | Lesson 结构化字段 | Agent 主动检索 |
| 任务转录 | 硬规则按 task_description 过滤 Raw | 对话+工具结果子集 | B Agent 上下文注入 |
| 决策转录 | 从 Raw 中提取决策动词 + 上下文 | Markdown 摘要 | A Agent 跨 task 回顾 |
| 压缩转录 | LLM compact（最后手段） | 结构化 JSON 摘要 | context 不够时的兜底 |

### 4.2 Transcript 格式原则

1. **为 LLM 理解优化，非人类可读。** 不追求自然语言流畅——用结构化标签、明确的分隔符、精确的字段名。
2. **量化内容显式标注。** 数值、时间、行数、token 数——不给 LLM 留模糊空间。
3. **稳定生成。** Transcript 不依赖 LLM 的自由文本生成——用 Schema 约束输出格式。同一段 Raw 两次转录结果应高度一致。

### 4.3 Transcript 存储

知识转录走 Knowledge 系统的三层结构（pending → instance → abstract）。其他转录暂存为 HistoryManager event（`operation="transcript"`）或独立文件。

---

## 六、回收 —— 转录轮换

### 5.1 机制

```
Agent 开始 task:
  → Context Assembler 组装 needed + relevant
  → 注入 context window

Agent 完成 task:
  → round_complete 或 B Agent kill
  → 将当前 context 中的转录存入 Transcript Store
  → RetentionAdvisor 降非 sticky 内容的优先级
  → ContextWindow.prune() 释放空间
  → 原文保留在 Raw Store
```

### 5.2 与知识系统回收的统一

知识回收和上下文回收使用同一机制：
- 知识回收：recall 工具检索的 lesson 在 task 完成后从 context 撤出
- 上下文回收：所有 tool_call/tool_result 对（不只 recall）在 task 完成后撤出
- 判断依据：lesson/信息的热冷分类（`classify_lesson_heat`），非 hot 的降优先级
- 执行方式：`ContextWindow.prune(session, force=True)`

### 5.3 单 Session 轮换

一个项目一个 A Agent session。上下文里始终只有当前 task 需要的内容。用户感受不到切换——Agent 通过 recall 工具可以查到历史 task 的任何信息。

**Prompt Cache 稳定性**：固定前缀（System Prompt + Contract + Role Prompt = ~95%）+ 变化尾巴（Task Transcript + Recent Conversation = ~5%）。

---

## 七、依赖图 —— 治理 + 上下文共用

同一套图数据，两个消费者：

**治理侧**：
```
文件 X 变更 → get_changed_symbols(X) 找到变了什么函数
→ get_callers(workspace, X, func) 找到谁调了它
→ DependencyChainCheck 告警精确到函数
→ CompletionGuard 在交付前验证调用者
```

**上下文侧**：
```
Agent 要改 X → 查依赖图 → 找到 X 在依赖链上的相关文件
→ 这些文件的历史决策/lesson/对话 → 加权保留
→ 不相关的 → 正常参与 prune
```

分层精度：
- Level 1: `get_dependents()` — import 正则，毫秒级，全量跑
- Level 2: `get_callers(file, func)` — AST 函数级，只对 Level 1 命中的文件做
- Level 3: 统计记忆 — Lesson 的 `trigger_count` + `violated_after_count`，HistoryManager 已有

---

## 八、实现优先级

| 优先级 | 内容 | 状态 |
|--------|------|------|
| P0 | PolicyEngine → never-compact 管道（已有 Harness，需接上 ContextWindow） | 部分 |
| P0 | AST 函数级依赖图（刚加入 contract.py，需在 DependencyChainCheck 中消费） | 新增 |
| P1 | 依赖图打分替换时间衰减（ContextWindow prune 优先级改依赖图权重） | 待做 |
| P1 | Transcript 统一存储格式（复用 Knowledge 三层或独立 event） | 待做 |
| P2 | 单 session 轮换（Assembler 稳定性优先 + Prompt Cache 分区） | 待做 |
| P2 | LLM compact 最后手段化（穷尽轻量算法后才调） | 待做 |
