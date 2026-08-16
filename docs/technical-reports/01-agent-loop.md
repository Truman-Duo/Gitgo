# 报告一：Agent Loop —— 自包含执行引擎深度解析

> gitgo v0.35 | 2026-07-16 | 完全透底技术报告

---

## 概述

gitgo 的 Agent Loop 是一个**完全自包含的多步 Agent 执行引擎**。与 Claude Code 依赖系统提示驱动循环不同，gitgo 自己实现了 LLM 调用 → XML 工具调用解析 → 权限检查 → 工具分发 → 策略验证 → 循环的完整链路。

**核心文件**（| 文件 | 行数 | 职责 |）

|------|------|------|
| `loop/executor.py` | 482 | 多步执行引擎主循环 |
| `loop/llm.py` | 343 | LLM HTTP 调用 + CircuitBreaker + Failover |
| `loop/context_window.py` | 288 | 三档水位线上下文管理 |
| `loop/manager.py` | 197 | Agent 进程树 fork/kill/reap |
| `loop/signal_bus.py` | 273 | HarnessPlugin 信号分发总线 |
| `loop/signal_normalizer.py` | 191 | 多源治理数据归一化 |
| `loop/signals.py` | 188 | 统一信号格式（GovernanceSignal） |
| `loop/context_builder.py` | 257 | 治理上下文构建 |
| `loop/session.py` | 70 | Agent 会话管理 |
| `loop/gate.py` | 51 | RingGate 权限模型 |
| `loop/tools.py` | 40 | 工具注册表 |
| `loop/task_gate.py` | 78 | 任务完成门检查 |
| `loop/models.py` | 43 | 数据模型 |
| `loop/harness/pre_dispatch.py` | 113 | 工具调用前策略检查 |
| `loop/harness/completion.py` | 120 | 任务完成前验证 |
| `loop/harness/retention.py` | 98 | 上下文裁剪优先级 |

---

## 一、系统定位

```
┌──────────────────────────────────────────────────────────────┐
│                      入口层                                  │
│  Dashboard ──→ MCP Server ──→ DaemonClient ──→ Daemon       │
│                                                    │         │
│                                          _handle_command()   │
│                                                    │         │
│                                    ┌───────────────┘         │
│                                    ▼                         │
│  ┌─────────────────────────────────────────────────────┐     │
│  │              Agent Loop (本报告)                      │     │
│  │                                                      │     │
│  │  AgentProcessManager.fork()                          │     │
│  │       │                                              │     │
│  │       ▼                                              │     │
│  │  agent_step(process, llm_provider, ...)              │     │
│  │       │                                              │     │
│  │       ├─→ ContextWindow.check()    水位线检查         │     │
│  │       ├─→ LLMProvider.chat()       HTTP → OpenAI API │     │
│  │       ├─→ _parse_tool_calls()      XML 解析          │     │
│  │       ├─→ SignalBus.check_tool()   Harness Layer 1   │     │
│  │       ├─→ ToolDispatcher.dispatch() 工具执行         │     │
│  │       ├─→ CompletionGuard          Harness Layer 2+3 │     │
│  │       └─→ TaskGate.decide()        完成判定          │     │
│  │                                                      │     │
│  └─────────────────────────────────────────────────────┘     │
│                    │                                         │
│                    ▼                                         │
│  Policy Engine ←─ SignalNormalizer ←─ HistoryManager         │
└──────────────────────────────────────────────────────────────┘
```

Agent Loop 是整个 gitgo 的"大脑"——它不依赖 Claude Code 或其他外部 Agent 框架。LLM 调用在 Loop 内部通过 HTTP POST 直连 OpenAI API 完成。

---

## 二、完整执行追踪：一次 agent_step() 的全流程

以 MCP 工具 `gitgo_agent_chat` 为例，追踪 B Agent 从创建到完成的全链路。

### 2.1 外部触发 → Daemon 接收

