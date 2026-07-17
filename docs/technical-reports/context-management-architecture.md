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

## 一、整体数据流

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

## 二、Raw Store —— 原文层

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

## 三、"需要的" vs "相关的" —— 两阶段组装

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

## 四、Transcript 系统 —— 多套转录

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

## 五、回收 —— 转录轮换

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

## 六、依赖图 —— 治理 + 上下文共用

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

## 七、实现优先级

| 优先级 | 内容 | 状态 |
|--------|------|------|
| P0 | PolicyEngine → never-compact 管道（已有 Harness，需接上 ContextWindow） | 部分 |
| P0 | AST 函数级依赖图（刚加入 contract.py，需在 DependencyChainCheck 中消费） | 新增 |
| P1 | 依赖图打分替换时间衰减（ContextWindow prune 优先级改依赖图权重） | 待做 |
| P1 | Transcript 统一存储格式（复用 Knowledge 三层或独立 event） | 待做 |
| P2 | 单 session 轮换（Assembler 稳定性优先 + Prompt Cache 分区） | 待做 |
| P2 | LLM compact 最后手段化（穷尽轻量算法后才调） | 待做 |
