# 报告二：Daemon 守护进程与 Dispatch 调度层深度解析

> gitgo v0.35 | 2026-07-16 | 完全透底技术报告

---

## 概述

Daemon 是 gitgo 的**常驻后台进程**，负责三件事：(1) 监控工作区文件变更，(2) 接收并路由外部命令，(3) 运行 Policy Engine 实时检查。Dispatch 层则在 daemon 内部将命令路由到具体工具执行。

**核心文件**：

| 文件 | 行数 | 职责 |
|------|------|------|
| `daemon/__init__.py` | 845 | Daemon 主循环 + 全部事件处理 |
| `daemon/client.py` | 442 | DaemonClient 子进程通信客户端 |
| `daemon/watcher.py` | 75 | 文件系统监控（watchdog） |
| `daemon/poller.py` | 29 | Trial 仓库轮询 |
| `daemon/commands.py` | 44 | stdin 命令读取 |
| `dispatch/dispatcher.py` | 125 | ToolDispatcher 命令→工具路由 |
| `mcp_tools/daemon_registry.py` | 40 | DaemonClient 单例注册表 |

---

## 一、Daemon 架构：三线程 + 主循环

```
watcher (Thread-1) ──┐
poller  (Thread-2) ──┼── event_queue ──► Main Loop (主线程) ──► stdout (JSON)
reader  (Thread-3) ──┘
```

### 1.1 三线程职责

**Thread-1: WorkspaceWatcher**（基于 watchdog）
- 监控 workspace 目录的所有文件变更
- 去抖（debounce）：变更后等待 `debounce_sec` 秒（默认 2s）静默
- 静默期结束后 → 收集所有变更文件路径 → `evq.put({"event": "workspace_dirty", "files": [...]})`

**Thread-2: TrialPoller**
- 周期性（`trial_interval` 秒）发送 `trial_check` 事件
- 使用 `threading.Event.wait()` 实现可取消 sleep

**Thread-3: CommandReader**
- 从 stdin 读取 line-delimited JSON 命令
- 每行 → `evq.put({"event": "stdin_command", "data": parsed_json})`
- stdin EOF → shutdown 事件

### 1.2 主循环事件类型

主循环的 `evq.get()` 返回以下事件之一：

| 事件类型 | 来源 | 处理方式 |
|----------|------|----------|
| `workspace_dirty` | Watcher | ThreadPoolExecutor 后台扫描 → PolicyEngine |
| `trial_check` | Poller | step_check_trial() → 有变更时 emit |
| `stdin_command` | Reader | _handle_command() 分发 17 种命令 |
| `llm_response` | LLM 后台线程 | 写入 DaemonClient 的响应队列 |
| `agent_complete` | agent_run 后台线程 | 写入 DaemonClient 的响应队列 |
| `shutdown` | Reader (EOF) 或 stdin shutdown 命令 | 清理 + 退出循环 |

---

## 二、Daemon 启动与初始化序列

### 2.1 run_daemon() 启动流程（daemon/__init__.py）

```python
def run_daemon(cfg, project, trial_interval=9999, debounce_sec=2.0):
    # 1. PID 文件获取（防止重复启动）
    if not _acquire_pid_file(project):
        raise RuntimeError("Daemon already running")

    # 2. Session 初始化
    session = SyncSession(project, cfg)
    hash_cache = FileHashCache(session.workspace_path / ".gitgo")

    # 3. 初始扫描
    session.step_scan(hash_cache=hash_cache)
    session.step_load_commits()
    trial_incoming = session.step_check_trial()

    # 4. 创建 Agent Process Manager
    proc_mgr = AgentProcessManager()

    # 5. 注册工具执行器
    tool_executors = {
        "scan": lambda args, p: ...,
        "status": lambda args, p: ...,
        "formalize": lambda args, p: ...,
    }

    # 6. 创建 ToolDispatcher（含 RingGate）
    gate = RingGate()
    history_writer = HistoryManager.add_operation
    dispatcher = ToolDispatcher(gate, tool_executors, history_writer)

    # 7. SignalBus 初始化
    signal_bus = SignalBus.from_contract(str(session.workspace_path))

    # 8. 启动三线程
    watcher = WorkspaceWatcher(event_queue, str(workspace), debounce_sec)
    poller = TrialPoller(event_queue, trial_interval)
    reader = CommandReader(event_queue)
    watcher.start(); poller.start(); reader.start()

    # 9. 后台线程池
    bg_executor = ThreadPoolExecutor(max_workers=2)

    # 10. 主事件循环
    while not _shutdown:
        event = evq.get()
        ...
```