```
MCP Client (Dashboard / Claude Code)
  │
  └─→ mcp_tools/loop.py: gitgo_agent_chat(project="gitgo", message="修复 bug")
        │
        ├─→ _resolve_llm_config(workspace)
        │     ├─ 检查环境变量 GITGO_LLM_BASE_URL / GITGO_LLM_API_KEY / GITGO_LLM_MODEL
        │     └─ 回退到 .gitgo/llm_config.json active_provider
        │
        ├─→ build_governance_context(project_name, workspace_path)
        │     从 HistoryManager 收集 policy_results + lessons + rejections + facts
        │     → SignalNormalizer.normalize() → {signals, brief}
        │
        ├─→ get_client(project_name)  # DaemonClient 单例
        │     │
        │     ├─→ client.send_command("llm_configure", providers=[...])
        │     │     Daemon 内部：从 llm_config.json 加载多 Provider → 创建 LLMProvider
        │     │
        │     ├─→ _ensure_b_agent(client, project, ctx)
        │     │     │
        │     │     └─→ client.send_command("fork_agent", {
        │     │           role: "B", ring_level: "ring_3",
        │     │           tool_registry: [...], max_steps: 10,
        │     │           context_snapshot: {signals, brief}
        │     │         })
        │     │           │
        │     │           └─→ Daemon._handle_command("fork_agent")
        │     │                 → AgentProcessManager.fork(...)
        │     │
        │     └─→ client.send_agent_run(process_id, instruction="修复 bug")
        │           │
        │           └─→ Daemon._handle_command("agent_run")
        │                 → ThreadPoolExecutor.submit(
        │                      agent_step, process, llm, instruction, dispatcher, workspace)
        │
        └─→ 等待 DaemonClient 收到 agent_complete 事件（异步响应路由）
```

### 2.2 agent_step() 主循环逐步骤追踪

代码见 `loop/executor.py` 行 37-229。

#### Step 0: 前置检查

```python
session = process.session
if session is None:
    return _error_result(process, "NO_SESSION")
if process.status != ProcessStatus.RUNNING:
    return _status_result(process, session)
```

#### Step 0a: 工具 Prompt 注入（仅首次）

`_inject_tool_prompt()` 遍历 session.messages 查找是否已有 TOOL_PROMPT_MARKER。如果未注入：
1. 取 `dispatcher._executors.keys()` 与 `process.tool_registry.list_all()` 的交集
2. 构造 XML 格式工具说明，追加到 system 消息末尾

注入后的 system prompt 包含：
```
## 可用工具 (B Agent Ring 3)
可用工具: scan, formalize, sync, push, trial_list, trial_triage

如需调用工具，请使用以下格式：
<tool_call>
  <name>工具名</name>
  <args>{"key": "value"}</args>
</tool_call>
```

#### Step 0b: SignalBus 初始化（新格式）

```python
harness = process.context_snapshot or {}
signals = harness.get("signals")
_use_signal_bus = signals is not None  # 有 signals 键 = 新格式

if _use_signal_bus:
    _signal_bus = SignalBus.from_contract(workspace_path) if workspace_path else SignalBus()
    _pre_result = _signal_bus.dispatch(signals, process, context="pre_dispatch")
    if _pre_result.suggestions:
        for sug in _pre_result.suggestions[:3]:
            session.append_user(f"[治理建议] {sug}")
```

新格式 vs 旧格式判断：`context_snapshot` 有 `"signals"` 键 → 新格式（GovernanceSignal 列表）；否则 → 旧格式（flat dict），使用旧式 `_policy_pre_check()`。

#### Step 1: 上下文窗口检查（每轮循环开始）

```python
window_check = _context_window.check(session)
if window_check["action"] == "prune":
    _context_window.prune(session, harness_data=harness)
elif window_check["action"] == "force_compact":
    # 通过 SignalBus 获取 retention 建议后再 compact
    _context_window.compact(session, llm_provider, ...)
```

水位线判定（context_window.py:36-60）：
- `ratio = tokens / 128000`
- >= 90% → force_compact（LLM 摘要，付费）
- >= 80% → prune（智能裁剪，免费）
- >= 50% 且首次 → soft_warn（仅通知，保护 prompt cache）

#### Step 2: LLM 调用

```python
response = llm_provider.chat(
    session.to_openai_messages(),
    provider_id=process.provider_id,
)
session.append_assistant(response)
process.steps_used += 1
```

异常 → 进程 KILLED。重试逻辑在 LLMProvider 内部完成。

#### Step 3: XML tool_call 解析

正则 `TOOL_CALL_PATTERN` 提取 `<tool_call>` 块。每块解析为 `{name, args}`。

对每个 tool_call：
1. Harness Layer 1: SignalBus.check_tool() 或 _policy_pre_check()
2. 阻止 → 追加 `[工具调用被阻止]` 消息
3. 通过 → ToolDispatcher.dispatch() → 追加工具结果
4. `continue` 回到循环顶部（让 LLM 处理工具结果）

