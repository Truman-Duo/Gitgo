# Gitgo Phase 2: Agent-Ready Runtime

> 设计日期：2026-05-13 | 基于 v0.12 源码审计 | 已综合审阅建议

---

## Phase 2 的定位

Phase 1 完成了"把 runtime 暴露给 agent"——agent 可以通过 `gitgo status --json` 理解项目状态，
通过 9 个 CLI verb 执行操作。但每次操作都是一次性的进程调用——agent 每做一件事就要启动一个 Python 进程，
初始化 ConfigManager、SyncSession、适配器，执行一个 step，输出结果，进程退出。

Phase 2 的目标是让 Gitgo 从 **CLI tool** 变成 **persistent runtime**——agent 连接到一个持续运行的
进程，读取实时状态，接收变更事件，发起操作，获得流式进度反馈。

一句话：**从 "agent 调用 Gitgo" 到 "agent 连接到 Gitgo"。**

---

## 当前基线（v0.12 源码审计结果）

| 问题 | 位置 | 影响 |
|------|------|------|
| `status_dict()` 是原始数据 | `sync_session.py:140-165` — 只有计数，无语义判断 | agent 需要自己推断"下一步该做什么" |
| 无流式输出 | 所有 `on_progress` 回调仅用于 GUI 进度条 / CLI print | agent 看不到 scan/sync 的中间进度 |
| `HistoryManager` 只记 sync | `history.py:60-81` — `action_type` 字段存在但从未写入 | 无法查询 scan/formalize/triage 历史 |
| Daemon 是一次性执行 | `run_full_workflow()` (`sync_session.py:761-802`) — 执行完即退出 | 无法作为后台服务运行 |
| 无文件监控 | workspace 变化不会触发任何逻辑 | agent 必须主动轮询 `gitgo scan` |
| 无 trial 定时轮询 | trial 新 commit 需要主动 `gitgo trial list` | agent 必须定时 cron 触发 |
| `save_session()` 不持久化 `is_incoming` / `sources_cleared` | `sync_session.py:822-838` — formal_commits 序列化遗漏字段 | 会话恢复后 incoming 标记丢失 |

---

## 阶段结构

| Stage | 名称 | 核心产出 | 预估 |
|-------|------|---------|------|
| P2-A | Semantic State + Streaming | `status_dict()` 增加 semantic 块 + `--stream` 流式输出 | 1 周 |
| P2-B | Unified Operation History | HistoryManager 覆盖全操作类型 | 1-2 周 |
| P2-C | Persistent Daemon Core | watchdog + 定时轮询 + 事件循环（纯线程架构） | 2-3 周 |
| P2-D | Agent Interface | 守护进程 JSON 协议 + MCP Server 包装器（可选） | 1-2 周 |

---

## P2-A: Semantic State + Streaming

### 现状

`status_dict()` (`sync_session.py:140-165`) 输出原始计数：

```json
{
  "workspace": {"entries_total": 45, "entries_changed": 3},
  "commits": {"workspace_total": 5, "formal_total": 2, "formal_synced": 1, "formal_pushed": 0},
  "trial": {"configured": true, "pending": 3, "total": 5}
}
```

Agent 拿到这个数据后需要自己推断"下一步该做什么"——这些推断逻辑在每个 agent 里重复实现。

### 目标

在 `status_dict()` 中新增 `semantic` 子块，提供 agent 可直接消费的判断：

```json
{
  "workspace": {...},
  "commits": {...},
  "trial": {...},
  "semantic": {
    "workspace_entropy": "medium",
    "trial_requires_review": true,
    "safe_to_formalize": true,
    "safe_to_publish": false,
    "blocked_reason": "unsynced_formal_commits",
    "suggested_next_action": "triage",
    "action_queue": ["triage", "formalize", "sync", "push"]
  }
}
```

### 字段定义

| 字段 | 类型 | 计算逻辑 |
|------|------|---------|
| `workspace_entropy` | `"low" \| "medium" \| "high"` | entries_changed=0 → low, 1-10 → medium, >10 → high |
| `trial_requires_review` | `bool` | trial.pending > 0 |
| `safe_to_formalize` | `bool` | entries_changed > 0 且 stage 非 busy |
| `safe_to_publish` | `bool` | formal_synced > 0 且 formal_synced > formal_pushed |
| `blocked_reason` | `string \| null` | 阻塞原因枚举（见下） |
| `suggested_next_action` | `string` | 优先级: triage > formalize > sync > push > idle |
| `action_queue` | `list[string]` | 所有待执行操作的有序列表 |