### 2.2 PID 文件管理

```python
_pid_file_path(project) → Path(workspace) / ".gitgo" / "daemon.pid"

_acquire_pid_file(project):
    if pid_path.exists():
        old_pid = int(pid_path.read_text())
        os.kill(old_pid, 0)  # signal 0 = 存在性检查
        → 进程还在 → return False（拒绝启动）
        → 进程不在 → 覆盖（清理旧 PID）

_release_pid_file(project):
    pid_path.unlink(missing_ok=True)
```

---

## 三、workspace_dirty 事件处理链路（完整追踪）

这是 daemon 最核心的工作流程——文件变更 → 策略检查 → 治理信号输出。

```
watchdog 检测到文件变更
  │
  ├─ WorkspaceWatcher.on_any_event()
  │   ├─ 取消旧的 debounce timer
  │   ├─ 收集变更文件路径到 _changed_files
  │   └─ 启动新的 debounce timer (debounce_sec)
  │
  ├─ debounce 计时结束 → on_dirty()
  │   └─ evq.put({"event": "workspace_dirty", "files": [...], "timestamp": ...})
  │
  ├─ 主循环收到 workspace_dirty
  │   └─ bg_executor.submit(_do_workspace_scan, session, project, changed_files, hash_cache, daemon_ctx)
  │
  └─ _do_workspace_scan() 后台执行:
        │
        ├─ 1. Cache 失效
        │   for f in changed_files: hash_cache.invalidate(f)
        │
        ├─ 2. 增量扫描
        │   session.step_scan_files(changed_files, hash_cache=hash_cache)
        │   session.step_load_commits()
        │
        ├─ 3. Fact 推导
        │   derive_facts(project.name)
        │
        ├─ 4. Policy Engine 运行
        │   engine = PolicyEngine()  # 4 个默认检查
        │   results = engine.run(session, project)
        │
        ├─ 5. 结果分发
        │   for lesson_matched: _emit({"event": "lesson_matched", ...})
        │   for contract_drift: _emit({"event": "governance_drift", ...})
        │                         HistoryManager.add_operation("governance_drift", ...)
        │   for identity_integrity: _emit(...) + HistoryManager
        │
        ├─ 6. SignalNormalizer 归一化
        │   normalizer = SignalNormalizer()
        │   signals = normalizer.normalize(results, lessons, rejections, facts)
        │
        ├─ 7. 发射治理信号
        │   _emit({"event": "governance_signals", "signals": [...], ...})
        │
        └─ 8. HistoryManager 记录
            HistoryManager.add_operation("policy_check_result", ...)
```

### 关键设计点

1. **后台线程扫描**：`_do_workspace_scan` 在 ThreadPoolExecutor 中执行，不阻塞主循环。这意味着在扫描期间 daemon 仍可响应 stdin 命令。

2. **HashCache 加速**：先 invalidate 变更文件的缓存，再调用 step_scan_files。已缓存的文件（SHA256 + mtime + size 匹配）直接命中，跳过重新计算。

3. **治理事件 vs 操作事件**：daemon 写入的 HistoryManager 事件（policy_check_result, governance_drift 等）是**治理事件**，与 sync_session 的**操作事件**（scan, formalize, sync, push）是不同的类别。

---

## 四、_handle_command() —— 17 种命令的完整分发

daemon 主循环收到 `stdin_command` 后调用 `_handle_command(cmd, session, project, daemon_ctx, on_shutdown)`。

### 命令分类