#### Steps 4-8: 无 tool_call 时的完成检查

```python
if _is_completion(response):
    # Harness Layer 2+3: CompletionGuard
    completion_result = _signal_bus.dispatch(signals, process, context="completion")
    if completion_result.blocked:
        continue  # 缺失工具 → 回到循环

    # TaskGate
    gate_decision = _task_gate.decide(process, response)
    if gate_decision.need_reentry:
        continue  # 要求重入 → 回到循环

    process.status = ProcessStatus.COMPLETED
    return _make_result(...)
```

完成信号检测：匹配 `"任务完成"` / `"TASK_COMPLETE"` / `"DONE:"` / `"FINAL_ANSWER:"` 等 7 个标记。

#### Steps 9-10: 死循环检测

无 tool_call 也无完成信号的纯文本响应：

1. `check_doom_loop()`: 连续 3 次相同 (tool_name, args) → KILL
2. `_repeated_plain_text()`: 连续 3 条 assistant 消息前 100 字符相同 → KILL
3. `check_budget_continuity()`: 最近 5 轮 CV < 0.2 且无行动 → 追加提示

### 2.3 循环终止条件汇总

| 终止条件 | 状态 | 行号 |
|----------|------|------|
| TASK_COMPLETE + 所有检查通过 | COMPLETED | 194 |
| steps_used >= max_steps | KILLED | 227 |
| LLM API 异常 | KILLED | 115 |
| doom_loop 检测 | KILLED | 205 |
| plain_text_loop 检测 | KILLED | 211 |

---

## 三、LLMProvider 深度解析（llm.py 343 行）

### 3.1 设计决策

gitgo 的 LLMProvider 选择 **urllib.request + 标准库**，零第三方依赖。原因：
- daemon 子进程不需要额外安装 Python 包
- 完全掌控 HTTP 请求构建和响应解析
- 兼容所有 OpenAI-compatible API（Groq、DeepSeek、本地 vLLM 等）
- 打包到 exe 时不需要处理第三方库

### 3.2 调用层次

```
chat() ─┬─ 指定 provider_id → _chat_with_provider_id()
        ├─ failover 模式 → _chat_with_failover()
        └─ 单 provider 模式 → _chat_single()
          └─ _chat_single_provider()  [重试循环，指数退避: 1s→2s→4s]
              └─ _chat_once()  [单次 HTTP POST，无重试]
```

### 3.3 _chat_once() — 单次 HTTP 调用

```python
def _chat_once(self, cfg, messages, max_tokens, timeout, model_override=""):
    body = json.dumps({
        "model": model_override or cfg.model_id,
        "messages": messages,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{cfg.base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]
```

### 3.4 CircuitBreaker 状态机

每个 Provider 独立熔断。状态转换：

```
CLOSED ──连续5次失败──→ OPEN ──30s后──→ HALF_OPEN
   ▲                                        │
   │         on_success() ──────────────────┘
   │         on_failure() ──→ OPEN
   └────────────────────────────────────────┘
```

关键参数：`failure_threshold=5`，`recovery_timeout=30s`。

HALF_OPEN 的 `allow()` 始终返回 True，意味着可以有多个并发请求同时试探——无锁保护。

### 3.5 多 Provider Failover

```python
def _chat_with_failover(self, messages, max_tokens, timeout, max_retries):
    with self._semaphore:  # Semaphore(3) 限制并发
        for cfg in self._providers:  # 按 priority 排序，低=优先
            breaker = self._breaker_mgr.get_or_create(cfg.provider_id)
            if not breaker.allow():
                continue  # 跳过已熔断的 provider
            try:
                result = self._chat_single_provider(cfg, messages, ...)
                self._breaker_mgr.on_success(cfg.provider_id)
                return result
            except RuntimeError as e:
                self._breaker_mgr.on_failure(cfg.provider_id)
                if not _is_retryable(str(e)):
                    raise  # 400/401 立即抛出，不尝试下一个 provider
                continue  # 可重试错误 → 下一个 provider
```

### 3.6 重试与可重试错误

可重试：HTTP 429（限流）、500/502/503/504（服务端错误）、连接失败（URLError）
不可重试：HTTP 400（请求格式错误）、401（认证失败）

### 3.7 向后兼容

