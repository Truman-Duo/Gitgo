# Gitgo Context Management Architecture

> 设计日期：2026-07-17 | 状态：架构设计 | 定位：实施规格

---

## 零、与已有系统的对接表

| 本设计的概念 | gitgo 已有模块 | 当前状态 | 需改动 |
|-------------|-------------|---------|--------|
| Raw Store | `HistoryManager` | append-only JSONL + threading.Lock | 追加 2 个 operation 类型 |
| L0-L4 固定前缀 | `ContextBuilder` + `agent_step` | 已有 `build_governance_context()` | 添加组装入口 |
| L5 隐用户输入 | `Harness` 三层 Plugin | `PreDispatchGuard.check_tool()` 已做 `session.append_user` | CompletionGuard 同上 |
| L6 拉取通道 | `recall_grep/semantic/rag` | 已注册为 tool_executor | 无 |
| L7-L8 结构化转录 | **新增** | 无 | 新建 `TranscriptBuilder` |
| 压缩优先级链 | `ContextWindow` | 三档水位线 (SOFT/PRUNE/FORCE) | 增加隐用户输入回收 + 依赖图过滤 |
| Context Assembler | **新增** | 无 | 新建 tool executor |
| 依赖图 | `contract.py` | v0.36 新增 `get_callers()` + `get_changed_symbols()` | DependencyChainCheck 消费 |

---

## 一、单 Session 心智模式 —— 具体流程

### 1.1 用户视角

用户始终在一个对话窗口里。只说人话——"修登录 bug"、"加 rate limit"。不知道 A/B fork。

### 1.2 系统实际执行的步骤

**Step 1: A 收到用户指令**

```
用户 → A Agent (Ring 0, session=gitgo-main):
  "修复 login 模块的认证漏洞"
```

**Step 2: A 调 assemble_context 工具，查看 B 会拿到什么**

```
A → tool: assemble_context
  args: { task: "fix-auth", files: ["login.py","auth.py","middleware.py"], role: "executor" }
  
返回:
  context_snapshot: {
    needed: [
      { signal: "lesson_trigger", rule: "修改 auth.py 前必须先 scan", tools:["scan"] },
      { signal: "contract_drift", file: "login.py", rule: "login函数签名已变更" }
    ],
    relevant: [
      { lesson: "L01", trigger: "auth.py", rule: "修改 auth 前 scan", score: 0.92 },
      { lesson: "L03", trigger: "login", rule: "修改登录必须更新测试", score: 0.85 }
    ],
    dependency: {
      "auth.py": { callers: ["login.py:handle_login", "api.py:verify_token"] },
      "login.py": { callers: ["middleware.py:auth_middleware"] }
    },
    transcript_tokens: 1800
  }
```

A 审阅这份 snapshot → 确认足够 → 决定 fork。

**Step 3: A fork B**

```
A → tool: fork_agent
  args: { role: "executor", ring_level: 3, context_snapshot: {...}, instruction: "修复 login 认证漏洞" }
  
内部执行:
  AgentProcessManager.fork() → 创建 AgentProcess
    → _create_agent_scoped_lessons() → 从 context_snapshot 中提取 relevant lessons
    → ContextBuilder.build_from_snapshot() → 构建完整 Context
    
B Agent context 组装结果:
  L0: "你是 gitgo Agent Runtime 中的一个 Agent。你的行为受 RingGate 约束..."
  L1: "项目: gitgo, 工作区: /home/dev/gitgo, 技术栈: python, HEAD: abc123"
  L2: "decided_features: auth(authenticate); 约束: 不使用绝对定位, 不跳过 git hooks"
  L3: "Ring: RING_3, 可用: scan,formalize,recall_grep; 禁止: sync,push,promote_lesson"
  L4: 工具 Schema (scan/formalize/recall_grep/recall_semantic/recall_rag/status)
  L5-L6: (空——隐用户输入由 Harness 在 loop 中动态注入)
  L7: (空——新 task)
  L8: (空——新 task)
```

**Step 4: B 执行 task**

