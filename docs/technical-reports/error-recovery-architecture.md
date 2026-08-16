# gitgo Error Recovery Architecture Design

> 设计日期：2026-08-05
> 基于：gitgo 自有代码全量普查 + Claude Code / Kimi CLI / OpenCode / Reasonix 4 项目源码分析
> 原则：做全做好，不设最小工作量 / 大项目长周期要稳 / 基于已有做最合理决策

---

## 一、设计原则

从 5 个项目的跨项目调研中提炼出 3 条设计原则：

1. **错误是数据，不是终止信号**。工具执行失败转成消息发给 LLM——LLM 能自己纠错，框架替它决定只会丢掉这种能力。5/5 项目遵循。
2. **恢复策略按错误类别分流**。网络瞬态、配额限制、请求错误、上下文溢出——是不同的物理原因，不能用同一个"重试 N 次"笼统处理。Claude Code 和 OpenCode 做得最好。
3. **状态一致性 = 文件 + 对话同步回退**。回滚不能只回文件不回调对话——LLM 会看到"幻影成功"。Reasonix 和 Claude Code 都是文件+对话同步回退。

---

## 二、错误分类体系

### 当前状态

gitgo 没有运行时错误分类。所有错误是 ad-hoc 字符串（`TOOL_NOT_FOUND`、`PROCESS_NOT_RUNNING`、`nudge_escalation`）。治理信号层有 severity/category 分类，但只用于 governance，不用于执行错误。两者语义不同，不可混用。

### 设计

错误四维分类：**(Source, Severity, Retryability, Nature)**。

```
Source（来源）        Severity（严重度）       Retryability（可重试性）      Nature（错误性质）
────────────────     ──────────────────      ──────────────────────      ──────────────────
LLM      LLM 层      FATAL   任务不可继续     RETRYABLE     可无限重试    CRASH    工具本身出错
TOOL     工具层      ERROR   当前操作失败     LIMITED(n)    有限次重试    BUSINESS 工具正常但业务失败
SYSTEM   系统层      WARN    可继续但需注意   NON_RETRYABLE 不可重试
DAEMON   守护进程    INFO    仅记录
```

**Nature 维度是事务回滚的决策依据**（见第五节）：

| Nature | 语义 | 示例 | 回滚？ |
|--------|------|------|--------|
| CRASH | 工具本身崩溃/超时/异常 | Python exception、HTTP timeout、OOM | **回滚** |
| BUSINESS | 工具正常执行但业务结果为失败 | test exit_code≠0、lint 报错、编译失败 | **不回滚** |

Nature 区分解决了错误恢复和完成判断两个子系统的语义冲突：run_test 失败是 BUSINESS——工具正常执行完成了，退出码非零是业务结果。LLM 应该看到这个失败并修 bug，而不是整个批次被回滚消失。

### 新增文件

`backend/core/loop/error_taxonomy.py`（~60 行）

### 参考项目

- OpenCode：Effect TS TaggedError（`LLM.ToolFailure`、`InvalidArgumentsError`），每个错误类型自带恢复策略
- Claude Code：`withRetry.ts` 的 `retryable(error)` 谓词决定是否重试
- Reasonix：`IsStreamInterrupted`、`IsConnReset` 特征分类

---

## 三、LLM 重试引擎

### 当前状态

LLMProvider 已有简陋重试 + CircuitBreaker + failover，但三个组件的关系不清晰：
- `_chat_single_provider` 内有硬编码重试循环（1s→2s→4s, max_retries=3）
- CircuitBreaker 跟踪单 provider 健康状态（OPEN/HALF_OPEN/CLOSED）
- Failover 在主 provider 熔断时切到备用

问题：旧重试循环和 CircuitBreaker 各自计数，不协调。`harvest.py` 和 `compact()` 还有独立的 ad-hoc 重试逻辑。

### 设计

**三层分工，各管一维**：

