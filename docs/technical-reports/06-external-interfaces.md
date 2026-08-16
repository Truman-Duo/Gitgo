# 报告六：外部接口层 —— MCP、CLI 与 Dashboard 深度解析

> gitgo v0.35 | 2026-07-16 | 完全透底技术报告

---

## 概述

gitgo 提供三个外部入口：**MCP Server**（47 个工具，Claude Code 兼容）、**CLI**（21 个命令，headless 自动化）、**Dashboard**（Ink 终端 UI，人类观测）。

三者的关系：
- MCP 和 CLI 都是后端功能的封装器——它们调用 SyncSession / Daemon / PolicyEngine
- Dashboard 通过 MCP stdio 与后端通信，React → Ink → 终端渲染

**核心文件**：

| 文件 | 行数 | 职责 |
|------|------|------|
| `mcp_server.py` | 67 | FastMCP 服务器入口 |
| `mcp_tools/loop.py` | 361 | Agent 循环工具（chat/fork/status） |
| `mcp_tools/llm_config.py` | 149 | LLM Provider CRUD 工具 |
| `mcp_tools/project.py` | 64 | 项目列表/扫描/状态工具 |
| `mcp_tools/sync.py` | 205 | 同步/Formalize/Trial 工具 |
| `mcp_tools/governance.py` | 142 | 治理/合约/历史工具 |
| `mcp_tools/knowledge.py` | 123 | Lesson + Template 工具 |
| `mcp_tools/memory.py` | 76 | Memory Snapshot + Session 工具 |
| `mcp_tools/cache_stats.py` | 30 | 缓存统计工具 |
| `mcp_tools/daemon_registry.py` | 40 | DaemonClient 单例注册表 |
| `cli/commands.py` | 978 | 15 个 CLI verb |
| `cli/commands_ext.py` | 583 | 6 个扩展 CLI verb |
| `cli/dashboard/src/main.tsx` | 47 | Dashboard 入口 |
| `cli/dashboard/src/mcp/client.ts` | 190 | MCP stdio 客户端 |
| `cli/dashboard/src/state/store.ts` | 131 | 状态管理 |
| `cli/dashboard/src/components/App.tsx` | 562 | 顶层组件 + 键盘分发 |
| `cli/dashboard/src/components/CommandBar.tsx` | 168 | 命令栏 + IME |
| `cli/dashboard/src/components/LLMConfigPanel.tsx` | 446 | LLM 配置面板 |
| `cli/dashboard/src/hooks/` | 5 文件 | 数据获取 hooks |

---

## 一、MCP Server（mcp_server.py + mcp_tools/）

### 1.1 FastMCP 集成

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gitgo")
mcp_tools.register_all(mcp)  # 注册全部 47 个工具

if __name__ == "__main__":
    if "--sse" in sys.argv:
        mcp.run(transport="sse")       # HTTP SSE 传输
    elif "--http" in sys.argv:
        mcp.run(transport="streamable-http")  # Streamable HTTP
    else:
        mcp.run(transport="stdio")     # 默认：stdio JSON-RPC 2.0