```
agent_step() 循环:

  第1轮: LLM → tool_call: recall_grep("auth")
    → PreDispatchGuard.check_tool("recall_grep", ...) → allowed ✓
    → 执行 → 结果: { lessons: [L01, L03], total: 2 }
    → session.append_assistant("...") + session.append_user(tool_result)
    
  第2轮: LLM → tool_call: scan
    → PreDispatchGuard 检查 → 没有前置条件 → allowed ✓
    → 执行 → 结果: files=5, changed=3
    → 继续
    
  第3轮: LLM → "TASK_COMPLETE，auth.py的authenticate函数已修复"
    → CompletionGuard.on_signals(signals, process):
        _check_required_tools:
          L01.required_tools = ["scan", "test"] → B 调了 scan 但没调 test
          → missing_tools = ["test"]
          → result.blocked = True
    → agent_step: if blocked → session.append_user("[完成前需先调用以下工具] test")
    → _nudge_counter["required_tools"] += 1
    → continue ← 循环继续
    
  第4轮: LLM 看到隐用户输入 → tool_call: test
    → 执行 → 5 passed
    
  第5轮: LLM → "TASK_COMPLETE"
    → CompletionGuard 检查 → 全部通过
    → TaskGate.decide() → allowed
    → process.status = COMPLETED

  ⚠ 如果第3-5轮无限循环（B 反复不调 test）:
    
  第3轮: nudge 注入 → _nudge_counter["required_tools"] = 1
  第5轮: B 再次跳过 test → nudge 注入 → _nudge_counter["required_tools"] = 2
  第7轮: B 再次跳过 → _nudge_counter["required_tools"] = 3
  
  → MAX_NUDGE_REPEAT = 3 触发:
    agent_step 不继续循环:
      process.status = FAILED
      process.result = {
        "failure_reason": "nudge_escalation",
        "nudge_type": "required_tools",
        "repeated_count": 3,
        "missing_tools": ["test"],
        "message": "Agent 连续3次未响应治理约束, 已升级给 A Agent 决策"
      }
    → agent_complete 事件携带此 result
    → A Agent 收到结构化失败原因 → 决定: 换策略 / 询问用户 / 手动介入
```

**Step 5: B 结果返回 A**

```
agent_complete 事件到达 daemon 主循环:
  → _emit agent_complete 到 DaemonClient
  → assemble_return_context(process):
      提取 B 的 session 中的关键信息 → 结构化转录
      {
        task: "fix-auth",
        status: "COMPLETED",
        steps_used: 5,
        tools_called: ["recall_grep","scan","test"],
        lessons_triggered: ["L01"],
        compliance: { L01: {required:["scan","test"], called:["scan","test"], passed:true} },
        files_changed: ["auth.py"],
        key_decisions: ["修改了 authenticate 函数的签名校验逻辑"]
      }

A 收到这份结构化摘要:
  "已修复认证漏洞。修改了 auth.py 的 authenticate 函数。
   遵循了 L01 约束（scan + test），5 个测试通过。"
  
用户看到: A 的这条回复
```

**Step 6: B context 回收**

```
agent_complete 处理完 → ContextWindow.recycle_after_round():
  1. 隐用户输入 (L5-L6) → retention override = 0.1 → 被 prune 清除
  2. Tool 结果 (L7) → snip 为占位符
  3. 对话消息 → 保留在 Raw Store (HistoryManager)
  4. ContextWindow.prune(session, force=True)
```

---

## 二、隐用户输入 —— 具体注入机制

### 2.1 三个注入点

所有注入通过 `session.append_user()` —— 进入对话流，LLM 无法跳过。

| 时机 | Plugin | 代码位置 | 注入格式 |
|------|--------|---------|---------|
| tool_call 前 | `PreDispatchGuard.check_tool()` | `executor.py` 行 132 | `[工具调用被阻止] {tool}: {reason}` |
| 声明完成时 | `CompletionGuard.on_signals()` | `executor.py` 行 159 | `[完成前需先调用以下工具] {tools}` |
| 声明完成时 | `CompletionGuard._check_rejection_instructions()` | `completion.py` 行 91 | `[完成检查] 以下纠正指令未被处理: {instructions}` |

