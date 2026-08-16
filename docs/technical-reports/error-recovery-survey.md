# Error Recovery & Robustness: Cross-Project Survey + gitgo Gap Analysis

> 调研日期：2026-08-05
> 调研范围：gitgo (自有代码) + 4 个参考项目 (Claude Code, Kimi CLI, OpenCode, Reasonix)

---

## 一、跨项目模式对比总表

| 能力维度 | gitgo | Claude Code | Kimi CLI | OpenCode | Reasonix |
|---------|-------|-------------|----------|----------|----------|
| **LLM 调用重试** | 无 | withRetry.ts (820行) max=10, jitter+retry-after | tenacity max=3, jitter | SessionRetry.policy, Effect Schedule, max=2 | SendWithRetry max=10, jitter+retry-after |
| **流中断恢复** | StreamInterruptedError×1→chat()降级 | stream→non-streaming fallback + tombstone | 无显式流恢复 | 全局 buffered event queue 重放 | maxStreamRecoveries=1, step--, recovery prompt |
| **连接重建** | 无 | SSE Last-Event-ID, WS CircularBuffer | 401→OAuth refresh+client rebuild | 无 | streamWithReconnect max=3 (仅零token时) |
| **工具错误=数据** | ToolResult.is_error pipeline | tool_use_error 消息 | ToolResult 值 (永不crash) | LLM.ToolFailure TaggedError | error文本回传模型 |
| **优雅降级** | EventBus隔离, compaction→no-op, embedding→None, scheduler→single-B | 429→fast-mode模型降级 | web fetch→本地HTTP, AI title→截断文本 | image resizer→omit, compaction LLM→skip | fold digest→机械fallback, 超时token估算 |
| **事务/回滚** | _restore_snapshot stub, rollback从未调用 | 无文件级回滚 | Context checkpoint→revert (JSONL), session fork/undo | Git snapshot→restore→revert, 文件锁接管 | git-free snapshot→RestoreCode (JSON/turn) |
| **进程崩溃恢复** | watcher/poller/reader restart, reap orphans | daemon worker restart backoff 2s→120s, MAX_RAPID_FAILURES=5 | telemetry crash handlers, worker heartbeat恢复, SIGTERM→SIGKILL | 文件锁心跳接管(60s stale), SIGTERM→SIGKILL | recover() panic→TurnDone, crash丢失≤1 turn |
| **死循环检测** | CompletionGuard nudge, doom_loop detection | 无显式 | 无显式 | doom loop THRESHOLD=3→permission ask | stormBreakThreshold=3, repeatedSuccessBlock=2 |
| **事件/流控** | micro-batch 16ms/32event, priority tiers | sleep detection重置budget | 无 | tick counter防stale events | ChunkError sentinel, ctx.Done select |
| **遥测持久化** | 无 | 无 | disk spool→startup replay | 无 | crash-report worker |

---

## 二、gitgo 现有错误处理全景（代码普查结果）

### 2.1 已落实的健壮性设计

**流式恢复** (`executor.py`, `llm.py`):
- `StreamInterruptedError` 携带 partial_text + partial_tool_calls
- MAX_STREAM_RECOVERIES=1 → 重试一次 → chat() 降级
- 降级成功→继续；降级也失败→ProcessStatus.KILLED

**重试逻辑**:
- `harvest.py`: MAX_HARVEST_RETRY=5（知识收割重试）
- `task_slot.py`: retry state 字段（预留未接线）

**优雅降级**:
- EventBus 订阅者异常隔离（一个订阅者崩溃不影响其他）
- ContextWindow.compact() LLM 失败→no-op（不阻断循环）
- EmbeddingProvider 不可用→返回 None（不阻断 recall）
- recall_semantic→recall_grep fallback
- Scheduler 多Agent失败→降级为 single-B 执行
- Policy gates 加载失败→builtins fallback

**错误结果传播**:
- ToolResult.is_error=True → 管道不抛异常，作为 tool_result 发回模型
- ProcessStatus.KILLED 状态机转换链完整
- DispatchResult 4 字段错误传递
- Scheduler escalation/recovery_action 字段