#### 生命周期命令
| 命令 | 参数 | 处理 |
|------|------|------|
| `shutdown` | — | 设置 shutdown flag |
| `status` | — | 返回 session 状态 |
| `session` | action | save/restore session |

#### Agent 命令
| 命令 | 参数 | 处理 |
|------|------|------|
| `fork_agent` | role, ring_level, tool_registry, max_steps, context_snapshot, provider_id, model_id | AgentProcessManager.fork() → 返回 process_id |
| `dispatch_tool` | process_id, tool_name, args | ToolDispatcher.dispatch() |
| `llm_configure` | providers[] | 从参数创建 LLMProvider（多 provider 模式）或从 llm_config.json 加载 |
| `llm_call` | messages, process_id | 后台线程：LLMProvider.chat() → emit llm_response |
| `agent_run` | process_id, instruction | 后台线程：agent_step() → emit agent_complete |

#### 扫描/同步命令
| 命令 | 参数 | 处理 |
|------|------|------|
| `scan` | — | step_scan() + step_load_commits() |
| `formalize` | indices, message, template | step_create_formal_commit() |
| `sync` | — | step_sync() |
| `push` | skip_security, strip_authorship, aggressive | step_push() |

#### 治理命令
| 命令 | 参数 | 处理 |
|------|------|------|
| `round_complete` | project | _snapshot_workspace() + 返回结果 |
| `reject` | reason, instruction | HistoryManager 写入 rejection + 检查是否有 ≥3 次连续 rejection → _harvest_from_rejection_chain() |
| `trial` | action, index | list/accept/promote/discard |
| `cache_stats` | — | FileHashCache.stats() |
| `loop_status` | — | 返回进程树状态 + 断路器状态 |

### round_complete 流程详解

```
stdin: {"cmd": "round_complete", "project": "gitgo"}
  │
  ├─ _snapshot_workspace(session, project)
  │   ├─ changed = [e.rel_path for e in session.entries if e.status != "same"]
  │   ├─ git add -A
  │   ├─ git commit -m "gitgo: round snapshot [HH:MM:SS]\n变更文件: N\n  file1\n  file2\n..."
  │   ├─ HistoryManager.add_operation("workspace_state_snapshot", ...)
  │   └─ _emit({"event": "workspace_snapshot", "files": len(changed)})
  │
  └─ 返回 snapshot 结果给 DaemonClient
```

### rejection 流程详解

```
stdin: {"cmd": "reject", "reason": "编码风格不符合项目规范", "instruction": "使用 snake_case"}

  ├─ HistoryManager.add_operation("rejection", ...)
  │
  ├─ 查询最近 3 条 rejection 记录
  │   └─ 有 ≥3 条连续 rejection？
  │       └─ _harvest_from_rejection_chain(project_name, rejections, session)
  │           ├─ 合并 3 条 rejection 的 reason
  │           ├─ Lesson(trigger="连续3次被人否定: ...", rule=final_instruction)
  │           ├─ LessonManager.save_pending(ws, lesson)
  │           ├─ HistoryManager.add_operation("governance_lesson", ...)
  │           └─ _emit({"event": "lesson_harvested", ...})
```

---

## 五、DaemonClient 通信协议（client.py 442 行）

### 5.1 子进程生命周期

```python
client = DaemonClient("myproject")
client.start()           # 启动 daemon 子进程
# ... 通信 ...
client.stop()            # 发送 shutdown，等待最多 5s，超时 kill
```

**start() 流程**：
1. `_kill_existing()`：通过 PID 文件查找已有进程 → kill
2. 子进程命令：`python -m gitgo --mode daemon --project X --daemon-action start`
3. CWD = `project_root.parent`（解决模块发现）
4. 创建 `_reader_thread`（daemon=True）+ `_stderr_thread`（daemon=True）
5. 等待 `_started_event`（daemon 输出 `{"event": "daemon_started"}` 时 set）
6. 超时 → stop() + raise RuntimeError

**stop() 流程**：
1. 发 shutdown 命令
2. `process.wait(timeout=5.0)`
3. 超时 → `process.kill()`
4. `_wake_all_waiters()`：唤醒所有等待响应的线程