### 2.2 agent_step 中的集成

```python
# executor.py agent_step 伪代码
while process.steps_used < process.max_steps:
    # ... LLM 调用 ...
    
    for tc in tool_calls:
        # Layer 1: PreDispatchGuard
        pre_check = PreDispatchGuard.check_tool(tc.name, tc.args, process, signals)
        if not pre_check.allowed:
            session.append_user(f"[工具调用被阻止] {tc.name}: {pre_check.reason}")
            continue  # 跳过这个 tool_call, 回到 LLM
    
    if _is_completion(response):
        # Layer 2+3: CompletionGuard
        completion = CompletionGuard.on_signals(signals, process)
        if completion.blocked:
            session.append_user(completion.nudge_text)  # ← 隐用户输入
            continue  # 回到循环
            
        process.status = COMPLETED
        return
```

### 2.3 与 CC/OpenCode 的本质区别

| | CC/OpenCode | gitgo |
|---|---|---|
| 信号位置 | System prompt | `session.append_user()` |
| Agent 能否跳过 | 可以（prompt 是建议） | 不能（user message 必须响应） |
| 强制力 | `continue` 阻断循环 | `continue` 阻断循环 |
| Agent 的感知 | "我需要遵守这些规则" | "系统告诉我做 X，我必须做" |

---

## 三、结构化转录 —— 具体格式

### 3.1 任务转录（L7: Task Transcript）

LLM 最擅长理解的结构化 XML。由硬规则在每步工具执行后追加——不是 LLM 生成的。

```xml
<task id="fix-auth" started="2026-07-17T10:00:00">
<step n="1" tool="recall_grep" query="auth" matches="3" top="L01" time_ms="120"/>
<step n="2" tool="scan" files="5" changed="3" key="auth.py,login.py" time_ms="340"/>
<step n="3" governance="blocked" reason="required_tool_test_missing"/>
<step n="4" tool="test" passed="5" failed="0" time_ms="2100"/>
<step n="5" status="completed" tools_used="recall_grep,scan,test" lessons="L01"/>
</task>
```

### 3.2 返回转录（B → A）

B 完成时由 `assemble_return_context` 生成——纯结构化 JSON，A 直接消费。

```json
{
  "task": "fix-auth",
  "status": "COMPLETED",
  "steps": 5,
  "tools": ["recall_grep","scan","test"],
  "lessons_triggered": [
    {"id":"L01","rule":"修改auth前scan","complied":true}
  ],
  "files_changed": ["auth.py"],
  "governance_events": [
    {"step":3,"type":"blocked","reason":"required_tool_test_missing"}
  ]
}
```

### 3.3 压缩转录（Compact 产出）

Compact 最终触发时，LLM 产出也必须走 JSON Schema 约束，不是自由文本。

```json
{
  "compact_version": 3,
  "range": "step_1_to_step_8",
  "constraints": [
    {"rule": "不要改 API 层接口", "scope": "this_task", "source": "user_directive_step2"},
    {"rule": "修改 auth.py 前必须先 scan", "scope": "global", "source": "lesson_L01"},
    {"rule": "数据库变更前需要备份", "scope": "global", "source": "lesson_L03"}
  ],
  "key_decisions": [
    {"step":2,"decision":"scan后确定主要问题在authenticate函数","basis":"scan结果"}
  ],
  "files_touched": ["auth.py"],
  "errors_encountered": [
    {"step":3,"type":"governance_block","reason":"required_tool_test_missing","resolved_at":4}
  ],
  "pending_work": []
}
```

**constraints 字段的填充规则**（规则填充，非 LLM 判断）：

1. **硬规则抓取**：对 compact 范围内的所有 user 消息 + assistant 消息，匹配否定句/禁止句模式：
   `"不要..." "禁止..." "不能..." "先别..." "do not..." "must not..." "never..."`
   → 自动填入 constraints 数组，source="hard_extract"