**守护进程线程恢复**:
- watcher/poller/reader 线程崩溃后自动重启
- 孤儿进程 reaping
- stdin EOF→shutdown 优雅退出

### 2.2 已知缺口

| 缺口 | 严重度 | 现状 |
|------|--------|------|
| LLM 调用无重试 | **高** | 一次 HTTP 失败直接变 StreamInterruptedError |
| `_restore_snapshot()` stub | **高** | ToolExecution rollback 形同虚设 |
| `rollback()` 从未被 agent_step 调用 | **高** | 写入失败后文件系统状态不回滚 |
| 无 LLM 429/5xx 重试 | **高** | 瞬态失败 = 任务失败 |
| daemon subprocess 无重试 | **中** | 子进程崩溃后不自动重启 |
| 无遥测崩溃持久化 | **中** | 崩溃信息丢失 |
| tool_execution resources 冲突检测已实现但 execute_batch 未使用 | **低** | 仍用 read_only 二值分区 |

---

## 三、参考项目核心模式提炼

### 3.1 重试引擎 —— 所有项目都有，gitgo 没有

**共通模式**：指数退避 + 随机抖动 + retry-after header 解析

```
Claude Code: 2^(attempt-1)*1000ms + jitter, max=10, 429/529→model fallback
Kimi CLI:    2^(attempt-1)*300ms + jitter, max=3, tenacity库
OpenCode:    500*(2^retries) + jitter, max=2, Effect Schedule
Reasonix:    2^(attempt-1)*500ms + jitter, max=10, maxBackoff=15s
```

**gitgo 适用方案**：取最保守的——max=3, base=1s, 2^(attempt-1)*1s + random(0,1s), 解析 Retry-After header。重试条件：5xx + 429 + connection/timeout errors。400/401 不重试。

### 3.2 工具错误 = 模型反馈 —— gitgo 已有此模式

所有 5 个项目（包括 gitgo）都遵循：工具执行出错 → 生成一条错误消息 → 发给 LLM。没有项目让工具异常直接穿透 Agent Loop。

gitgo 的 ToolResult.is_error + formatted 文本回传已完整实现此模式。

### 3.3 事务回滚 —— gitgo 有骨架无血肉

| 项目 | 方案 |
|------|------|
| OpenCode | Git snapshot (snapshot/index.ts): track()→patch()→restore()→revert() |
| Reasonix | git-free snapshot (checkpoint.go): 每 turn 一个 JSON, RestoreCode 回退文件+对话 |
| Kimi CLI | Context checkpoint (JSONL rotation + backup), session fork/undo |

gitgo 的 `ToolExecution._take_snapshot()` / `_restore_snapshot()` 是 stub。建议采用 Reasonix 方案（git-free，每 turn JSON snapshot，轻量且与 gitgo 的 HistoryManager 兼容）。

### 3.4 优雅降级 —— gitgo 做得最好

gitgo 的 EventBus 隔离 + compaction no-op + embedding→None + recall fallback + scheduler degradation 链条是 5 个项目中最完整的。Claude Code 和 Kimi CLI 在具体子系统上降级更激进（模型降级、OAuth 重建），但 gitgo 的降级覆盖面最广。

### 3.5 流中断恢复 —— Reasonix 是标杆

Reasonix 的 `maxStreamRecoveries=1` + `step--` 是 gitgo 流恢复设计的直接灵感来源。关键差异：

| | gitgo | Reasonix |
|---|-------|----------|
| 恢复次数 | 1 | 1 |
| step 预算 | 不变（step 在成功后递增） | step--（step 在循环头递增） |
| 恢复提示 | 3 种模式 | 3 种模式（partial tool / partial text / bare） |
| 连接重建 | 无 | streamWithReconnect (max=3, 仅零 token 时重连) |
| 降级路径 | chat() fallback | 无（纯流式） |

gitgo 的 chat() 降级路径是比 Reasonix 更安全的设计——Reasonix 没有非流式 fallback。