`blocked_reason` 枚举（按优先级）：

| 值 | 条件 |
|------|------|
| `"trial_pending_first"` | trial.pending > 0 且用户在操作 workspace（governance 约束：外部输入优先） |
| `"no_backup_configured"` | backup_path 未设置或不存在 |
| `"unsynced_formal_commits"` | formal_synced > formal_pushed 且 entries_changed > 0（需先 sync 再 scan） |
| `"backup_not_git_repo"` | backup_path 存在但非 git 仓库 |

`suggested_next_action` 的优先级逻辑（反映 governance 状态机约束）：

```
if trial.pending > 0                              → "triage"
elif entries_changed > 0 and no unsynced blocking → "formalize"
elif formal_synced > formal_pushed                → "push"
else                                              → "idle"
```

### 实现

```python
# core/sync_session.py — status_dict() 扩展

def status_dict(self, semantic: bool = True) -> dict:
    """返回机器可读的当前项目状态。
    
    semantic=True 时附加 semantic 子块（agent 可直接消费的判断）。
    semantic=False 时仅输出原始计数（Phase 1 兼容）。
    """
    from models import TrialAction
    trial_pending = sum(1 for c in self.incoming_changes
                        if c.triage == TrialAction.PENDING)
    entries_changed = sum(1 for e in self.entries
                          if e.status != "same" and e.selected)
    formal_total = len(self.formal_commits)
    formal_synced = sum(1 for fc in self.formal_commits if fc.synced)
    formal_pushed = sum(1 for fc in self.formal_commits if fc.pushed)

    result = {
        "project": self.project.name,
        "stage": self.stage.name,
        "workspace": {
            "path": str(self.workspace_path),
            "entries_total": len(self.entries),
            "entries_changed": entries_changed,
        },
        "commits": {
            "workspace_total": len(self.commits),
            "formal_total": formal_total,
            "formal_synced": formal_synced,
            "formal_pushed": formal_pushed,
        },
        "trial": {
            "configured": (self.project.trial is not None
                           and bool(self.project.trial.file_access.path)),
            "pending": trial_pending,
            "total": len(self.incoming_changes),
        },
    }

    if semantic:
        result["semantic"] = self._build_semantic_layer(
            trial_pending, entries_changed, formal_total,
            formal_synced, formal_pushed,
        )

    return result

def _build_semantic_layer(self, trial_pending, entries_changed,
                           formal_total, formal_synced, formal_pushed) -> dict:
    """从原始计数计算 agent 可消费的语义判断。"""
    if entries_changed == 0:
        entropy = "low"
    elif entries_changed <= 10:
        entropy = "medium"
    else:
        entropy = "high"

    action_queue = []
    if trial_pending > 0:
        action_queue.append("triage")
    if entries_changed > 0:
        action_queue.append("formalize")
    if formal_synced > formal_pushed:
        action_queue.append("push")

    suggested = action_queue[0] if action_queue else "idle"

    # 阻塞判断
    blocked_reason = None
    if formal_synced > formal_pushed and entries_changed > 0:
        blocked_reason = "unsynced_formal_commits"
    elif not self.backup_path or not self.backup_path.exists():
        blocked_reason = "no_backup_configured"

    return {
        "workspace_entropy": entropy,
        "trial_requires_review": trial_pending > 0,
        "safe_to_formalize": entries_changed > 0 and self.stage == SessionStage.IDLE,
        "safe_to_publish": formal_synced > 0 and formal_synced > formal_pushed,
        "blocked_reason": blocked_reason,
        "suggested_next_action": suggested,
        "action_queue": action_queue,
    }
```

### CLI

```bash
gitgo status --project X --json           # 含 semantic 块（默认）
gitgo status --project X --json --raw     # 不含 semantic，仅原始计数（Phase 1 兼容）
gitgo status --project X --semantic-only  # 仅输出 semantic 块
```

`__main__.py` 需新增 `--raw` / `--semantic-only` flag。

### 流式进度输出（P2-A 同步实现）