```

### 1.2 47 个 MCP 工具分类

#### Agent Loop（5 个）
| 工具 | 说明 | 后端调用链 |
|------|------|-----------|
| `gitgo_fork_agent` | 派生 Agent | DaemonClient → daemon fork_agent → AgentProcessManager.fork() |
| `gitgo_agent_chat` | Agent 对话 | DaemonClient → daemon agent_run → agent_step() |
| `gitgo_agent_instruct` | B Agent 指令 | DaemonClient → daemon agent_run |
| `gitgo_loop_status` | 进程树状态 | DaemonClient → daemon loop_status |
| `gitgo_round_complete` | 交付快照 | DaemonClient → daemon round_complete → _snapshot_workspace() |

#### Project & Sync（20 个）
| 工具 | 说明 |
|------|------|
| `gitgo_list_projects` | 列出所有项目 |
| `gitgo_status` | 项目完整状态（含语义层） |
| `gitgo_scan` | 扫描工作区文件变更 |
| `gitgo_formalize` | 创建正式提交 |
| `gitgo_sync` | 同步到备份仓库（Gate A） |
| `gitgo_push` | 推送到远程（Gate B） |
| `gitgo_trial_list` / `_triage` | Trial 管理 |
| `gitgo_run_workflow` | 一键全流程 |
| `gitgo_formal_*`（6 个） | 正式提交管理（CRUD + 编辑/解散/清除源） |
| `gitgo_suggest_formalize/suggest_triage/suggest_summary` | AI 建议上下文 |

#### Governance（11 个）
| 工具 | 说明 |
|------|------|
| `gitgo_governance_quality/patterns/graph/releases/release_note` | 治理度量 |
| `gitgo_governance_feed` | 治理事件流 |
| `gitgo_history` | 操作历史查询 |
| `gitgo_release_info/create` | 远程 Release |
| `gitgo_remote_issues` | 远程 Issues |
| `gitgo_contract_show/update` | 合约管理 |

#### Knowledge & LLM Config（10 个）
| 工具 | 说明 |
|------|------|
| `gitgo_lesson_list/verify/search/promote` | 知识管理 |
| `gitgo_llm_status/save/switch/delete` | LLM Provider CRUD |
| `gitgo_template_*`（4 个） | 模板管理 |

#### 其他（6 个）
| 工具 | 说明 |
|------|------|
| `gitgo_memory_snapshot/restore/list` | 记忆快照 |
| `gitgo_session`（3 个动作） | Session 管理 |
| `gitgo_export` | State Bundle 导出 |
| `gitgo_cache_stats` | 缓存统计 |
| `gitgo_project_create/archive` | 项目管理 |
| `gitgo_overview` | 轻量概览 |

### 1.3 最复杂工具：gitgo_agent_chat 的完整调用链

```
mcp_tools/loop.py: gitgo_agent_chat(project, message)
  │
  ├─ 1. 加载配置
  │   ├─ config = ConfigManager.load()
  │   ├─ project_config = config.get_project(project)
  │   └─ llm_config = _resolve_llm_config(workspace)
  │       ├─ env GITGO_LLM_BASE_URL/API_KEY/MODEL?
  │       └─ 否则 → LLMConfigManager.get_active(workspace)
  │
  ├─ 2. 构建治理上下文
  │   └─ ctx = build_governance_context(project, workspace_path)
  │       → {signals: [GovernanceSignal, ...], brief: "..."}
  │
  ├─ 3. 尝试 daemon 通路
  │   ├─ client = get_client(project)  # DaemonClient 单例
  │   ├─ client.send_command("llm_configure", providers=[...])
  │   ├─ _ensure_b_agent(client, project, ctx)
  │   │   └─ client.send_command("fork_agent", {
  │   │        role: "B", ring_level: "ring_3",
  │   │        tool_registry: ["scan", "formalize", "sync"],
  │   │        max_steps: 10,
  │   │        context_snapshot: ctx,
  │   │      }) → process_id
  │   └─ client.send_agent_run(process_id, instruction=message)
  │       └─ 等待 agent_complete → 返回 LLM 响应 + 工具调用记录
  │
  └─ 4. Fallback（daemon 离线时）
      ├─ 直接 LLMProvider.chat([system: ctx.brief, user: message])
      └─ 或 Mock 响应（测试/演示模式）
```

### 1.4 DaemonRegistry 单例管理

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
    for client in _clients.values():
        client.stop()
    _clients.clear()

atexit.register(shutdown_all)  # MCP server 退出时自动清理
```

---

## 二、CLI 命令矩阵（cli/commands.py 978 行）

### 2.1 入口点

所有 CLI 命令通过 `--mode` 参数路由：

```python
MODE_MAP = {
    "list": _cmd_list,
    "status": _cmd_status,
    "scan": _cmd_scan,
    "sync": _cmd_sync,
    "push": _cmd_push,
    "formalize": _cmd_formalize,
    "trial": _cmd_trial,
    "daemon": _cmd_daemon,
    "session": _cmd_session,
    "history": _cmd_history,
    "release": _cmd_release,
    "suggest": _cmd_suggest,
    "bootstrap": _cmd_bootstrap,
    "governance": _cmd_governance,
    "export": _cmd_export,
    "template": _cmd_template,
    "formal": _cmd_formal,
    "memory": _cmd_memory,
    "contract": _cmd_contract,
    "lesson": _cmd_lesson,
}
```

### 2.2 关键 CLI 命令的实现特点

