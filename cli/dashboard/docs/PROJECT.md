# gitgo-dashboard CLI 项目文档

> 日期：2026-06-12
> 技术栈：TypeScript + Bun + @anthropic/ink (vendored from claude-code-main) + MCP stdio
> 独立项目，位于 `C:\Users\Duo\Desktop\Truman\ClaudeCode_WorkSpace\gitgo-dashboard\`

---

## 一、架构

```
gitgo-dashboard/
├── package.json              # Bun 项目，27 依赖
├── tsconfig.json             # JSX react-jsx, paths → vendor/ink
├── vendor/ink/src/           # 145 文件，从 claude-code-main 复制（@anthropic/ink fork）
├── src/
│   ├── main.tsx              # 入口：McpClient + renderSync(<App/>)
│   ├── mcp/client.ts         # MCP stdio client（~170 行）
│   ├── hooks/useGitgoData.ts # 数据 hook（~75 行）
│   └── components/
│       ├── App.tsx           # 顶层状态 + 键盘输入 + 焦点管理 + 命令执行（~250 行）
│       ├── Overview.tsx      # 响应式项目列表（~75 行）
│       ├── Detail.tsx        # 三级详情视图（~250 行）
│       ├── CommandBar.tsx    # 命令栏 + 自动补全提示（~80 行）
│       └── HelpPanel.tsx     # 帮助面板（~40 行）
├── scripts/build.ts          # Bun 构建脚本 → dist/cli.js
└── dist/cli.js               # 构建产物 548KB 单文件
```

### 数据流

```
gitgo mcp_server.py (Python FastMCP, 42 tools)
       ↑ MCP stdio (JSON-RPC 2.0)
       │
  src/mcp/client.ts          ← 子进程管理 + 握手 + 请求/响应
       │
  src/hooks/useGitgoData.ts  ← 5s 间隔串行获取，原子更新
       │
  src/components/App.tsx     ← 状态中心 + 键盘分发
       ├── Overview.tsx      ← 项目列表
       ├── Detail.tsx        ← 三级详情
       ├── CommandBar.tsx    ← 命令输入
       └── HelpPanel.tsx     ← 帮助