`--stream` 是独立于守护进程的低风险增强，在 P2-A 同步实现。将 `on_progress` 回调在 CLI 层包装为
line-delimited JSON 输出：

```bash
gitgo scan --project X --json --stream
```

输出：
```json
{"event": "progress", "op": "scan", "current": 1, "total": 45, "message": "src/main.py"}
{"event": "progress", "op": "scan", "current": 2, "total": 45, "message": "src/utils.py"}
...
{"event": "complete", "op": "scan", "result": {"entries": [...]}}
```

实现方式：在 `_cmd_scan` / `_cmd_sync` / `_cmd_push` 中，当 `--stream` flag 存在时，
将 `session.on_progress` 设为 line-delimited JSON writer，替代默认的 print 回调。

支持 `--stream` 的 verb：`scan`、`sync`、`push`、`daemon`（run_full_workflow）。

### 附带修复

`save_session()` (`sync_session.py:822-838`) 当前遗漏 `is_incoming` 和 `sources_cleared` 字段。
在 P2-A 中一并修复，确保 `session.json` 持久化所有 `FormalCommit` 字段。

### P2-A 认证标准

- [ ] `status_dict(semantic=True)` 输出含 `semantic` 块
- [ ] `status_dict(semantic=False)` 输出与 Phase 1 完全兼容（不加 semantic 块）
- [ ] `workspace_entropy` 在 0 / 1-10 / >10 三种情况下值正确
- [ ] `suggested_next_action` 优先级: triage > formalize > push > idle
- [ ] `blocked_reason` 枚举覆盖上述 4 种情况
- [ ] `--semantic-only` 输出纯 semantic JSON
- [ ] `--raw` 输出与 Phase 1 兼容
- [ ] `--stream` 每行输出合法 JSON
- [ ] `gitgo scan --project X --json --stream` 流式输出进度
- [ ] `save_session()` 持久化 `is_incoming` / `sources_cleared`

---

## P2-B: Unified Operation History

### 现状

`HistoryManager` (`history.py:27-81`) 只记录 sync 操作。`action_type` 字段已在 `HistoryEntry` 中定义
但从未被填充（始终为空字符串）。

`SyncSession._last_op` 在 formalize/sync/triage/push/delete/dissolve 时被设置，
但仅用于 `save_session()` 写入 session.json 的 `last_operation` 字段——不进入 HistoryManager。

### 目标

所有 governance 级操作统一记录到 HistoryManager，agent 可查询完整的操作历史。

### 操作类型范围

**核心操作（必须记录）：**

| 操作类型 | 触发位置 | 记录内容 |
|---------|---------|---------|
| `scan` | `step_scan()` | 文件总数、变更数 |
| `formalize` | `step_create_formal_commit()` | commit tag、source_indices |
| `sync` | `step_sync()` | commit tag、文件数、commit hash |
| `push` | `step_push()` | commit tag |
| `triage_accept` | `step_triage_incoming(action="accept")` | trial hash、消息摘要 |
| `triage_promote` | `step_triage_incoming(action="promote")` | 同上 |
| `triage_discard` | `step_triage_incoming(action="discard")` | 同上 |

**v0.12 破坏性操作（建议记录）：**

| 操作类型 | 触发位置 | 理由 |
|---------|---------|------|
| `delete_formal` | `step_delete_formal()` | 不可逆删除 |
| `dissolve_formal` | `step_dissolve_formal()` | 恢复 workspace commits |

非破坏性操作（edit_message / edit_number / clear_sources）不记录——过多噪音，降低历史可读性。

### 改动范围

**1. 重构 `HistoryEntry`**

```python
# history.py — 统一字段设计
@dataclass
class HistoryEntry:
    timestamp: str               # ISO format
    project_name: str
    operation: str = ""          # "scan" | "formalize" | "sync" | "push"
                                  # | "triage_accept" | "triage_promote" | "triage_discard"
                                  # | "delete_formal" | "dissolve_formal"
    status: str = "success"       # "success" | "failed" | "cancelled"
    detail: dict = field(default_factory=dict)  # 操作特定数据
    # 保留旧字段向后兼容（废弃但保留，避免破坏现有调用）
    file_count: int = 0
    commit_hash: str = ""
    commit_message: str = ""
    workspace: str = ""
    backup: str = ""
```