`LLMProvider(base_url, api_key, model_id)` → 单 provider 模式 + fallback_models 链
`LLMProvider(providers=[...], failover_enabled=True)` → 多 provider failover 模式

---

## 四、RingGate 权限模型

### 4.1 两级环设计

| 环 | 枚举 | 权限 | 角色 |
|----|------|------|------|
| RING_0 | 治理环 | 全部工具可用 | A Agent |
| RING_3 | 执行环 | 仅注册表内工具 | B Agent |

RING_0 专属工具：`sync`, `push`, `accept_trial`, `promote_lesson`, `modify_contract`。

### 4.2 RingGate.check() 逻辑

```python
if process.ring_level == RingLevel.RING_0:
    return GateResult(allowed=True)  # 全通，无检查

# RING_3 三步检查：
# 1. registry 非空？
# 2. tool_name in registry？
# 3. tool_name 不是 ring_0 工具？
```

### 4.3 Per-Agent ToolRegistry

每个 AgentProcess 拥有独立的 ToolRegistry 实例。A Agent 包含全部 47 个 MCP 工具。B Agent 由 fork 时传入，通常只包含 scan/formalize/sync/push/trial_* 等执行层工具。

---

## 五、AgentProcessManager 进程树

### 5.1 fork() 完整流程

1. **深度检查**：parent.parent_id 非 None → B Agent 不能再次 fork（MAX_FORK_DEPTH=2）
2. **B Agent 上下文过滤**：
   - 新格式：白名单过滤 signals（仅保留 severity >= MEDIUM，即 critical/high/medium）
   - 旧格式：检查禁止字段 `contract_yaml`, `lessons_full`, `contract_raw`
3. **创建 AgentProcess**：UUID process_id，RUNNING 状态
4. **创建独立 AgentSession + 注入治理简报**
5. **创建独立 worktree**：`git worktree add --detach` → `.gitgo/worktrees/{pid}/`

### 5.2 Worktree 管理

Windows 兼容：`creationflags=0x08000000`（CREATE_NO_WINDOW）。

_cleanup_worktree: `git worktree remove --force` + `git worktree prune`

### 5.3 进程生命周期

```
fork() → RUNNING
  ├─ agent_step() 完成 → COMPLETED
  ├─ kill() → KILLED（清理 worktree）
  ├─ max_steps 耗尽 → KILLED
  ├─ 异常/熔断 → KILLED
  └─ reap() → ORPHANED（父进程消失时清理 worktree）
```

---

## 六、AgentSession

- `append(role, content)` 追加消息含时间戳
- `estimate_tokens()` = `sum(len(content) for msg) // 4`（粗略近似）
- `to_openai_messages()` 过滤内部字段，返回 `[{role, content}]`
- `inject_governance_brief(brief)` 兼容新旧格式，插入 system prompt

---

## 七、ContextWindow 上下文管理

### 7.1 三档水位线

| 水位线 | 阈值 | 动作 | 代价 |
|--------|------|------|------|
| SOFT | 50% (64K) | 仅通知 | 零 |
| PRUNE | 80% (102K) | 智能裁剪 | 零 |
| FORCE | 90% (115K) | LLM 摘要压缩 | 付费 |

SOFT 只触发一次（`_flags` 集合追踪）。

### 7.2 prune() 算法

1. 从后往前标记 tail 区（保留 tail_tokens=16000 的最近消息）
2. 遍历：system 永远保留；tail 区保留；_retention_priority >= 0.7 保留；其余替换为 elided 标记

### 7.3 保留优先级

| 匹配 | 优先级 | 含义 |
|------|--------|------|
| rejection 指令前30字符匹配 | 1.0 | 不可裁剪 |
| lesson_trigger 文件匹配 | 0.8 | 前科文件 |
| contract_drift 文件匹配 | 0.7 | 漂移文件 |
| critical_feature 匹配 | 0.6 | 关键功能 |
| 默认 | 0.3 | 普通消息 |

### 7.4 compact() LLM 摘要

1. 保留 system + 最近 3 条
2. 中间消息提取前 300 字符拼接
3. LLM 调用（max_tokens=2048, timeout=60s）
4. 重建消息列表：`[system] + [摘要] + [最近3条]`
5. 失败静默返回 False（不压缩）

### 7.5 Token Budget 停滞检测

最近 5 条 assistant 消息：计算变异系数 CV。CV < 0.2 且无 tool_call/完成标记 → stagnant。

---

## 八、信号系统

### 8.1 设计动机