```
新重试引擎（本次实现）  →  单 provider 内临时错误的分类重试
                           ├─ 5xx / connection reset / timeout → 指数退避 + jitter, max=5
                           ├─ 429 → 解析 Retry-After header, 最多等 30s
                           ├─ 400/401/402/403 → NON_RETRYABLE, 不重试
                           ├─ Context overflow → 降 max_tokens 后重试 1 次
                           └─ CircuitBreaker OPEN → NON_RETRYABLE → 不重试，立即失败

Failover（已有，不动）  →  跨 provider 切换
                           主 provider 连续失败 → 切到备用

CircuitBreaker（已有，保留）→  单 provider 健康状态跟踪
                              CLOSED → 正常调用
                              失败计数 ≥ 阈值 → OPEN → 拒绝请求
                              HALF_OPEN → 试探性放行一个请求
```

**协作规则**：
1. 删除 `_chat_single_provider` 里的旧重试循环——只保留 CircuitBreaker 状态机
2. 新重试引擎收到 CircuitBreaker OPEN 抛出的异常 → 打 `NON_RETRYABLE` 标签 → 不重试该 provider → 交给 Failover 切备用
3. 新重试引擎的重试计数和 CircuitBreaker 的失败计数互相独立——前者决定"这个请求是否重试"，后者决定"这个 provider 是否还健康"
4. 删除各处 ad-hoc 重试（`harvest.MAX_HARVEST_RETRY`、`compact.summarizeWithRetry`）——由 LLMProvider 统一覆盖

**分类重试策略**：

| 错误类别 | 策略 | 参数 |
|---------|------|------|
| 5xx + connection reset + timeout | 指数退避 + jitter | max=5, base=1s, maxBackoff=10s |
| 429 rate limit | 解析 Retry-After header | 跟服务端指令，最多等 30s |
| 400 Bad Request | **不重试** | 请求本身有问题 |
| 401/402/403 | **不重试** | 认证/配额问题 |
| Context overflow | 降 max_tokens 后重试 1 次 | 借鉴 Claude Code |

### 改动文件

`backend/core/loop/llm.py`（~120 行：新重试引擎 ~80 行 + 删旧循环 ~20 行 + CircuitBreaker 协作 ~20 行）

### 参考项目

- Claude Code：`withRetry.ts` (820 行)，max=10, jitter+retry-after, 429→fast-mode 模型降级, context-overflow→降 max_tokens
- Kimi CLI：tenacity，max=3, initial=0.3s, max=5s
- OpenCode：`SessionRetry.policy`，Effect Schedule，分类：5xx 重试 / ContextOverflow→compaction / FreeLimit→终止
- Reasonix：`SendWithRetry`，max=10, maxBackoff=15s

---

## 四、工具错误 → 模型反馈

### 当前状态

已实现。`ToolResult.is_error=True` → `formatted` 文本回传 LLM。异常不穿透 Agent Loop。

### 需补充

结合错误分类体系（第二节），工具错误打上 `ErrorSource.TOOL` + `Nature` + 具体 `code`→ 模型收到结构化信息（`"[TOOL/CRASH/FILE_NOT_FOUND] 文件不存在：path/to/file"`），比纯文本更利于 LLM 理解。

### 改动文件

`backend/core/loop/tool_pipeline.py` Step 5 中 catch 异常后调用 `_classify_tool_error(e)`（~15 行）

---

## 五、事务回滚 + 会话同步

### 当前状态

`ToolExecution` 有完整的事务生命周期（`begin()` → `execute_batch()` → `commit()/rollback()`），但：
- `_take_snapshot()` 只记录文件 size/mtime，不存内容
- `_restore_snapshot()` 是 `pass` stub
- `rollback()` 从未被 `agent_step()` 调用
- 失败后对话继续——LLM 看到"幻影成功"（文件已回退但对话里显示成功）

### 设计

**回滚粒度**：Execution 级（一次 LLM 回复的所有工具调用 = 一个原子事务）。依赖图不够可靠（静态正则+AST，缺动态导入/别名/非 Python 文件），不用于回滚决策。

**回滚触发条件**（关键——Nature 维度在此决策）：

| 触发条件 | 动作 |
|---------|------|
| `ToolResult.is_error == True` + `Nature == CRASH` | **回滚整个 Execution** |
| `ToolResult.is_error == True` + `Nature == BUSINESS` | **不回滚**——作为正常 tool_result 发给 LLM |
| 任一工具超时（TOOL_TIMEOUT） | **回滚整个 Execution** |