### 5.2 同步命令协议

```python
def send_command(self, cmd, timeout=30.0):
    request_id = str(uuid.uuid4())
    cmd["request_id"] = request_id

    event = threading.Event()
    self._cmd_events[request_id] = event

    self._write_cmd(cmd)

    if not event.wait(timeout=timeout):
        raise RuntimeError(f"Command '{cmd.get('cmd')}' timed out after {timeout}s")

    result = self._cmd_results.pop(request_id, {})
    self._cmd_events.pop(request_id, None)

    if result.get("status") == "error":
        raise RuntimeError(result.get("error", "Unknown error"))
    return result.get("result", result)
```

**_read_stdout 后台线程**持续读取 daemon 的 stdout：
```python
def _read_stdout(self):
    for line in self._process.stdout:
        data = json.loads(line)

        if data.get("event") == "daemon_started":
            self._started_event.set()

        elif data.get("event") == "command_result":
            request_id = data.get("request_id")
            self._cmd_results[request_id] = data
            self._cmd_events[request_id].set()

        elif data.get("event") == "llm_response":
            process_id = data.get("process_id")
            self._llm_data[process_id] = data
            self._llm_events[process_id].set()

        elif data.get("event") == "agent_complete":
            process_id = data.get("process_id")
            self._agent_data[process_id] = data
            self._agent_events[process_id].set()
```

### 5.3 异步命令协议

`send_llm_call()` 和 `send_agent_run()` 是**异步**的——它们发送命令后立即返回一个 Event，调用方可以阻塞等待或轮询：

```python
def send_agent_run(self, process_id, instruction, timeout=300.0):
    event = threading.Event()
    self._agent_events[process_id] = event

    self._write_cmd({"cmd": "agent_run", "process_id": process_id,
                     "instruction": instruction})

    if not event.wait(timeout=timeout):
        raise RuntimeError(f"agent_run for {process_id} timed out")
    return self._agent_data.pop(process_id, {})
```

异步命令的实现方式：daemon 收到命令后，用 `ThreadPoolExecutor.submit()` 在后台线程执行 LLM 调用或 agent_step，完成后通过 `_emit()` 输出结果。

### 5.4 重连机制

```python
IDEMPOTENT_COMMANDS = {
    "status", "loop_status", "cache_stats", "scan",
    "llm_configure", "llm_call", "agent_run", "fork_agent",
}

def send_command(self, cmd, timeout=30.0):
    for attempt in range(self.MAX_RECONNECT_ATTEMPTS + 1):  # 最多 6 次
        try:
            return self._send_command_once(cmd, timeout)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            if not idempotent or attempt >= self.MAX_RECONNECT_ATTEMPTS:
                raise
            backoff = min(2 ** attempt, self.RECONNECT_BACKOFF_CAP)
            time.sleep(backoff)
            self.start()  # 重连
```

**仅幂等命令可自动重连**：非幂等命令（如 sync、push、formalize）如果子进程崩溃，直接抛异常——因为操作可能已部分执行。

### 5.5 并发安全

- `threading.Lock`（`_lock`）：保护 `_process`, `_running` 的状态变更
- `threading.Event`：每个待处理请求一个 Event，`_read_stdout` 线程通过匹配 `request_id` 或 `process_id` 来 set 对应的 Event
- `_wake_all_waiters()`：stop() 时唤醒所有等待者，防止死锁

---

## 六、ToolDispatcher 命令路由（dispatch/dispatcher.py 125 行）

### 6.1 dispatch() 五步执行