6 个不同来源产生异构治理数据 → 统一 GovernanceSignal 格式 → HarnessPlugin 只需消费一种类型。

### 8.2 GovernanceSignal 格式

```python
@dataclass
class GovernanceSignal:
    signal_id: str; source: str       # 6种来源
    severity: SignalSeverity          # CRITICAL|HIGH|MEDIUM|LOW
    category: SignalCategory          # BLOCK|WARN|SUGGEST|NOTIFY
    target_tools: list[str]           # 受影响工具
    target_files: list[str]           # 受影响文件
    prerequisite_tools: list[str]     # 前置工具
    required_tools: list[str]         # 必须工具
    rule: str; suggestion: str        # 人类可读
    check_id: str; metadata: dict
```

6 个工厂方法：from_lesson_trigger / from_contract_drift / from_identity_integrity / from_dependency_chain / from_rejection / from_fact。

### 8.3 SignalNormalizer 归一化

```python
def normalize(policy_results, lessons, rejections, facts):
    signals = []
    signals.extend(_from_policy_results(...))  # 已知key用工厂方法
    signals.extend(_from_lessons(...))         # 有工具约束才转换
    signals.extend(_from_rejections(...))      # 提取纠正指令
    signals.extend(_from_facts(...))
    return _sort_by_priority(signals)  # severity > category > source
```

### 8.4 SignalBus 三上下文路由

```python
CONTEXT_PLUGIN_ROLES = {
    "pre_dispatch": ["pre_dispatch_guard"],
    "completion":  ["completion_guard"],
    "retention":   ["retention_advisor"],
}
```

每个上下文 dispatch 时只运行对应角色的插件。插件通过 `accepts(signal)` 过滤订阅的信号源和严重级别。

### 8.5 三个 HarnessPlugin

**PreDispatchGuard**：工具调用前验证 prerequisite_tools + drift 文件保护
**CompletionGuard**：Layer 2（required_tools 检查）+ Layer 3（rejection 50% 词匹配）
**RetentionAdvisor**：计算保留优先级（1.0/0.8/0.7/0.6/0.3）

---

## 九、Context Builder

```python
def build_governance_context(project_name, workspace_path, changed_files=None):
    # 1. 从 HistoryManager 收集：policy_results, lessons, rejections, facts
    # 2. SignalNormalizer 归一化 → signals[]
    # 3. 构建文本摘要 brief（800-1500 tokens）
    return {"signals": signals, "brief": brief}
```

**双轨输出**：signals（结构化，给 HarnessPlugin 精确匹配）+ brief（文本，注入 LLM system prompt）。

---

## 十、TaskGate 与死循环检测

- **TaskGate**: MAX_REENTRY=2。零步完成→重入；重入超限→强制放行
- **check_doom_loop**: 连续 3 次相同 (tool, args) → True
- **_repeated_plain_text**: 连续 3 条 assistant 前 100 字符相同 → True
- **check_budget_continuity**: 最近 5 轮 CV<0.2 且无行动 → stagnant

---

## 十一、模块间数据流

```
HistoryManager ──→ ContextBuilder ──→ SignalNormalizer
                                         │
                                    GovernanceSignal[]
                                         │
                                         ▼
                                     SignalBus
                                    /    |    \
                         PreDispatch  Completion  Retention
                              \         |         /
                               HarnessResult
                                    │
                                    ▼
                               agent_step()
```

---

## 十二、测试覆盖

| 测试文件 | 测试内容 | 方法 |
|----------|----------|------|
| test_executor.py | XML解析、工具结果格式化、策略预检查、拒绝检查、完成检测 | 纯单元 |
| test_llm.py | 重试逻辑、CircuitBreaker状态机、断路器管理器 | 纯单元 |
| test_gate.py | RING_0全通、RING_3注册表、未注册拒绝、ring_0拒绝 | 纯单元 |
| test_session.py | 消息追加、token估算、OpenAI转换、治理注入 | 纯单元 |
| test_manager.py | fork A/B、上下文过滤、深度限制、worktree | 纯单元 |
| test_context_window.py | 水位线、prune优先级、compact、停滞检测 | 纯单元 |

所有 loop 测试为纯单元测试，不依赖外部资源（不连接真实 LLM API、不创建真实 git worktree）。

---

## 十三、已知限制与潜在问题

### 设计级别