run_test exit_code≠0 是 BUSINESS——工具正常执行完成，业务结果为失败。这是完成判断子系统需要的信号，不能回滚。

**回滚方式**：内容备份 + SHA256 去重。借鉴 Claude Code `fileHistory.ts`。

```
ToolExecution.begin():
  对本批次所有 write 工具的目标文件:
    sha = sha256(file_content)
    备份: .gitgo/snapshots/{sha}@v{N}
    记录: (file_path → sha) 映射

ToolExecution.execute_batch():
  写入工具: 写前记录 (file_path → new_sha)
  任何工具返回 CRASH → rollback() 整个 Execution
  BUSINESS 失败 → 不回滚，作为正常结果追加到 session

ToolExecution.rollback():
  对本批次已执行的每个 write 操作:
    从快照恢复文件内容:
      修改文件 → 写回原始内容
      新建文件 → 删除
      删除文件 → 从快照写回
  裁剪 session.messages:
    移除本 Execution 的所有 tool_call + tool_result
    但跳过 _nudge_state == "pending" 的治理 nudge 消息
      （即使它属于被回滚的 Execution——pending nudge 受 Context Management 设计保护）
  注入回滚通知:
    "[系统] 上一批次操作已回滚：{reason}。请重新尝试。"
  发射 rollback_notification 流式事件:
    Dashboard 收到后对该 turn 加删除线/灰化，不物理删除已渲染文本
```

**三种恢复模式**（借鉴 Claude Code `/rewind`）：
- **默认**：文件 + 对话同步回退
- 后续扩展：只回退文件 / 只回退对话

**为什么 SHA256 去重有用**：Claude Code 的方案——相同内容的文件共享同一份备份。多 Execution 引用同一版本不会重复占用磁盘。

**为什么 gitgo 比 Claude Code 更好**：Claude Code 的 checkpoint 不跟踪 Bash 命令（rm、mv、sed）。gitgo 所有操作走 ToolPipeline——不存在这个盲区。

### 改动文件

`backend/core/loop/tool_execution.py`（~80 行） + `backend/core/loop/executor.py`（~20 行，调用 rollback 逻辑）

### 参考项目

- Claude Code：`fileHistory.ts`，SHA256 去重，per user-prompt 快照，三种恢复模式，MAX_SNAPSHOTS=20
- Reasonix：`checkpoint.go`，git-free snapshot，per-turn JSON，RestoreCode 回退文件+对话（MsgIndex 截断）
- OpenCode：`snapshot/index.ts`，Git snapshot，track→patch→restore→revert
- Kimi CLI：Context checkpoint，JSONL 轮转+备份，session fork/undo

---

## 六、流中断恢复

### 当前状态

**已实现，达标，不动。**

`StreamInterruptedError` → 保存 partial_text + partial_tool_calls → 注入恢复提示 → 重试 1 次 → 耗尽后 `chat()` 非流式降级。这是 Reasonix 的方案 + chat() 降级安全网——Reasonix 没有的安全网。

---

## 七、超时管理 + 工具进程隔离

### 当前状态

工具执行**完全没有超时**。`ToolPipeline.execute()` 直接调 `tool.execute(args)`——死工具 = Agent Loop 永远卡住。

且工具在当前架构中是**线程**调用。Python 线程有一个致命问题：`future.cancel()` 只置标志位，**不能真正杀线程**。这是 GIL 模型的根本限制，没有 workaround。

### 设计

**工具执行层走进程隔离——Python `subprocess.Popen`**。不用 Rust（见下文"为什么不是 Rust"）。

```
Python 侧                              Python 子进程
─────────                             ──────────────
ToolPipeline                          gitgo.tools.runner
  ↓                                      ↓
  validate_args()                      stdin: tool_name + args_json
  ↓                                      ↓
  execute() ──── subprocess ────▶     execute tool
  ↓                                      ↓
  format_result()                      stdout: result_json
  ↓
  if timeout → _kill_tree() → TOOL_TIMEOUT
```

**ProcessToolRunner**（~150 行，标准库零依赖）：