**2. 新增 `HistoryManager.add_operation()`**

```python
# history.py — 新增方法
@classmethod
def add_operation(cls, project_name: str, operation: str,
                  status: str = "success", detail: dict | None = None):
    entries = cls.load()
    entries.append(HistoryEntry(
        timestamp=datetime.now().isoformat(),
        project_name=project_name,
        operation=operation,
        status=status,
        detail=detail or {},
    ))
    if len(entries) > 200:
        entries = entries[-200:]
    cls.save(entries)
```

**3. `add_entry()` 委托到 `add_operation()` 保持向后兼容**

```python
# history.py — 旧 API 变为新 API 的适配器
@classmethod
def add_entry(cls, project_name: str, file_count: int, commit_hash: str,
              commit_message: str, workspace: str, backup: str) -> None:
    cls.add_operation(project_name, "sync", "success", {
        "file_count": file_count,
        "commit_hash": commit_hash,
        "commit_message": commit_message.split("\n")[0][:80],
        "workspace": workspace,
        "backup": backup,
    })
```

现有调用点（`step_sync()` 和 `_cmd_sync()`）无需修改。

**4. 在 `SyncSession.step_*()` 中调用**

```python
# step_scan 成功后
HistoryManager.add_operation(
    self.project.name, "scan", "success",
    {"entries_total": len(self.entries),
     "entries_changed": sum(1 for e in self.entries if e.status != "same")},
)

# step_create_formal_commit 成功后
HistoryManager.add_operation(
    self.project.name, "formalize", "success",
    {"commit": f"[{fc.prefix}-{fc.number}]",
     "source_indices": list(fc.source_indices)},
)

# step_sync 成功后 — 已有 add_entry() 调用，保持不变

# step_push 成功后
HistoryManager.add_operation(
    self.project.name, "push", "success",
    {"commit": f"[{target.prefix}-{target.number}]"},
)

# step_triage_incoming 成功后
HistoryManager.add_operation(
    self.project.name, f"triage_{action}", "success",
    {"trial_hash": change.hash,
     "trial_message": change.message.split('\n')[0][:80]},
)

# step_delete_formal / step_dissolve_formal 成功后
HistoryManager.add_operation(
    self.project.name, op, "success",
    {"commit": f"[{fc.prefix}-{fc.number}]"},
)
```

**5. CLI 更新**

```bash
gitgo history --project X --json          # 全部操作历史（JSON 数组）
gitgo history --project X --op formalize  # 仅 formalize 操作
gitgo history --project X --limit 20      # 最近 20 条
```

`__main__.py:110-119` 当前 `--mode history` 仅做纯文本列表输出，需扩展支持 `--json`、`--project`、
`--op` 过滤和 `--limit`。

### P2-B 认证标准

- [ ] 全 9 种操作类型全部记录（7 核心 + 2 破坏性）
- [ ] `gitgo history --project X --json` 输出合法 JSON 数组
- [ ] `--op` 过滤器正确筛选操作类型
- [ ] `--limit N` 正确限制返回数量
- [ ] `add_entry()` 旧 API 向后兼容（内部委托到 `add_operation()`）
- [ ] `HistoryEntry.action_type` 废弃后仍然可读取旧数据

---

## P2-C: Persistent Daemon Core

### 现状

`run_full_workflow()` (`sync_session.py:761-802`) 是一次性执行——启动 → scan → formalize → sync → push → 退出。
没有事件循环，没有文件监控，没有定时轮询。

### 目标

```
gitgo daemon start --project X
```

启动一个长期运行的进程，持续监控 workspace 文件变化和 trial 新 commit。
输出 line-delimited JSON 事件到 stdout。

### 架构决策：纯线程 vs asyncio

**选择纯线程架构，不使用 asyncio。**

理由：
- Gitgo 已使用 `QThread` 作为工作线程——线程是已知模式
- Windows 上 asyncio 对 stdin 缺少 `add_reader` 支持，需要 `run_in_executor` 变通
- 守护进程不是高并发服务器——线程数固定（watcher + poller + stdin reader），无调度优势
- `watchdog.Observer` 内部已经是线程化的，强行桥接到 asyncio 引入不必要的复杂度

### 架构