2. **Lesson 约束继承**：compact 范围内触发的所有 lesson，其 rule 自动填入 constraints：
   → source="lesson_{id}"

3. **PolicyEngine 告警继承**：compact 范围内 PolicyEngine 产生的所有 violation：
   → source="policy_{check_name}"

LLM 不决定要不要写 constraints——系统用规则抓完，直接填进去。LLM 只负责填其他字段。

---

## 四、压缩优先级链 —— 具体算法

### 4.1 实现位置

`ContextWindow.manage()` —— 新增方法，替代当前裸调 `check() → prune() → compact()`。

```python
def manage(self, session, harness_data, llm_provider, dep_graph):
    tokens = session.estimate_tokens()
    budget = self._limit
    
    # ── 50%: 隐用户输入回收 ──
    if tokens > budget * 0.5:
        self._recycle_governance_nudges(session)
    
    # ── 70%: Tool Result Snip（免费）──
    if tokens > budget * 0.7:
        self._snip_old_tool_results(session)
    
    # ── 80%: 依赖图过滤（免费，图遍历）──
    if tokens > budget * 0.8:
        self._dep_graph_filter(session, dep_graph)
    
    # ── 85%: 知识替代（免费，已有 recall 工具结果）──
    if tokens > budget * 0.85:
        self._replace_with_lesson_transcripts(session, harness_data)
    
    # ── 90%: ⚠️ 最后手段 —— LLM Compact ──
    if tokens > budget * 0.9:
        # 生成转录，不覆盖原文
        transcript = self.compact(session, llm_provider, harness_data)
        # 转录存入转录池（HistoryManager event）
        _save_transcript(transcript)
```

### 4.2 每步具体做什么

**`_recycle_governance_nudges`**：
```
遍历 session.messages → 找到 Harness 注入的隐用户输入

对每条 nudge，判断状态:
  ┌─ pending（B 尚未响应）:
  │     此 nudge 之后没有任何 B 的 assistant 消息回复 → 绝对不能碰
  │     → 标记 _nudge_state = "pending"，retention 保持正常优先级
  │     → 为什么: 如果这条被 prune，B 下一轮看不到，等于变相跳过——破坏了 §2.3 的核心卖点
  │
  └─ resolved（B 已经响应过）:
        此 nudge 之后有 B 的 assistant 消息（说明 B 看到了并处理了）
        → 标记 _nudge_state = "resolved"
        → retention override = 0.1 → prune 时优先删除

判断方法:
  idx = 此 nudge 在 session.messages 中的位置
  subsequent = session.messages[idx+1:]
  
  if any(m.get("role") == "assistant" for m in subsequent):
      → resolved (Agent 已响应)
  
  elif len(subsequent) >= MAX_NUDGE_TTL_TURNS:  # 默认 5 轮
      → orphan (Agent 未响应但已过去太多轮)
      → 允许回收，但写入 HistoryManager: operation="orphan_nudge"
      → 为什么需要 orphan: 如果 LLM 生成了空响应导致永远没有 assistant 消息，
        pending nudge 会永远不能回收 → 90% 触发 compact → compact 也清不掉 → 硬崩
  
  else:
      → pending (仍在等待 Agent 响应)

MAX_NUDGE_TTL_TURNS = 5  # 常量，可通过 contract.yaml 覆盖

关键:
  - resolved → 进入回收池（降 retention 到 0.1）
  - orphan → 进入回收池 + 写入 HistoryManager warning event（事后可查）
  - pending → 绝对不能碰（保持正常 retention，不参与 prune）
```

**`_snip_old_tool_results`**：
```
遍历 session.messages → 找到 tool_result 类型
  → 如果已有 _snip_state == "snipped" → 跳过（幂等保护）
  → 如果 tool_result 对应的 tool_call 在 3 轮之前
  → 替换 content 为 "[tool {name}: {N} files, {M}ms elapsed — use recall to retrieve]"
  → 标记 _snip_state = "snipped"
  → 释放原文 token

幂等保护原因: manage() 在同一 session 生命周期内被多次调用(阈值反复触发)时，
已 snip 过的消息不需要再处理——避免重复替换导致格式错乱。
```