```python
import subprocess, sys, os, signal, time, json

class ProcessToolRunner:
    """Run a tool in an isolated subprocess with true timeout kill."""

    def run(self, tool_name: str, args: dict, timeout_secs: float = 60) -> dict:
        proc = subprocess.Popen(
            [sys.executable, "-m", "gitgo.tools.runner", tool_name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            preexec_fn=os.setpgrp if sys.platform != "win32" else None,
        )
        try:
            stdout, stderr = proc.communicate(
                input=json.dumps(args).encode(),
                timeout=timeout_secs,
            )
            return json.loads(stdout)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            return {
                "is_error": True,
                "error": {"code": "TOOL_TIMEOUT", "nature": "CRASH",
                          "message": f"Tool {tool_name} timed out after {timeout_secs}s"}
            }
        except json.JSONDecodeError:
            self._kill_tree(proc)
            return {"is_error": True, "error": {"code": "TOOL_CRASH", "nature": "CRASH",
                     "message": f"Tool {tool_name} returned invalid output"}}

    def _kill_tree(self, proc):
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(5)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
```

**超时三级体系**：

| 层级 | 管理者 | 默认值 | 超时后 |
|------|--------|--------|--------|
| 单工具执行 | ProcessToolRunner | 工具自声明（`AgentTool.timeout`），默认 60s | SIGTERM → 5s → SIGKILL → ToolResult(is_error=True, nature=CRASH) |
| 批次总时长 | Python ToolExecution | 300s | 剩余工具不再执行，已有结果+超时错误一起返回 |
| LLM 调用 | LLMProvider | 120s | 进重试引擎（第三节） |

**为什么不是 Rust**：`multiprocessing.Process` / `subprocess.Popen` 已经能做到真进程隔离、真杀死、跨平台支持（Windows CREATE_NEW_PROCESS_GROUP + taskkill /F /T）。零依赖、零编译链。Rust ToolRunner 放入迭代计划（见第十五节），在出现以下需求时再评估：cgroup 资源限制、seccomp 沙箱、进程级性能监控。

### 新增/改动文件

`backend/core/loop/process_tool_runner.py`（~150 行）+ `backend/core/loop/tool_pipeline.py` 接入（~20 行）+ `backend/core/loop/agent_tool.py` 加 `timeout` 字段

---

## 八、死循环检测 + Storm Break

### 当前状态

已有两套机制：
- `CompletionGuard`：nudge counter ≥ MAX → FAILED
- `LoopGuard._repeated_plain_text` / `_repeated_success_block`：重复输出/重复成功 → blocked

**缺失**：不检测"工具反复返回相同错误"。

### 设计

**扩展 LoopGuard**，新增 `_repeated_tool_errors()`：
- 触发条件：同一 `(tool_name, error_code)` 连续 ≥ 3 次
- 不用 error_hash（不哈希 error message——路径/数字变化会导致相同错误模式被判定为不同，漏报死循环）
- `FILE_NOT_FOUND × 3` → 触发。无论路径是 `/foo/a.py`、`/foo/b.py`、`/foo/c.py`——error_code 统一是 `FILE_NOT_FOUND`
- 触发后注入 nudge：`"操作 {tool_name} 已连续 3 次返回相同错误 [{error_code}]，请换策略。"`

### 改动文件

`backend/core/loop/loop_guard.py`（~30 行）

### 参考项目

- Reasonix：`stormBreakThreshold=3`
- OpenCode：`doomLoopThreshold=3`→弹权限询问

---

## 九、幂等性与状态一致性

### 当前状态

只有一个地方做了去重：`LoopGuard._canonicalize_args`。工具本身没有幂等键，Dispatcher 没有去重。

### 设计

**Execution 级幂等键**：`idempotency_key = sha256(process_id + step_number + tool_names)`。
- 崩溃恢复时检查幂等键 → 已完成的 Execution 跳过 → 未完成的回滚
- 不依赖工具本身是否幂等——未完成的统一回退是最安全的策略

**工具级幂等**（后续扩展，本次不实施）：给 `AgentTool` 加 `idempotent: bool`。标记为幂等的（scan、recall）可安全重试，非幂等的（write、formalize）需要回滚后重试。