```
┌──────────────────────────────────────────────┐
│               gitgo daemon                    │
│                                               │
│  ┌───────────┐  ┌───────────┐  ┌──────────┐  │
│  │ File      │  │ Trial     │  │ Command  │  │
│  │ Watcher   │  │ Poller    │  │ Reader   │  │
│  │(watchdog) │  │(Timer)    │  │(stdin)   │  │
│  │ Thread-1  │  │ Thread-2  │  │ Thread-3 │  │
│  └─────┬─────┘  └─────┬─────┘  └────┬─────┘  │
│        │              │              │         │
│        └──────────────┼──────────────┘         │
│                       │                        │
│                ┌──────▼──────┐                 │
│                │  Event Queue│                 │
│                │ (queue.Queue)                 │
│                └──────┬──────┘                 │
│                       │                        │
│                ┌──────▼──────┐                 │
│                │  Main Loop  │                 │
│                │  (主线程)    │                 │
│                │  SyncSession│                 │
│                └──────┬──────┘                 │
│                       │                        │
│                ┌──────▼──────┐                 │
│                │ JSON Writer │                 │
│                │  (stdout)   │                 │
│                └─────────────┘                 │
└──────────────────────────────────────────────┘
```

三个后台线程各自通过 `queue.Queue` 向主线程投递事件。主线程阻塞在 `queue.get()` 上，
收到事件后调用对应的 session 方法，输出 JSON 到 stdout。

### 三大组件

#### File Watcher（线程 1）

```python
# core/daemon/watcher.py
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class WorkspaceWatcher(FileSystemEventHandler):
    """监控 workspace 文件变化，去抖后投递 on_dirty 事件。"""

    def __init__(self, workspace_path, exclude_patterns,
                 on_dirty: callable, debounce_sec: float = 2.0):
        self._exclude = exclude_patterns
        self._on_dirty = on_dirty
        self._debounce = debounce_sec
        self._timer: threading.Timer | None = None
        self._observer = Observer()
        self._observer.schedule(self, str(workspace_path), recursive=True)

    def on_any_event(self, event):
        if event.is_directory:
            return
        if self._is_excluded(event.src_path):
            return
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce, self._fire)
        self._timer.start()

    def _fire(self):
        self._on_dirty()

    def start(self):
        self._observer.start()

    def stop(self):
        if self._timer:
            self._timer.cancel()
        self._observer.stop()
        self._observer.join()
```

#### Trial Poller（线程 2）

```python
# core/daemon/poller.py
import threading

class TrialPoller:
    """定时轮询 trial 仓库，发现新 commit 时投递事件。"""

    def __init__(self, event_queue, interval_sec: float = 300.0):
        self._queue = event_queue
        self._interval = interval_sec
        self._stopped = threading.Event()

    def run(self):
        while not self._stopped.wait(self._interval):
            self._queue.put({"event": "trial_check"})

    def stop(self):
        self._stopped.set()
```

#### Command Reader（线程 3）

```python
# core/daemon/commands.py
import sys
import json
import threading

class CommandReader:
    """从 stdin 逐行读取 JSON 命令，投递到事件队列。"""

    def __init__(self, event_queue):
        self._queue = event_queue
        self._stopped = threading.Event()

    def run(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
                self._queue.put({"event": "stdin_command", "cmd": cmd})
                if cmd.get("cmd") == "shutdown":
                    self._stopped.set()
                    return
            except json.JSONDecodeError:
                self._queue.put({
                    "event": "error",
                    "message": f"Invalid JSON: {line[:80]}",
                })

    def stop(self):
        self._stopped.set()
```

### 会话生命周期

守护进程持有一个持久化的 `SyncSession` 作为状态持有者。每次触发时：

1. **workspace_dirty →** 主线程调用 `session.step_scan()` 重新扫描 → 刷新 `entries`。提交由 agent 决定（不自动 formalize）。
2. **trial_check →** 主线程调用 `session.step_check_trial()` → 刷新 `incoming_changes`。
3. **stdin_command →** 主线程执行对应 step 方法。

`entries`、`commits`、`incoming_changes` 在每次触发时刷新——它们是 transient 数据，会话本身
只持有 `formal_commits`（持久化状态）和配置引用。

### stdin 命令协议