**_cmd_daemon**（启动/停止/状态/一次性运行）：
- `start`：拉起 daemon 子进程
- `stop`：发送 shutdown 命令
- `status`：查询 PID 文件
- `run`：一次性执行全流程（scan → formalize → sync → push），完成后退出

**_cmd_suggest**（AI 建议上下文生成）：
- `_build_formalize_context(session, indices)`: 每个 commit 的 diff 统计（新增/删除行数 + 顶层符号）
- `_build_triage_context(session)`: Trial 变更摘要
- `_build_summary_context(session)`: 三段统计（workspace/trial/release）

**_cmd_bootstrap**（一键注册 gitgo 自举）：
创建 gitgo 管理自身项目的配置——即用 gitgo 管理 gitgo 的 workspace → release → GitHub 流程。

### 2.3 Human-Readable 输出格式化

`_print_quality()`, `_print_patterns()`, `_print_graph()`, `_print_releases()`——将 governance metrics 的 dict 输出格式化为终端可读的表格和图表。

---

## 三、Dashboard TUI 架构

### 3.1 技术栈

```
TypeScript + Bun
  ├─ React 19 (JSX via @anthropic/ink)
  ├─ @anthropic/ink (vendored 145 文件: reconciler → Yoga layout → termio ANSI)
  ├─ MCP stdio (JSON-RPC 2.0)
  └─ 状态管理: createStore + useSyncExternalStore (零外部依赖)
```

### 3.2 数据流架构

```
gitgo mcp_server.py (Python FastMCP, 47 tools)
       │ MCP stdio (JSON-RPC 2.0)
       ▼
  src/mcp/client.ts          ← 子进程管理 + 握手 + 请求/响应路由
       │
       ▼
  src/hooks/                 ← 4 数据 hooks + 1 通用 hook
  ├─ useGitgoData  (107 行)  ← 项目列表 + 并行衍生数据
  ├─ useLoopData   (127 行)  ← Agent 进程 + 工具事件（16ms 批量窗口）
  ├─ useChat       (78 行)   ← 消息发送 + 轮询响应
  ├─ useLLMConfig  (117 行)  ← Provider CRUD 绑定
  └─ usePoll       (20 行)   ← 通用 setInterval 轮询
       │
       ▼
  src/state/store.ts          ← createStore<AppState> 全局单例
       │
       ▼
  src/components/App.tsx       ← 状态中心 + 键盘分发 + 7 场景路由
```

### 3.3 MCP 客户端（mcp/client.ts 190 行）

```typescript
class McpClient {
    private process: ChildProcess;
    private pending: Map<number, {resolve, reject}>;
    private nextId = 1;

    constructor(pythonPath: string, mcpServerPath: string) {
        this.process = spawn(pythonPath, [mcpServerPath]);
        // 握手：initialize → initialized notification
        this.handshake();
    }

    async callTool(toolName: string, args?: Record<string, any>): Promise<any> {
        const id = this.nextId++;
        const request = { jsonrpc: "2.0", id, method: "tools/call",
                         params: { name: toolName, arguments: args || {} } };

        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            this.process.stdin.write(JSON.stringify(request) + "\n");
            // 30s 超时
            setTimeout(() => {
                if (this.pending.has(id)) {
                    this.pending.delete(id);
                    reject(new Error(`Tool ${toolName} timed out`));
                }
            }, 30000);
        });
    }
}
```

**响应格式兼容**：处理 FastMCP 的两种响应格式——`structuredContent.result`（优先）和 `content[]` 数组（fallback）。

### 3.4 状态管理（state/store.ts 131 行）

受 Claude Code 自身状态管理启发的最小化模式：

```typescript
type Store<T> = {
    getState: () => T;
    setState: (updater: (prev: T) => T) => void;
    subscribe: (listener: () => void) => () => void;
};

function createStore<T>(initial: T): Store<T> {
    let state = initial;
    const listeners = new Set<() => void>();

    return {
        getState: () => state,
        setState: (updater) => {
            state = updater(state);
            listeners.forEach(fn => fn());
        },
        subscribe: (listener) => {
            listeners.add(listener);
            return () => listeners.delete(listener);
        },
    };
}

function useStore<T, R>(store: Store<T>, selector: (s: T) => R): R {
    return useSyncExternalStore(
        store.subscribe,
        () => selector(store.getState()),
    );
}
```

**AppState 单例**：每个 App 挂载生命周期一个全局 store，包含 scene、inputBuf、cmdBuf、chatMessages、llmConfig 等全部 UI 状态。