**`_dep_graph_filter`**：
```
获取当前 task 涉及的文件列表 → 查依赖图
  → 对每条 session 消息: 
    它涉及的文件在依赖图上离 task 文件几跳？
    0-1 跳 → retention = 0.9
    2 跳 → retention = 0.6
    3+ 跳 → retention = 0.3

⚠ "消息涉及的文件"来源: 不在消费时从 free text 反解 → 在产生时打元数据。
  Message 新增字段: referenced_files: list[str]
  填充时机:
    - tool_result: ToolDispatcher 返回时，从 args 提取 file/path 参数
    - governance_nudge: Harness 注入时，从 signal.target_files 提取
    - assistant/user text: 不反解（自由文本不可靠），该消息 referenced_files=[]
    - compact_transcript: 从 transcript 的 files_touched 提取

⚠ 权重是 v0 经验值，CONFIGURABLE via contract.yaml。
  白名单文件(.gitgo/config.yaml, pyproject.toml, conftest.py)不受跳数限制,
  default retention=0.8。白名单从 HistoryManager 的 governance_drift 记录自动学习:
  过去90天 drift 次数最多的文件自动加入。
```

**`_replace_with_lesson_transcripts`**：
```
对每条对话消息:
  → 如果消息的内容在 recall 检索结果中有对应的 lesson
  → 用 lesson 的结构化转录替代原文
  → lesson 格式: {id, rule, trigger, severity} — 远小于原文
```

---

## 五、Retention 优先级多源合成

一条消息可能同时被多个 filter 打上 `retention_override`。合成规则：取最大值（most conservative——任一 filter 认为重要，就保留）。

```python
final_retention = max(
    signal_based_priority,       # from RetentionAdvisor plugin (lesson_trigger=0.8, rejection=1.0)
    dep_graph_priority,          # from _dep_graph_filter (0-1跳=0.9, 2跳=0.6, 3+=0.3)
    lesson_transcript_priority,  # from _replace_with_lesson_transcripts (0.5)
    nudge_state_priority,        # from _recycle_governance_nudges (resolved=0.1, pending=1.0, orphan=0.1)
)
```

**理由**：任一 filter 认为重要就保留——不会因为两个 filter 互相矛盾而错误裁剪。

---

## 六、L2-ext 注入策略与 Prompt Cache 权衡

L2-ext（中途约束）不注入 `session.messages`。在每次 LLM 调用前动态拼接到 system message 末尾：

```python
def _build_system_message_for_llm(process, base_system):
    if process.task_constraints:
        constraint_block = "\n\n## Task-level Constraints (this task only)\n" + "\n".join(
            f"- {c}" for c in process.task_constraints
        )
        return base_system + constraint_block
    return base_system
```

**代价**：constraint 变化时 system prompt 变化 → 破坏 prompt cache。
**权衡**：task_constraints 在一个 task 内变化频率极低（0-3 次），影响可接受。**这不是 bug，是有意的设计权衡。**

---

## 七、A Agent 的 Recycle

B Agent: kill/reap → context 自然销毁。A Agent: 没有 kill 事件，锚定 `round_complete`。

```
A Agent round_complete:
  1. 界定"本 round 范围": 上次 round_complete 之后新增的 session 消息
  2. 对本 round 内的 recall tool_result → 查热冷分类（classify_lesson_heat）
     hot → 不降优先级
     non-hot → retention override = 0.1
  3. ContextWindow.prune(session, force=True)
  4. Raw 保留在 HistoryManager
```

**A 的 隐用户输入**：A 是 Ring 0，不需要 CompletionGuard 的强制检查（那是 B 需要的）。A 通过 GovernanceSignal 在 assemble_context 时了解状态，而非被 loop 阻断。

---

## 八、Transcript 池的下游消费者