```

---

## 二、文件说明

### `src/main.tsx`

入口文件。创建 McpClient → 等待握手 → renderSync 渲染 App → waitUntilExit 阻塞直到用户退出。

- Python 路径硬编码为 `C:/Users/Duo/AppData/Local/Programs/Python/Python312/python.exe`
- MCP server 路径为 `../../gitgo/mcp_server.py`（相对 dashboard 项目）
- 刷新间隔通过命令行参数传入（默认 5s）

### `src/mcp/client.ts`

MCP stdio 协议客户端。关键实现：

- **子进程管理**：`spawn(python, [mcp_server_path])`，stdin/stdout/stderr pipe
- **握手流程**：`initialize` request → 等待响应 → `notifications/initialized` 通知
- **请求/响应**：每次 `sendRequest` 分配单调递增 id，pending Map 管理 Promise
- **超时**：30 秒
- **退出处理**：process.on("exit") → reject 所有 pending 请求
- **参数修正**：空 arguments 不发送（FastMCP 拒绝 `{}`）
- **返回值解析**：优先 `structuredContent.result`，否则合并多个 content 条目

### `src/hooks/useGitgoData.ts`

5 秒间隔定时刷新，串行获取每个项目的数据。

调用的 MCP 工具：
- `gitgo_list_projects` → 项目列表（读 config.json，毫秒）
- `gitgo_lesson_list` → pending lessons 数（读 pending.jsonl，毫秒）
- `gitgo_contract_show` → features/constraints 数 + tech_stack（读 contract，毫秒）

**不调用 `gitgo_status`**：因为该工具内部做 `step_scan()` 全量文件 SHA256 哈希，每个项目耗时数秒，不适合概览刷新。

### `src/components/App.tsx`

顶层组件，持有所有状态。

**状态变量**：
| 变量 | 类型 | 用途 |
|---|---|---|
| sel | number | 选中的项目索引 |
| detail | boolean | 是否在详情模式 |
| focus | "table" \| "command" | 键盘焦点 |
| cmdBuf | string | 命令输入文本 |
| cmdCursor | number | 光标位置 |
| cmdResult | string | 命令执行结果 |
| cmdHistory | string[] | 命令历史（暂未展示，仅记录） |
| suggestionIdx | number | 当前选中的补全提示索引 |

**键盘分发（useInput）**：
- 命令模式：光标编辑 + 回车执行 + Esc 退出 + ↑↓ 选补全 + Tab 填充
- 帮助模式：Esc/h/q 关闭
- 详情模式：q 退出
- 概览模式：↑↓ 选项目 + 回车进详情 + `:` 进命令 + h 帮助 + q 退出

**焦点模型**：
```
table → (↓到底 / 按:) → command → (↑空输入 / Esc) → table
```

### `src/components/Overview.tsx`

项目列表表格。5 列：Project / Lessons / Contract / Tech Stack / Path。

- `useTerminalSize()` 检测终端宽度，列宽按百分比自适应（最小 60 列）
- 超长文本截断 + `…`
- 焦点在表格时选中行显示 `▶` 青色标记

### `src/components/Detail.tsx`

**三级导航**：

| 层级 | 操作 | 退出方式 |
|---|---|---|
| L1 概览 | ↑↓ 选项目，Enter 进 L2 | — |
| L2 Tab 列表 | ←→ 切 Tab，↑↓ 选条目，Enter 进 L3 | Esc 回 L1 |
| L3 条目详情 | 查看完整字段 | Esc 回 L2 |

**三个 Tab**：
- Contract：features 列表 + constraints 列表
- Pending Lessons：severity 着色 + 触发原因
- Recent Events：时间戳 + 操作类型 + 状态

**滚动窗口**：`scrollWindow()` 函数根据当前选中位置自动计算可见范围，超出可见区域时显示 `... N more above/below ...` 提示。

**L3 详情视图**：根据条目类型显示不同字段组（LessonFields / FeatureFields / ConstraintFields / EventFields）。

### `src/components/CommandBar.tsx`

底部命令栏，带绿色边框。

- **空闲态**：显示 `> : for commands  ↑↓ move focus  h help  q quit`
- **命令态**：显示输入文本 + 反色光标 + 补全提示列表
- **补全提示**：竖向列表，`▸` 标记选中项，格式 `:command (description)`
- **非命令输入**：不以 `:` 开头时红字提醒 `(type : for command)`

### `src/components/HelpPanel.tsx`

蓝色边框面板，显示键盘快捷键和命令列表。

---

## 三、功能清单

### 已完成

| 功能 | 状态 |
|---|---|
| MCP stdio 连接 + 握手 | ✅ |
| 项目列表展示（3 个项目） | ✅ |
| 5 秒定时刷新 | ✅ |
| 响应式列宽（useTerminalSize） | ✅ |
| 三级导航（概览→Tab→详情） | ✅ |
| 光标编辑（←→Home/End Backspace/Delete 插入） | ✅ |
| 粘贴支持 | ✅ |
| 命令补全提示（↑↓选 Tab填 Enter执行） | ✅ |
| 7 个命令：lesson/contract/status/verify/project/refresh/help | ✅ |
| 滚动窗口（超出可见范围自动滚动） | ✅ |
| React.memo + useCallback 渲染优化 | ✅ |
| Bun 构建（dist/cli.js 548KB） | ✅ |

### 已知问题

| 问题 | 优先级 |
|---|---|
| Python 路径硬编码，不能自动检测 | P1 |
| `gitgo_status` 不在概览调用（太慢），概览缺少 stage/changed 列 | P2 |
| Contract Tab 在有大量 features 时仍可能跳底 | P2 |
| 命令历史记录但未展示/复用界面 | P3 |
| 粘贴时 Bracketed Paste 依赖终端支持（Windows Terminal 支持） | P3 |
| vendor/ink 145 文件与 React 版本耦合，升级需重新对齐 | P3 |

### 待做

| 任务 | 优先级 |
|---|---|
| 概览增加轻量 status 展示（不加 step_scan 的快速模式） | P1 |
| Python 路径自动检测（读 .venv / PATH） | P1 |
| Contract Tab 用固定高度容器替代 flexGrow | P2 |
| L2 列表中高亮当前选中条目背景色（不仅是 ▶ 标记） | P2 |
| 命令历史 Up/Down 恢复（独立于补全的键位） | P3 |
| 鼠标点击支持（Ink AlternateScreen 内） | P3 |
| 构建接入 gitgo build.py | P3 |

---

## 四、命令参考

| 命令 | 示例 | 说明 |
|---|---|---|
| `:lesson [proj]` | `:l gitgo` | 显示 pending lesson 数 + 前 5 条 |
| `:contract [proj]` | `:c` | features/constraints 数 + tech_stack |
| `:status [proj]` | `:s lexi` | stage + changed + formal + next_action |
| `:verify <id>` | `:v abc123` | 验证指定 lesson（调 MCP） |
| `:project <name>` | `:p lexi` | 跳转到指定项目 |
| `:refresh` | `:r` | 强制刷新数据 |
| `:help` | `:h` | 显示帮助面板 |

---

## 五、键盘参考

| 键 | 概览模式 | 命令模式 | 详情模式 |
|---|---|---|---|
| ↑↓ | 选项目 | 选补全提示 / 空时退出 | 选条目 |
| ←→ | — | 移动光标 | 切换 Tab |
| Enter | 进详情 | 执行命令 | 进 L3 详情 |
| Esc | — | 退出命令模式 | 返回上一级 |
| Tab | — | 填充选中补全 | — |
| Home/End | — | 光标跳首/尾 | — |
| Backspace/Delete | — | 删字 | — |
| `:` | 进命令模式 | 输入 `:` 激活补全 | — |
| h | 帮助 | — | — |
| q | 退出 | — | 退出详情 |