### 3.5 App.tsx 键盘分发优先级链（P0-P8）

```
P0.5: Ctrl+V 粘贴（跨平台：powershell Get-Clipboard / pbpaste / xclip）
P1:   帮助叠加层关闭
P2:   内联面板关闭（Contract/Lessons/Events）
P3:   命令模式输入处理（:command）
P4:   Escape — 场景特定返回导航
P4.5: 全局快捷键（l=LLM面板, ?=帮助, /=命令模式）
P5:   场景特定导航（↑↓ 选择, Tab/Shift+Tab 切换焦点, Enter 进入/确认）
P5.6: NORMAL 模式文本编辑（←→ Home/End Delete 光标移动）
P6-P8: 文本输入、退格、回车提交
```

### 3.6 CommandBar IME 支持

```tsx
// 使用 Ink 的 useDeclaredCursor 定位终端物理光标
const cursorCol = (isCommand ? "/ " : "▸ ").length
                + (isCommand ? cmdCursor : inputCursor);

const cursorRef = useDeclaredCursor({
    line: 0,
    column: cursorCol,
    active: inputFocused !== false,
});
```

**机制**：`useDeclaredCursor` → CursorDeclarationContext → Ink onRender() → ANSI `cursorPosition(row, col)` (CUP 序列) → 终端在指定位置渲染 IME 预编辑文字。这使得中文输入法的拼音候选窗口能正确显示在光标位置。

### 3.7 LLMConfigPanel 三种模式（446 行）

**列表模式**：Provider 卡片 + ●/○ 激活标记 + API key 遮罩（`sk-****...ab12`）

**编辑模式**：内联表单（name / base_url / api_key / model_id）+ Tab/Shift+Tab 字段切换 + Enter 保存

**状态模式**：断路器状态（CLOSED/OPEN/HALF_OPEN）+ 故障次数 + Failover 顺序 + Daemon 在线状态

测试连接：调 `gitgo_agent_chat` 验证 Provider 连通性。

### 3.8 useLoopData 的 16ms 批量窗口

```typescript
function useLoopData(client, project, refreshSec) {
    const [pending, setPending] = useRef(false);

    const fetchData = useCallback(async () => {
        if (pending.current) return;  // 跳过重复请求
        pending.current = true;

        setTimeout(async () => {      // 16ms 批量窗口
            const data = await client.callTool("gitgo_loop_status", { project });
            // 原子更新所有状态
            setProcesses(data.processes);
            setToolEvents(data.tool_events);
            setProviders(data.providers);
            pending.current = false;
        }, 16);
    }, [client, project]);

    usePoll(fetchData, refreshSec * 1000, [fetchData]);
}
```

### 3.9 Ink 渲染管线

```
React Component Tree
  │
  ▼
Ink Reconciler (React-reconciler)
  │
  ▼
Yoga Layout Engine (Flexbox 布局计算)
  │
  ▼
termio (ANSI/CSI/SGR/DEC 序列生成)
  │
  ▼
Terminal stdout (原始 ANSI 转义序列)
```

### 3.10 组件清单（18 个）

| 组件 | 行数 | 功能 |
|------|------|------|
| App.tsx | 562 | 顶层状态 + 键盘分发 P0-P8 + 7 场景路由 |
| LLMConfigPanel.tsx | 446 | Provider 三模式管理面板 |
| AgentDetail.tsx | 210 | B Agent 详解 + 上下文热图 + 聊天 |
| CommandBar.tsx | 168 | NORMAL/COMMAND 双模式输入 + IME |
| ProcessList.tsx | 157 | 进程树 + 状态着色 |
| ChatPanel.tsx | 131 | A Agent 聊天 |
| Overview.tsx | 90 | 项目列表 + 状态推导 |
| InlineContext.tsx | 77 | Contract/Lessons/Events 内联面板 |
| ProjectWorkspace.tsx | 70 | 项目工作区数据桥接 |
| TextInput.tsx | 60 | 反色光标渲染 |
| EventsTab.tsx | 63 | 工具事件日志 |
| ToolCallDisplay.tsx | 50 | 共享工具卡片组件 |
| LessonsTab.tsx | 89 | Lesson 三层展示 |
| HelpPanel.tsx | 47 | 键盘参考面板 |
| ContractTab.tsx | 37 | 合约展示 |
| Spinner.tsx | 14 | Unicode 点状旋转动画 |