每行一个 JSON，与 Phase 1 CLI mode 一一对应：

```json
{"cmd": "status"}
{"cmd": "scan"}
{"cmd": "formalize", "indices": [0, 2]}
{"cmd": "sync"}
{"cmd": "push"}
{"cmd": "trial", "action": "list"}
{"cmd": "trial", "action": "accept", "index": 0}
{"cmd": "session", "action": "save"}
{"cmd": "shutdown"}
```

### 事件输出格式（line-delimited JSON）

```json
{"event": "daemon_started", "project": "MyProject", "timestamp": "..."}
{"event": "state_changed", "stage": "IDLE", "status": {...}}
{"event": "workspace_dirty", "project": "MyProject", "timestamp": "..."}
{"event": "trial_new_commits", "project": "MyProject", "count": 3, "timestamp": "..."}
{"event": "operation_started", "op": "scan", "timestamp": "..."}
{"event": "progress", "op": "scan", "current": 15, "total": 45, "message": "..."}
{"event": "operation_complete", "op": "scan", "status": "success", "result": {...}}
{"event": "daemon_stopped", "project": "MyProject", "timestamp": "..."}
```

### Daemon 生命周期 CLI

```bash
gitgo daemon start --project X [--trial-interval 300] [--debounce 2.0]
    # 前台模式 — stdout 输出事件流

gitgo daemon start --project X --background
    # 后台启动 — 写 PID 文件到 .gitgo/daemon.pid
    # 启动前检查 stale PID（os.kill(pid, 0)）并清理

gitgo daemon stop --project X
    # 发送 shutdown 命令或 SIGTERM

gitgo daemon status --project X --json
    # {"running": true, "pid": 12345, "uptime_sec": 3600}
```

### PID 文件与双启动防护

```
.gitgo/
  daemon.pid    # 单行: <pid>
```

- 启动时：读取 `daemon.pid` → `os.kill(pid, 0)` 检查存活 → 存活则拒绝启动（exit 1）
- 退出时：`atexit.register()` 清理 PID 文件（正常退出 + crash 都清理）
- 崩溃残留：`os.kill(pid, 0)` 抛 `ProcessLookupError` → 判定为 stale → 覆盖

### 新增依赖

`watchdog` — 跨平台文件系统监控，纯 Python。

添加到 `requirements.txt`：`watchdog>=6.0`

当前 `requirements.txt` 内容：
```
PySide6>=6.7
rich>=13.0
paramiko>=3.0
httpx>=0.27
```

### P2-C 认证标准

- [ ] `gitgo daemon start --project X` 启动后持续运行（不立即退出）
- [ ] workspace 文件修改后 debounce 秒内 emit `workspace_dirty` 事件
- [ ] trial 有新 commit 时在 interval 内 emit `trial_new_commits` 事件
- [ ] stdin 发送 `{"cmd": "status"}` 返回当前状态
- [ ] stdin 发送 `{"cmd": "shutdown"}` 后 daemon 在 3 秒内优雅退出
- [ ] 两个 daemon 同时针对同一项目启动时第二个报错退出
- [ ] 进程崩溃后 PID 文件不阻止重启（stale PID 检测）
- [ ] daemon 正常退出后 `.gitgo/daemon.pid` 被清理
- [ ] `requirements.txt` 含 `watchdog>=6.0`

---

## P2-D: Agent Interface

### 现状

Agent 通过 subprocess 调用 `gitgo --mode xxx --json`，每次调用启动新进程。

### 目标

Agent 有两种使用模式：

**模式 1：One-shot CLI（已有 + P2-A 增强）**
```bash
gitgo status --project X --json --semantic-only
gitgo scan --project X --json --stream
```
适合 CI、脚本、简单的 agent 查询。

**模式 2：Persistent Connection（新增）**
```bash
gitgo daemon start --project X | agent_parse_events
```
适合长时间运行的 agent 会话——连接一次，持续接收事件，随时发起操作。

### MCP Server 包装器（可选，P2-D）

在守护进程之上包装一个 MCP server，使 Claude 等 agent 可以通过 MCP 协议直接调用 Gitgo。

使用 `mcp` Python SDK（`pip install mcp`）实现，预计 ~300-500 行：