Transcript 存入 HistoryManager（`operation="transcript"`），被以下消费者使用：

| 消费者 | 查询方式 | 用途 |
|--------|---------|------|
| `recall_grep/semantic` | 搜索 transcript 的 `key_decisions`、`constraints`、`files_touched` 字段 | Agent 主动检索历史决策 |
| `assemble_return_context` | 读取最近 N 个 transcript → 生成 B→A 的结构化摘要 | A 审阅 B 的工作 |
| `_replace_with_lesson_transcripts` | 如果 transcript 包含一条 lesson → 用 lesson 的紧凑格式替代原文对话 | 压缩层 85% |
| `compact()` | 读取旧 transcript → 作为 compact 的输入上下文 | LLM compact 时避免重新扫描原文 |

**存了就必须有消费者**。如果某个 transcript 类型当前没有消费者——不存。

---

## 九、常量与单位定义

```python
class ContextConstants:
    TURN_UNIT = "assistant_message"     # 一"轮"=一次 assistant 响应（含 tool_call）
    SNIP_AGE_TURNS = 3                   # tool_result 超过 3 轮 → snip
    NUDGE_TTL_TURNS = 5                  # pending nudge 超过 5 轮 → orphan
    MAX_NUDGE_REPEAT = 3                 # 同一条 nudge 重复注入上限
    DEP_GRAPH_HOP_WEIGHTS = {0: 0.9, 1: 0.9, 2: 0.6}  # default=0.3
    CONTEXT_BUDGET_RESERVED = 3000       # 预留空间（避免 API 413）
```

**计算 age 用索引距离不用时间戳**。时间戳在离线 replay 时不递增，索引在 session.messages 中一定递增。

**transcript_tokens 估算**: 用 tiktoken（`cl100k_base` 或 provider 对应的 tokenizer）。值标记为保守上限（取 `len(tokens) * 1.15`）。`assemble_context` 返回增加 `context_utilization_ratio` 字段，让 A 直接知道会占多少 budget。

---

## 十、中途临时约束晋升

### 5.1 问题

L2 固定前缀里的约束是项目级的（"不使用绝对定位"）——永远不会被压缩。但对话中途用户说的"这次先别动 API 层"活在 conversation 里，随 prune 被清掉。没有机制把它提升到 hard constraint 层。

### 5.2 机制

检测对话中的中途指令 → 写入 `AgentProcess.task_constraints` → 生命期绑定到当前 task。不进入永久 L2（不会污染项目级约束），但在这个 task 内不参与压缩。

```python
# executor.py, 每轮 LLM 响应后
def _promote_mid_task_constraints(session, process):
    """从最近 user 消息中检测中途指令 → 晋升为 task 约束。"""
    recent_user_msgs = [m for m in session.messages[-5:]
                       if m.get("role") == "user"]
    
    for msg in recent_user_msgs:
        content = msg.get("content", "")
        
        # 层1: 硬正则匹配（负例句式）
        candidates = re.findall(
            r'(?:不要|先别|这次不要|暂时别|do not|don\'t|must not|never)\s+(.{10,80})',
            content, re.I
        )
        
        # 层2: 结构过滤（必须包含明确宾语——动词+名词）
        candidates = [c for c in candidates if _has_action_object(c)]
        # _has_action_object: "别改 API"→True（动+名）, "不要这样"→False（无宾语）
        
        # 层3: 排除引述（用户引述 lesson 或系统消息不算新约束）
        candidates = [c for c in candidates
                     if not any(q in c for q in ["lesson", "规则说", "系统提示"])]
        
        for c in candidates:
            if c not in process.task_constraints:
                process.task_constraints.append(c)
                # 写入 HistoryManager（可追溯、可撤销）
                HistoryManager.add_operation(
                    project.name, "task_constraint_promoted",
                    "recorded",
                    {"directive": c, "source_msg_idx": msg.get("_idx"),
                     "task": process.task_description}
                )
    
    # 撤销机制: A Agent 可通过 MCP 工具或在对话中说 "撤销约束 #N" 来移除
    # 撤销 = 从 process.task_constraints 删除 + HistoryManager 写入 "task_constraint_revoked"
```