---

## 四、测试覆盖

| 测试文件 | 测试内容 |
|----------|----------|
| `test_loop/test_executor.py` | XML tool_call 解析（Dashboard 依赖的 agent chat 后端） |
| `test_loop/test_llm.py` | CircuitBreaker 状态（LLMConfigPanel 显示的数据） |
| 构建：`bun run build` | Dashboard TypeScript 编译验证 |

**Dashboard 前端测试策略**：通过 `--mock` CLI 标志使用 MockMcpClient（mockData.ts 485 行固定数据）测试全量 UI 场景，不连接真实后端。

---

## 五、已知限制与潜在问题

1. **MCP 工具与 CLI 命令的重复实现**：`gitgo_status` MCP 工具和 `--mode status` CLI 命令都调用 `SyncSession.status_dict()`，但参数处理逻辑有差异（如 CLI 支持 `--layered`），可能导致输出不一致。

2. **agent_chat 的 timeout 硬编码**：`send_agent_run` 默认 timeout=300s（5 分钟）。对于复杂任务可能不够，但没有提供自定义 timeout 的接口。

3. **Dashboard 轮询开销**：`useLoopData` 每 `refreshSec` 秒调用 `gitgo_loop_status`（默认 5s）。如果项目数量多，每个项目都需要一次 MCP 调用。

4. **McpClient 无重连机制**：与 DaemonClient 不同，Dashboard 的 McpClient 在子进程崩溃时不会自动重连——需要用户重启 Dashboard。

5. **useDeclaredCursor 的跨终端兼容性**：ANSI CUP 序列在某些终端（Windows Terminal 旧版、ConEmu）可能行为不同。

6. **CLI 命令的参数解析是手工的**：没有使用 argparse 子命令，而是通过 `if/elif` 链和 `--mode` 参数路由，扩展新命令需要修改 MODE_MAP。

---

## 六、设计审查总结

### ✅ 已实现
- 47 个 MCP 工具覆盖全部后端功能
- 21 个 CLI 命令（headless 自动化）
- Dashboard 18 组件 + 5 hooks 的完整 TUI
- MCP stdio JSON-RPC 2.0 协议
- DaemonClient 单例注册表（atexit 自动清理）
- IME 中文输入支持
- 场景路由 + 键盘分发系统

### ⚠️ 部分实现
- MCP/CLI 实现有轻微差异
- Dashboard 不支持子进程自动重连
- CLI 参数解析非标准

### ❌ 未实现
- Dashboard 的 Trial 状态展示（P2 待做）
- Dashboard 的远程连接器状态展示
- Web Dashboard（替代 Ink TUI 的浏览器版本）

---

## v0.34-v0.35 更新补遗

**v0.34**: Dashboard `--native` 通路: DaemonClient.ts 直连 daemon stdin/stdout。
双路径并存: MCP(Claude Code兼容) + 原生(gitgo Loop专用)。

**v0.35**: MCP 工具新增 recall_grep/recall_semantic/recall_rag (3个)。
Knowledge System 架构文档 + Testing Subsystem 架构文档。
501 测试 (从 334 增长 +167)。

---

## v0.36-v0.41 更新补遗

**v0.39 错误恢复（架构）**:
- `mcp_tools/cache_stats.py`: 新增缓存统计 MCP 工具，暴露 `cache/file_hash.py .stats()`

**v0.40 流式响应（架构）**:
- `cli/dashboard/src/daemon/streamEvents.ts` + `streamReducer.ts`: 流式事件管线骨架
- `components/StreamingMessage.tsx`: 流式消息渲染组件
- `chat/sendChat.ts`: 聊天发送接入
- 后端 LLM 流式 → 前端渲染端到端接线待迭代

**v0.41 前端工作（架构）**:
- `components/config/`: Bin / Providers / Publish 三标签（ConfigPanel 多标签化）
- `input/overlays/`（13 个 overlay 子模块）: 输入覆盖层拆分
- `mock/`（11 个数据域）: mock 数据模块化
- `effects/run.ts`: 运行时效果
- 新面板: Governance / Memory / Formal / Trial / Lessons / Export / Status / RuntimeMenu / Quit / ConfirmBox / DiffView
- `commands.ts` 命令矩阵更新