```python
# mcp_server.py — 可选组件
# 将 Gitgo CLI 暴露为 MCP tools:
#   - gitgo_status(project) → status_dict (semantic=True)
#   - gitgo_scan(project) → entries
#   - gitgo_formalize(project, indices?, message?) → formal_commit
#   - gitgo_sync(project) → result
#   - gitgo_push(project) → result
#   - gitgo_trial_list(project) → incoming_changes
#   - gitgo_trial_accept(project, index) → result
```

注意：P2-D 的 MCP server 是**无守护进程模式**——每个 tool 调用启动一次性的 `SyncSession` 执行操作
（通过 subprocess 或直接调用 CLI verb 函数）。这与守护进程模式互补：
- **无守护进程**：低开销，适合偶发查询；每次调用启动进程
- **守护进程**：适合长 session；一次连接，持续复用

### P2-D 认证标准

- [ ] daemon 事件流每行输出合法 JSON
- [ ] `{"cmd": "shutdown"}` 后 daemon 在 3 秒内退出
- [ ] agent 可通过 daemon 完成完整的 scan→formalize→sync→push 流程
- [ ] daemon 断连后 agent 重新连接仍能看到最新状态（状态持久化已在 P1-D 完成）
- [ ] MCP server（如果实现）：`tools/list` 返回 7 个 tool 定义
- [ ] MCP server（如果实现）：`tools/call` 正确调度并返回结构化结果

---

## Phase 2 完成标准

| 条件 | 阶段 |
|------|------|
| `status_dict()` 含 `semantic` 块，agent 可直接消费 | P2-A |
| `suggested_next_action` 正确反映 governance 优先级 | P2-A |
| one-shot CLI 支持 `--stream` 流式输出 | P2-A |
| 全部操作类型记录到统一 Operation History | P2-B |
| `gitgo history --json` 可查询和过滤 | P2-B |
| daemon 可后台运行，监控 workspace + 轮询 trial | P2-C |
| daemon 通过 stdin/stdout JSON 协议与 agent 通信 | P2-C |
| daemon 双启动防护 + stale PID 清理 | P2-C |
| daemon 事件流每行合法 JSON | P2-D |
| MCP server（可选）暴露 7 个 tools | P2-D |

## Phase 2 完成后的里程碑

Agent 不再需要：

- 每次调用启动新进程
- 自己从原始计数推断"下一步该做什么"
- 用 cron 定时轮询 trial
- 解析不同格式的 one-shot 输出

Agent 只需要：

1. `gitgo daemon start --project X` 启动 runtime（或 one-shot CLI 偶发查询）
2. 读取 `semantic.suggested_next_action` 知道该做什么
3. 读取 stdout 事件流了解操作进度
4. 发送 JSON 命令发起操作

此时 Gitgo 从 "agent 可以调用的 CLI 工具" 变为 **"agent 可以连接的 development runtime"**。

---

## 与后续阶段的关系

Phase 2 是 Phase 3（Agent Integration — AI 理解 workflow semantics）和 Phase 4（Governance Layer）的基础：
- Semantic state layer → Phase 3 的 "Agent-readable semantics" 的雏形
- Operation history → Phase 4 的 "Semantic change graph" 的数据源
- Daemon event stream → Phase 5 的 "Remote workflow orchestration" 的传输层

Phase 2 不要求实现 AI 自动建议 formal commit、自动分组、自动 triage——那些是 Phase 3 的工作。
Phase 2 只要求 Gitgo 成为一个 agent 可以**持续连接、实时感知、随时操作**的 runtime。

---

## 审阅记录

- **2026-05-13**：基于 v0.12 源码审计完成审阅。主要调整：
  - `--stream` 从 P2-D 移至 P2-A（低风险，独立于守护进程）
  - P2-C 架构从 asyncio 改为纯线程（Windows 兼容性 + 已知模式一致性）
  - P2-B 操作类型从 7 种扩展到 9 种（含 v0.12 delete/dissolve）
  - P2-B `add_entry()` 委托到 `add_operation()` 保持向后兼容
  - `blocked_reason` 枚举从 1 种扩展到 4 种
  - MCP server 预估从 ~100 行修正为 ~300-500 行
  - 新增 `save_session()` is_incoming/sources_cleared 持久化修复（P2-A 附带）
  - 新增 PID stale 检测与双启动防护（P2-C）