**L2-ext 的生命周期**：
- 写入时机：每轮 LLM 响应后自动检测
- 作用范围：当前 task 内 → 不参与 ContextWindow 的 prune
- 清除时机：task 完成（B Agent killed/completed）→ task_constraints 清空
- 存储位置：`AgentProcess.task_constraints: list[str]`

---

## 十一、Context Assembler 工具

### 5.1 工具定义

```python
# 注册到 ToolDispatcher
"assemble_context": {
    "description": "为指定 task 组装 Agent Context。返回 context_snapshot 供 A Agent 审阅。",
    "parameters": {
        "task": "str — task 描述",
        "files": "list[str] — 涉及的相对文件路径",
        "role": "executor | reviewer | observer",
        "ring_level": "int — 0 or 3, default 3"
    },
    "returns": {
        "needed": "list[GovernanceSignal] — PolicyEngine 硬约束",
        "relevant": "list[Lesson] — recall 检索结果 + 依赖图打分",
        "dependency": "dict — AST 函数级调用链",
        "transcript_tokens": "int — 预估 token 数"
    }
}
```

### 5.2 内部实现

```python
def _exec_assemble_context(args: dict) -> dict:
    task = args["task"]
    files = args.get("files", [])
    
    # Phase 1: needed — PolicyEngine 最近一次 check
    policy_results = HistoryManager.load(operation="policy_check_result")[-1:]
    signals = SignalNormalizer().normalize(policy_results=policy_results)
    needed = [s for s in signals if any(f in s.target_files for f in files)]
    
    # Phase 2: relevant — recall 检索 + 依赖图打分
    lessons = Knowledge.recall_grep(task, top_k=10)
    dep_graph = load_function_graph(workspace)
    for l in lessons:
        l._relevance_score = _dep_graph_score(l, files, dep_graph)
    relevant = sorted(lessons, key=lambda l: -l._relevance_score)[:5]
    
    # Phase 3: dependency — 函数级调用链
    dependency = {}
    for f in files:
        callers = get_callers(workspace, f)
        dependency[f] = {"callers": callers}
    
    return {"needed": needed, "relevant": relevant, "dependency": dependency, "transcript_tokens": ...}
```

### 5.3 assemble_return_context

```python
def _exec_assemble_return_context(args: dict) -> dict:
    process_id = args["process_id"]
    process = apm.get(process_id)
    
    return {
        "task": process.task_description,
        "status": process.status.value,
        "steps": process.steps_used,
        "tools": _extract_tools_from_session(process.session),
        "lessons_triggered": _extract_lessons_from_session(process.session),
        "files_changed": _extract_files_from_tool_results(process.session),
        "governance_events": _extract_governance_nudges(process.session),
    }
```

---

## 十二、依赖图双消费者 —— 具体调用链

### 6.1 治理侧

```
daemon workspace_dirty
  → PolicyEngine.DependencyChainCheck.check(session, project)
    → for each changed_file:
        get_changed_symbols(file) → ["authenticate"]
        get_callers(workspace, file, "authenticate") → ["login.py:handle_login"]
    → result: DependencyChain 告警 → SignalNormalizer → GovernanceSignal
    → CompletionGuard.on_signals():
        for sig in dependency_signals:
            当前 task 的 files 包含 sig 的 changed_file？
              → required_tools.append("验证 " + sig.callers)
    → B 声明 TASK_COMPLETE 时被阻拦
```

### 6.2 上下文侧

```
ContextWindow._dep_graph_filter(session, dep_graph):
  task_files = 当前 task 涉及的 files
  for each message in session.messages:
    msg_files = 消息中涉及的文件
    min_hops = min(dep_graph.distance(f, task_files) for f in msg_files)
    msg._retention_override = {0: 0.9, 1: 0.9, 2: 0.6}.get(min_hops, 0.3)
  → prune() 时低 override 优先清除
```