### 3.6 连接/会话恢复 —— Claude Code 独有

Claude Code 的 SSE Last-Event-ID replay + WS CircularBuffer + conversationRecovery.ts 是唯一完整实现"断线重连不丢消息"的项目。这个复杂度对 gitgo 当前阶段来说过度。

### 3.7 死循环检测 —— Reasonix + OpenCode + gitgo 都有

| 项目 | 机制 | 阈值 |
|------|------|------|
| Reasonix | stormBreakThreshold (相同错误) + repeatedSuccessBlock | 3 / 2 |
| OpenCode | doom loop THRESHOLD | 3 |
| gitgo | CompletionGuard nudge counter + doom_loop detection | MAX_NUDGE_REPEAT (可配) |

---

## 四、gitgo 错误恢复模块实施建议

按优先级排序：

### P0 (必须做): LLM 调用重试引擎

缺这个就是"瞬态网络故障 = 任务失败"。所有 4 个参考项目都有，gitgo 没有。

**方案**：`loop/llm_retry.py` (新文件, ~80 行)
- `retry_chat(provider, messages, max_retries=3, base_delay=1.0)`
- 指数退避 + jitter + Retry-After 解析
- 重试条件: 5xx, 429, connection/timeout errors
- 不重试: 400, 401, 402
- 替换 executor.py 中的裸 `llm.chat()` / `llm.stream_chat()`

### P1 (应该做): 事务回滚实现

`_restore_snapshot()` 是 stub，写入失败后文件系统不回滚。

**方案**：`loop/tool_execution.py` (~50 行)
- `_take_snapshot()`: 记录本 turn 涉及文件的内容哈希 (SHA256)
- `_restore_snapshot()`: 回退变更文件到快照内容
- `execute_batch()`: write 工具失败→自动 rollback（仅回退本批次写入）
- 采用 Reasonix 的 per-turn JSON snapshot 格式，与 HistoryManager 兼容

### P2 (建议做): daemon subprocess 重试

子进程崩溃后不自动重启。

**方案**：`daemon/__init__.py` (~20 行)
- 子进程启动失败→backoff 重试 (max=3, 2s→4s→8s)
- MAX_RAPID_FAILURES=5 快速崩溃检测（借鉴 Claude Code）

### P3 (锦上添花): 遥测崩溃持久化

**方案**：写入 `.gitgo/crashes/` JSON 文件，下次启动时上报

---

## 五、自查清单

- [ ] LLM 重试是否区分 4xx（不重试）和 5xx（重试）？
- [ ] 重试是否解析 Retry-After header？
- [ ] 事务回滚是否只回退本批次写入（不回退其他 Agent 的写入）？
- [ ] 回滚粒度是文件级还是 turn 级？
- [ ] daemon 重试是否设 MAX_RAPID_FAILURES 防止快速循环？
- [ ] 所有新增错误处理是否保持 539+ tests passed？

---

## 六、参考项目关键文件索引

### Claude Code
- `src/utilities/withRetry.ts` — 重试引擎 (820 行)
- `src/conversationRecovery.ts` — 会话恢复
- `src/process-coordinator.ts` — daemon worker 重启
- `src/tools/toolExecution.ts:498` — 工具错误→tool_use_error

### Kimi CLI
- `src/kimi/retry.py` — tenacity 重试配置
- `src/kimi/session.py` — checkpoint/revert/undo
- `src/kimi/client.py` — OAuth refresh + client rebuild

### OpenCode
- `packages/opencode/src/session/retry.ts` — SessionRetry.policy
- `packages/opencode/src/snapshot/index.ts` — Git 快照服务
- `packages/opencode/src/session/processor.ts` — 四阶段边界

### Reasonix
- `internal/agent/agent.go` — stream recovery + executeOne error
- `internal/provider/retry.go` — SendWithRetry + backoff
- `internal/checkpoint/checkpoint.go` — git-free snapshot
- `internal/provider/provider.go` — StreamInterruptedError + IsStreamInterrupted
- `internal/agent/cache_shape.go` — CompareShape cache 诊断