1. **compact() 失败静默**：LLM 异常只返回 False，上下文可一直增长到超限
2. **HALF_OPEN 无并发保护**：高并发下多请求可能同时试探半开状态
3. **max_steps 直接 KILL**：不给 Agent 保存/总结已完成工作的机会
4. **Token 估算粗略**：字符数/4 在中英文混合时偏差大

### 实现级别

5. **_repeated_plain_text 只比较前100字符**：可能误判
6. **旧格式兼容代码冗余**：约40行可删除
7. **compact() max_tokens=2048 硬编码**
8. **Agent 无跨 session 记忆**

### 未实现

9. Agent checkpoint/resume（断点续跑）
10. 流式 LLM 响应

---

## 十四、设计审查总结

### ✅ 已实现

自包含 Agent 循环、XML tool_call 解析、环级权限隔离、三档水位线、治理信号统一、Harness 三层注入、多 Provider failover、B Agent worktree 隔离、上下文过滤、三层死循环检测。

### ⚠️ 部分实现

compact() 失败降级、HALF_OPEN 并发安全、max_steps 优雅处理。

### ❌ 未实现

Agent checkpoint/resume、流式响应、跨 session 记忆。

---

## v0.34-v0.35 更新补遗

**v0.34**: daemon 新增 native task 命令, 编排逻辑下沉。MCP 回归兼容层。Agent Loop 自包含。

**v0.35**: tool_executors 3→6 (新增 recall_grep/semantic/rag)。Agent 可主动检索 Knowledge。

---

## v0.36-v0.41 更新补遗

> 以下 6 个版本为本报告（Agent Loop）所覆盖主题的后续演进。除 v0.36 已落地外，其余均为**架构阶段**——架构设计 + 骨架代码已就位，具体子任务见对应架构报告的延期项 / Phase 2，尚未实施。

**v0.36 上下文管理（已落地，+38 测试 → 539）**:
- `loop/context_window.py`: 九层 Context 分层（System / Identity / Contract / Governance / Knowledge / Session / Transcript / Tool / Signal），水位线自动检测
- `loop/signals.py` + `signal_normalizer.py` + `signal_bus.py`: 治理信号统一采集、归一化与分发总线
- `loop/session.py`: 会话状态持久化
- `loop/harness/`（completion / pre_dispatch / registry / retention / tool_history）: Harness 三层注入框架
- `loop/transcript.py` + Assembler + Compact: 压缩链（Assembler/Transcript/Compact），上下文 >80% 修剪、>90% LLM 摘要压缩
- 详见 `context-management-architecture.md`

**v0.37 多 Agent 运行时（架构）**:
- `loop/scheduler.py` + `task_slot.py`: Actor Model 调度器 + TaskSlot 四件套（接口/状态/数据/执行）
- `loop/interface_contract.py`: 结构化通信契约（L0/L1/L2 三级）
- `loop/event_bus.py`: 事件总线；`loop/execution_context.py`: 执行上下文
- `loop/llm_adapter.py` + `agent_tool.py`: LLM 适配 + Agent 工具封装
- `tools/{registrations,runner}.py`: 工具注册中心
- Phase 2 数据驱动迭代（SSHBackend / RemoteLLMBackend / Escalate 多分支 / DAG 深度可配置）未实施，见 `multi-agent-architecture.md` §十二

**v0.38 完成判断（架构）**:
- `loop/loop_guard.py` + `gate.py` + `task_gate.py`: 任务完成语义判定骨架
- `loop/harness/completion.py`: BUSINESS 结果判定
- 完成判定策略细化待迭代，见 `error-recovery-architecture.md` §十六

**v0.39 错误恢复（架构，+40 测试 → 579）**:
- `loop/error_taxonomy.py`: 四维分类（Source / Severity / Retryability / Nature，Nature = CRASH vs BUSINESS）
- `loop/loop_guard.py` + `loop/harness/tool_history.py`: 事务回滚 + 重试引擎 + 工具历史追踪
- `loop/process_tool_runner.py`: 子进程工具运行器
- 延期项（Rust PyO3 ToolRunner 6 个月后评估 / 遥测持久化 P3）见 `error-recovery-architecture.md` §十五

**v0.40 流式响应（架构）**:
- 流式事件管线骨架就位，后端 LLM 流式 → 前端渲染端到端接线待迭代（详见报告六补遗）

**v0.41 前端工作（架构）**:
- 组件矩阵 + god module 解耦收尾（详见报告六补遗）