```python
class ToolDispatcher:
    def __init__(self, gate, tool_executors, history_writer):
        self._gate = gate           # RingGate 实例
        self._executors = tool_executors  # {"scan": func, "status": func, ...}
        self._history = history_writer    # HistoryManager.add_operation

    def dispatch(self, process, tool_name, args):
        # 1. RingGate 权限检查
        gate_result = self._gate.check(process, tool_name)
        if not gate_result.allowed:
            return ToolResult(allowed=False, error=gate_result.reason)

        # 2. worktree 路径注入
        if process.worktree_path:
            args["_worktree"] = process.worktree_path

        # 3. 工具查找
        executor = self._executors.get(tool_name)
        if executor is None:
            return ToolResult(allowed=True, success=False, error=f"Unknown tool: {tool_name}")

        # 4. 线程池执行（30s 超时）
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(executor, args, process)
                data = future.result(timeout=30)
            return ToolResult(allowed=True, success=True, data=data,
                            duration_ms=...)
        except TimeoutError:
            return ToolResult(allowed=True, success=False, error="TOOL_TIMEOUT")

        # 5. 步骤计数 + 超步数 KILL + HistoryManager 记录
        process.steps_used += 1
        if process.steps_used >= process.max_steps:
            process.status = ProcessStatus.KILLED
        self._history(...)
```

### 6.2 工具执行器注册

daemon 在启动时注册 3 个内置工具执行器：

```python
tool_executors = {
    "scan": lambda args, p: session.step_scan(hash_cache=hash_cache),
    "status": lambda args, p: session.status_dict(),
    "formalize": lambda args, p: session.step_create_formal_commit(
        args.get("indices", []), args.get("message", ""), args.get("template", "default")
    ),
}
```

### 6.3 worktree 路径注入

`args["_worktree"] = process.worktree_path` 是**隐式参数注入**——B Agent 调用工具时，自动使用它的独立 worktree 路径，而不是主 workspace。实现文件系统隔离。

---

## 七、WorkspaceWatcher 文件监控（watcher.py 75 行）

```python
class WorkspaceWatcher(FileSystemEventHandler):
    def __init__(self, event_queue, workspace_path, debounce_sec=2.0):
        self._evq = event_queue
        self._debounce = debounce_sec
        self._timer: threading.Timer | None = None
        self._changed_files: set[str] = set()
        self._observer = Observer()

    def start(self):
        self._observer.schedule(self, self._workspace_path, recursive=True)
        self._observer.start()

    def on_any_event(self, event):
        if event.is_directory:
            return
        if '.git/' in event.src_path:
            return  # 忽略 .git 目录变更

        self._changed_files.add(event.src_path)

        # 重置去抖计时器
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce, self._on_dirty)
        self._timer.start()

    def _on_dirty(self):
        files = list(self._changed_files)
        self._changed_files.clear()
        self._evq.put({"event": "workspace_dirty", "files": files,
                       "timestamp": datetime.now().isoformat()})
```

**关键设计**：
- 每次文件变更都重置 debounce timer——连续修改时只在最后一次修改后等待 debounce_sec 秒
- 中间变更的文件全部积累到 `_changed_files` 集合中
- `.git/` 目录变更被忽略（避免 git 操作触发自循环）
- `threading.Timer` 而非 `time.sleep`——不阻塞 watchdog 线程

---

## 八、DaemonRegistry 单例管理

```python
# mcp_tools/daemon_registry.py
_clients: dict[str, DaemonClient] = {}

def get_client(project_name):
    if project_name not in _clients:
        client = DaemonClient(project_name)
        client.start()
        _clients[project_name] = client
    return _clients[project_name]

def shutdown_all():
    for name, client in list(_clients.items()):
        try:
            client.stop()
        except Exception:
            pass
    _clients.clear()

# atexit 注册
atexit.register(shutdown_all)
```

每个项目最多一个 DaemonClient 实例。MCP 工具调用 `get_client()` 时按需创建和启动。

---

## 九、Module 间数据流

```
MCP Tool / CLI
    │
    ▼
mcp_tools/loop.py: get_client(project)
    │
    ▼
DaemonClient ──stdin JSON──→ CommandReader (Thread-3)
    │                            │
    │ stdout JSON                ▼
    │                        evq.put("stdin_command")
    │                            │
    │                            ▼
    │                    主循环: _handle_command()
    │                            │
    │              ┌─────────────┼──────────────┐
    │              ▼             ▼              ▼
    │         fork_agent    agent_run      round_complete
    │              │             │              │
    │    AgentProcessMgr   ThreadPool      _snapshot_
    │         .fork()       .submit(       workspace()
    │              │        agent_step)        │
    │              │             │              │
    │              ▼             ▼              ▼
    │         AgentProcess   agent_step()   git commit
    │              │        (Report 1)      + History
    │              │
    └── _read_stdout() 线程匹配响应 ←── _emit() JSON
```