### 改动文件

`backend/core/loop/tool_execution.py` + `backend/core/loop/executor.py`（~30 行）

### 参考项目

- Claude Code：`fileHistoryTrackEdit` SHA256 去重
- Kimi CLI：checkpoint turn-index 截断实现幂等恢复

---

## 十、优雅降级

### 当前状态

**5 层 fallback 链，5 个项目中最完整。已达标，不动。**

| 层级 | 降级路径 |
|------|---------|
| EventBus | 订阅者异常隔离，一个崩溃不影响其他 |
| ContextWindow.compact() | LLM 失败→no-op，不阻断循环 |
| EmbeddingProvider | 不可用→None→recall_grep fallback |
| Scheduler | 多 Agent 失败→single-B 执行 |
| Policy gates | 加载失败→builtins fallback |

---

## 十一、进程崩溃恢复

### 当前状态

Daemon 侧做得不错：watcher/poller/reader 线程崩溃自动重启、孤儿进程 reaping、PID 文件 + stale 检测、SIGTERM/SIGINT 处理。

**缺失**：子进程崩溃后不自动重启（没有 backoff）、Agent session 纯内存（daemon 崩溃 = B Agent 对话全丢）。

### 设计

**子进程 backoff 重启**（借鉴 Claude Code daemon worker）：
- 启动失败→退避重试：2s→4s→8s（max=3）
- MAX_RAPID_FAILURES=5：30s 内崩溃 5 次→放弃，发告警
- 重试间隔内检查并 kill 上一轮残留子进程

**会话持久化**：见第十二节。

### 改动文件

`backend/core/daemon/__init__.py`（~30 行）

### 参考项目

- Claude Code：daemon worker restart，BACKOFF_INITIAL=2s, CAP=120s, MAX_RAPID_FAILURES=5
- Kimi CLI：background worker heartbeat 恢复，SIGTERM→SIGKILL 升级
- OpenCode：文件锁 heartbeat 接管（60s stale），token-based lock release

---

## 十二、会话持久化与恢复

### 当前状态

**极弱**。`AgentProcessManager._processes` 纯内存，`AgentSession` 没有持久化。daemon 崩溃 = 所有 B Agent 对话丢失。`sync_session.save_session()` 只保存 stage + formal_commits，不保存 Agent 对话。

### 设计

**append-only JSONL**——和 HistoryManager 同一模式，不是全量 JSON 覆写。架构一致性优于表面便利。

```
.gitgo/sessions/{process_id}.jsonl
每行一条 session event:
  {"type": "message_added", "step": 42, "message": {...}}
  {"type": "step_completed", "step": 42, "steps_used": 43}
  {"type": "status_changed", "status": "active"}
```

**写入策略**（平衡 I/O 压力和恢复精度）：
- 每个关键状态变化点写入：工具执行完成、step 递增、状态变更
- daemon shutdown 时（finally block）：全量 checkpoint 写入 `.gitgo/sessions/{process_id}.checkpoint.json`（atomic write: tmp → rename）
- 正常完成时：删除 jsonl + checkpoint

**恢复流程**：
1. daemon 启动→扫描 `.gitgo/sessions/`→找到未完成的 session（有 jsonl 文件）
2. 优先从 checkpoint 加载（更高效），缺失的部分从 jsonl replay
3. 注入恢复提示：`"系统在上一轮操作后重启。请检查当前状态并继续。"`
4. 重新进入 agent_step 循环

**为什么用 JSONL 而不是全量 JSON**：
- 和 HistoryManager 的 append-only 模式一致——架构统一
- 原子性：每行一条独立 event，写入中断最多丢最后一行，不会半行损坏
- 不需要 fsync——崩溃后丢弃最后一条不完整行即可恢复
- 降低了 I/O 频率——只写增量，不写全量

### 改动文件

`backend/core/loop/manager.py` + `backend/core/daemon/__init__.py`（~80 行）

### 参考项目

- Claude Code：checkpoint 持久化到 session storage，session resume 后可 rewind
- Kimi CLI：`.kimi/sessions/` JSONL
- Reasonix：per-turn JSON checkpoint，crash 最多丢 1 个 in-flight prompt