---

## 十、测试覆盖

| 测试文件 | 测试内容 |
|----------|----------|
| `test_loop/test_cache.py` | FileHashCache 存储/命中/过期/淘汰/持久化（daemon 使用的缓存层） |
| `test_regression.py` | daemon 相关回归测试 |
| `test_config.py` | ConfigManager 的配置加载 |

**测试策略说明**：
- DaemonClient 的测试需要真实子进程，当前主要通过集成测试覆盖
- Watchdog/Poller/Reader 三个线程组件通过 daemon 实际运行验证
- ToolDispatcher 的 RingGate 集成在 `test_loop/test_gate.py` 中测试

---

## 十一、已知限制与潜在问题

1. **后台扫描可能阻塞**：`_do_workspace_scan` 中的 `step_scan` 和 `PolicyEngine.run()` 在大项目上可能耗时很长。虽然它在后台线程执行，但连续快速的 `workspace_dirty` 事件可能导致多个后台扫描并发（`bg_executor` 只有 2 个 worker）。

2. **DaemonClient 重连的 CWD 假设**：`self._project_root = Path(__file__).resolve().parent.parent.parent.parent` 是硬编码的 4 层向上——如果 daemon 模块被移动到不同深度，会失效。

3. **PID 文件跨平台**：`os.kill(pid, 0)` 在 Windows 上不可用（仅 Unix）。代码没有 Windows 备选方案（如 `psutil.pid_exists()`）。

4. **rejection 收割条件**：`_harvest_from_rejection_chain` 在 >=3 条连续 rejection 时触发，但不检查 rejection 的**时间跨度**——3 条 rejection 可能跨越数天，它们是否仍然"连续"是有争议的。

5. **ToolDispatcher 的 ThreadPoolExecutor(max_workers=1)**：每个工具调用创建一个新的线程池（含 1 个 worker），用完即弃。在高频工具调用场景下，创建/销毁线程池的开销可能显著。

6. **_emit() 无缓冲**：`print(..., flush=True)` 每次都 flush，高频事件场景下可能成为 I/O 瓶颈。

---

## 十二、设计审查总结

### ✅ 已实现

| 设计要求 | 实现状态 |
|----------|----------|
| 常驻后台进程 + 文件监控 | ✅ 三线程架构 + watchdog |
| 去抖机制 | ✅ debounce timer（可配置时长） |
| stdin/stdout JSON 通信 | ✅ line-delimited JSON 协议 |
| 同步 + 异步命令 | ✅ request_id 路由 + ThreadPoolExecutor |
| 断线重连 | ✅ 幂等命令自动重连（指数退避） |
| PID 文件防重复 | ✅ 进程存在性检查 |
| Policy Engine 实时检查 | ✅ workspace_dirty → 后台扫描 → PolicyEngine |
| Rejection 知识收割 | ✅ ≥3 次连续 rejection → harvest |

### ⚠️ 部分实现

| 设计意图 | 当前状态 |
|----------|----------|
| PID 检查的 Windows 兼容 | `os.kill(pid, 0)` 在 Windows 不可用 |
| 后台扫描并发控制 | bg_executor 仅 2 worker，连续 dirty 可能排队 |
| CWD 计算 | 硬编码 4 层向上 |

### ❌ 未实现

| 设计要求 | 说明 |
|----------|------|
| Daemon 健康检查端点 | 无 HTTP health check，只能通过 DaemonClient.is_running() |
| Daemon 日志文件 | 所有输出走 stdout，无持久化日志 |

---

## v0.34-v0.35 更新补遗

**v0.34**: daemon 新增 task 命令 (chat/fork/status/kill), 17→21 种命令。

**v0.35**: workspace_dirty 后自动信号捕获 (capture_signal)。harvest 触发调度。独立 pending digest 定时器。