---

## 十三、资源清理

### 当前状态

基本完善：PID 文件 acquire/release + stale 检测、atexit + finally 清理、`hash_cache.flush()`。

### 需补充

temp 文件和 worktree 清理。`ToolExecution.commit()` 成功后清理该 Execution 的快照备份（已不需要回退）。

### 改动文件

`backend/core/daemon/__init__.py`（~20 行）

---

## 十四、连接恢复 + Auth 恢复

### 决策：本次不实施

**连接恢复**（SSE Last-Event-ID / WS CircularBuffer）：gitgo 当前本地/内网 LLM 部署，连接稳定性高。流中断恢复（第六节）已覆盖瞬态断连。公网部署时再实施。

**Auth 恢复**（OAuth token refresh）：当前无此需求。

### 参考项目

- Claude Code：SSE Last-Event-ID replay, WS CircularBuffer, RECONNECT_GIVE_UP_MS=600000
- Kimi CLI：401→OAuth refresh + client rebuild

---

## 十五、迭代计划中的延期项

以下两项列入迭代计划，不在本次实施：

### 15.1 Rust PyO3 ToolRunner（6 个月后评估）

当前 Python `ProcessToolRunner`（第七节）已实现真进程隔离和超时杀。在以下需求出现时考虑升级到 Rust：
- cgroup 资源限制（CPU/内存配额）
- seccomp 沙箱（限制系统调用）
- 进程级性能监控（精确的 RSS/CPU 时间统计）
- 大规模并发下的进程启动开销成为瓶颈

这是 VERSION.md v0.5 异构计划（diff_engine / git_ops）的同系模块，技术栈一致（Rust PyO3），届时作为异构计划第 3 项实施。

### 15.2 遥测与崩溃持久化（P3）

daemon 崩溃时写入 `.gitgo/crashes/{timestamp}.json`。当前阶段作业量低，暂缓。

---

## 十六、跨子系统兼容性核对

| 子系统 | 关系 | 协调方案 |
|--------|------|---------|
| Knowledge System | 独立 | 无冲突 |
| Context Management | 回滚裁剪时 pending nudge 可能被误删 | §五 rollback 裁剪跳过 `_nudge_state == "pending"` 的消息 |
| Multi-Agent Scheduling | 未启用 | 启用时 ToolExecution 事务边界需跨 slot 协调（届时再设计） |
| Completion Judgment | 回滚可能吃掉 test 失败证据 | §二 Nature=CRASH/BUSINESS 区分——BUSINESS 不回滚 |
| 流式响应 | Dashboard 已渲染的回滚内容 | §五 rollback 发射 `rollback_notification` 事件，Dashboard 加删除线/灰化 |

---

## 十七、完整维度汇总

| # | 维度 | 当前状态 | 本次动作 |
|---|------|---------|---------|
| 1 | 错误分类体系（含 Nature 维度） | **缺失** | **新建** `loop/error_taxonomy.py` |
| 2 | LLM 重试引擎（含 CircuitBreaker 协作） | **缺失** | **实现** LLMProvider 三层分工重试 |
| 3 | 流中断恢复 | 已实现 | 不动 |
| 4 | 工具错误→模型反馈 | 已实现 | 补充错误分类标签 |
| 5 | 事务回滚+会话同步（含 CRASH/BUSINESS 区分） | **空壳** | **实现** 内容备份+SHA256去重+NATURE门控+会话裁剪 |
| 6 | 死循环检测+Storm Break（error_code 触发） | 部分 | **扩展** LoopGuard |
| 7 | 超时管理+进程隔离 | **缺失** | **新建** Python ProcessToolRunner + 三级超时 |
| 8 | 幂等性 | 极弱 | **实现** Execution 级幂等键 |
| 9 | 优雅降级 | **超额达标** | 不动 |
| 10 | 进程崩溃恢复 | 部分 | **补充** 子进程 backoff 重启 |
| 11 | 会话持久化（JSONL） | **极弱** | **实现** append-only JSONL + checkpoint |
| 12 | 资源清理 | 基本完善 | 补 temp/worktree 清理 |
| 13 | 连接/Auth 恢复 | 不适用 | 不实施 |
| 14 | 遥测持久化 | 无 | 不实施（P3） |
| — | Rust PyO3 ToolRunner | — | 迭代计划（6 个月后评估） |

---

## 十八、实施优先级（经评审重排）

| 优先级 | 维度 | 新文件 | 改文件 | 约行数 |
|--------|------|--------|--------|--------|
| **P0** | #1 错误分类体系 | `loop/error_taxonomy.py` | — | 60 |
| **P0** | #5 事务回滚+会话同步 | — | `loop/tool_execution.py`, `loop/executor.py` | 100 |
| **P0** | #6 Storm Break | — | `loop/loop_guard.py` | 30 |
| **P0** | #8 幂等键 | — | `loop/tool_execution.py`, `loop/executor.py` | 30 |
| **P1** | #2 LLM 重试引擎 | — | `loop/llm.py` | 120 |
| **P1** | #11 会话持久化 | — | `loop/manager.py`, `daemon/__init__.py` | 80 |
| **P1** | #10 子进程 backoff | — | `daemon/__init__.py` | 30 |
| **P2** | #7 ProcessToolRunner | `loop/process_tool_runner.py` | `loop/tool_pipeline.py`, `loop/agent_tool.py` | 150+20 |
| **P2** | #4 工具错误分类标签 | — | `loop/tool_pipeline.py` | 15 |
| **P2** | #12 资源清理补漏 | — | `daemon/__init__.py` | 20 |
| **延期** | Rust PyO3 ToolRunner | `tool_runner/` | — | 6 个月后评估 |
| **延期** | 遥测持久化 | — | — | P3 |

**P0 合计**：新文件 1 个，改文件 3 个，新增 ~220 行。纯 bug 修复，零风险。
**全部合计**：新文件 2 个（`error_taxonomy.py` + `process_tool_runner.py`），改文件 6 个，新增 ~655 行。

---

## 十九、自查清单

- [x] Nature 维度（CRASH vs BUSINESS）是否定义了清晰的回滚触发条件？（§二、§五）
- [x] 事务回滚的 CRASH 触发是否和完成判断的 BUSINESS 信号不会互相吃掉？（§十六——BUSINESS 不回滚）
- [x] LLM 重试引擎是否说明了和已有 CircuitBreaker 的分工？（§三——三层分工）
- [x] LLM 重试引擎是否说明了和 Failover 的关系？（§三——CB OPEN→NON_RETRYABLE→Failover 接管）
- [x] 旧重试循环是否要删除？（§三——删除 `_chat_single_provider` 旧循环）
- [x] 会话持久化是否避免了全量 JSON 覆写的 I/O 和原子性问题？（§十二——JSONL + checkpoint）
- [x] 回滚裁剪是否保护 pending nudge？（§五——跳过 `_nudge_state == "pending"`）
- [x] 流式响应中 Dashboard 已渲染内容是否处理？（§五——`rollback_notification` 事件）
- [x] Storm Break 的 error_hash 是否指定为只用 error_code？（§八——`(tool_name, error_code)` 不用哈希）
- [x] 超时工具是否真能杀死？（§七——`subprocess.Popen` + `os.killpg` / `taskkill /F /T`）
- [x] 多 Agent 兼容性是否记录？（§十六——未启用，届时需跨 slot 协调）
- [x] Rust PyO3 是否放入迭代计划？（§十五——6 个月后评估）
- [x] ErrorNature 是否和治理层的 severity/category 边界清晰？（§二）

---

## 二十、验证

```bash
# Python side
pytest tests/ -q                        # 保持 539+ passed

# New module importability
python -c "
from backend.core.loop.error_taxonomy import (
    ErrorSource, ErrorSeverity, Retryability, ErrorNature, ClassifiedError
)
from backend.core.loop.llm import LLMProvider
from backend.core.loop.tool_execution import ToolExecution
from backend.core.loop.loop_guard import LoopGuard
from backend.core.loop.process_tool_runner import ProcessToolRunner
print('All error recovery modules OK')
"
```
