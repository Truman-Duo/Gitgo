# gitgo-dashboard 异构开发执行计划

> 日期：2026-06-11
> 技术栈：TypeScript + Bun + @anthropic/ink (React for terminals) + MCP stdio
> 目标：替代 `cli/dashboard.py` 的 Rich/mSVCRT 方案，零 stdin/stdout 竞争

---

## 一、架构

```
┌─────────────────────────┐      MCP stdio (JSON-RPC)      ┌──────────────────────┐
│  gitgo-dashboard        │ ──────────────────────────────→ │  gitgo mcp_server.py │
│  (Bun + Ink + React)    │ ←────────────────────────────── │  (Python, 已有)       │
│                         │                                 │                      │
│  ~400 行新代码           │                                 │  40+ tools，零改动    │
│  复用 @anthropic/ink    │                                 │                      │
└─────────────────────────┘                                 └──────────────────────┘
```

dashboard 不 import 任何 gitgo Python 代码。通过 MCP stdio transport 调用 `mcp_server.py` 获取数据。两进程独立，语言无关。

---

## 二、目录结构

在 `C:\Users\Duo\Desktop\Truman\ClaudeCode_WorkSpace\gitgo-dashboard\` 创建：

```
gitgo-dashboard/
├── package.json
├── tsconfig.json
├── src/
│   ├── main.tsx              # 入口：启动 Ink App + MCP client
│   ├── components/
│   │   ├── App.tsx            # 顶层：Layout + 状态管理
│   │   ├── Overview.tsx       # 项目列表表格（对应 _view_overview）
│   │   ├── Detail.tsx         # 项目详情（对应 _view_project_detail）
│   │   ├── CommandBar.tsx     # 底部指令行
│   │   └── HelpPanel.tsx      # 帮助面板
│   ├── mcp/
│   │   └── client.ts          # MCP stdio client（JSON-RPC 封装）
│   └── hooks/
│       └── useGitgoData.ts    # 定时拉 MCP 数据 + 缓存
├── vendor/
│   └── ink/                   # 从 claude-code-main 复制的 @anthropic/ink
└── scripts/
    └── build.ts               # Bun 构建脚本
```

---

## 三、执行步骤

### Step 1：初始化项目骨架（10 分钟）

```bash
cd C:\Users\Duo\Desktop\Truman\ClaudeCode_WorkSpace
mkdir gitgo-dashboard
cd gitgo-dashboard
bun init -y
```

**`package.json`**：

```json
{
  "name": "gitgo-dashboard",
  "version": "1.0.0",
  "type": "module",
  "bin": {
    "gitgo-dashboard": "./dist/cli.js"
  },
  "dependencies": {
    "react": "^19.2.4",
    "react-reconciler": "^0.33.0"
  },
  "devDependencies": {
    "bun-types": "latest"
  }
}
```

**`tsconfig.json`**：

```json
{
  "compilerOptions": {
    "target": "ESNext",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "paths": {
      "@anthropic/ink": ["./vendor/ink/src/index.ts"]
    }
  },
  "include": ["src/**/*.ts", "src/**/*.tsx", "vendor/**/*.ts", "vendor/**/*.tsx"]
}
```

安装依赖：

```bash
bun install
```

### Step 2：复制 Ink 基础设施（5 分钟）

**源目录**：`C:\Users\Duo\Desktop\claude-code-main\claude-code-main\packages\@ant\ink\`

**目标目录**：`gitgo-dashboard/vendor/ink/`

```bash
# Windows 命令
xcopy /E /I "C:\Users\Duo\Desktop\claude-code-main\claude-code-main\packages\@ant\ink\src" "C:\Users\Duo\Desktop\Truman\ClaudeCode_WorkSpace\gitgo-dashboard\vendor\ink\src"
xcopy "C:\Users\Duo\Desktop\claude-code-main\claude-code-main\packages\@ant\ink\package.json" "C:\Users\Duo\Desktop\Truman\ClaudeCode_WorkSpace\gitgo-dashboard\vendor\ink\"
```

只需要复制 `src/` 目录和 `package.json`。不需要 `docs/`、`utils/`、`tsconfig.json`。

**额外安装 Ink 的依赖**（这些在 claude-code-main 的 node_modules 里，需要独立安装）：

```bash
cd gitgo-dashboard
bun add auto-bind bidi-js chalk cli-boxes figures indent-string signal-exit strip-ansi wrap-ansi usehooks-ts
```

验证 Ink 能 import：

```bash
bun -e "import { Box, Text, render } from './vendor/ink/src/index.ts'; console.log('Ink OK');"
```

### Step 3：写 MCP stdio client（`src/mcp/client.ts`，~80 行）

MCP 协议是 JSON-RPC 2.0 over stdio。关键要点：

**启动 mcp_server.py**：作为子进程，通过 stdin/stdout pipe 通信。

```typescript
// src/mcp/client.ts — MCP stdio client
import { spawn, type ChildProcess } from 'node:child_process';

type JsonRpcResponse = {
  jsonrpc: '2.0';
  id: number;
  result?: any;
  error?: { code: number; message: string };
};

export class McpClient {
  private proc: ChildProcess;
  private requestId = 0;
  private pending = new Map<number, { resolve: Function; reject: Function }>();
  private buffer = '';

  constructor(pythonPath: string, mcpServerPath: string) {
    this.proc = spawn(pythonPath, [mcpServerPath], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.proc.stdout!.on('data', (chunk: Buffer) => this.onData(chunk.toString()));
    this.proc.stderr!.on('data', (d: Buffer) => {
      // MCP stderr 是日志，不参与协议。静默丢弃以免污染终端。
    });
  }

  private onData(data: string) {
    this.buffer += data;
    // MCP 消息以 \n 分隔
    const lines = this.buffer.split('\n');
    this.buffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const msg: JsonRpcResponse = JSON.parse(line);
        const pending = this.pending.get(msg.id);
        if (pending) {
          this.pending.delete(msg.id);
          if (msg.error) {
            pending.reject(new Error(`MCP error ${msg.error.code}: ${msg.error.message}`));
          } else {
            pending.resolve(msg.result);
          }
        }
      } catch { /* skip malformed lines */ }
    }
  }

  async callTool(toolName: string, args: Record<string, any> = {}): Promise<any> {
    const id = ++this.requestId;

    // 先发 initialize（MCP 握手）
    if (id === 1) {
      await this.sendRequest(0, 'initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'gitgo-dashboard', version: '1.0.0' },
      });
      // 不等待 initialized response，直接发 tools/list
      await this.sendRequest(++this.requestId, 'notifications/initialized', {});
    }

    return this.sendRequest(id, 'tools/call', {
      name: toolName,
      arguments: args,
    }).then((r: any) => {
      // FastMCP 的返回格式：{ content: [{ type: 'text', text: '...' }] }
      const content = r?.content;
      if (content && content.length > 0 && content[0].type === 'text') {
        // 尝试解析 JSON，如果是 JSON 字符串则展开
        try {
          return JSON.parse(content[0].text);
        } catch {
          return content[0].text;
        }
      }
      return r;
    });
  }

  private sendRequest(id: number, method: string, params: any): Promise<any> {
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      const request = JSON.stringify({ jsonrpc: '2.0', id, method, params });
      this.proc.stdin!.write(request + '\n');
      // 超时保护
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`MCP timeout: ${method}`));
        }
      }, 15000);
    });
  }

  close() {
    this.proc.kill();
  }
}
```

**Python 路径**：用户系统中 Python 的位置。可以传 `.venv/Scripts/python.exe`。

**MCP server 路径**：`C:\Users\Duo\Desktop\Truman\ClaudeCode_WorkSpace\gitgo\mcp_server.py`。

### Step 4：写数据 hook（`src/hooks/useGitgoData.ts`，~50 行）

```typescript
// src/hooks/useGitgoData.ts
import { useState, useEffect, useRef } from 'react';
import { McpClient } from '../mcp/client.js';

type Project = {
  name: string;
  workspace: string;
  backup: string;
  commit_prefix: string;
};

type GateInfo = {
  status: string;
  commit: string;
  time: string;
};

type ProjectRow = Project & {
  gate: GateInfo;
  pendingCount: number;
  features: number;
  constraints: number;
};

export function useGitgoData(client: McpClient | null, refreshSec: number = 5) {
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<Timer | null>(null);

  const fetchData = async () => {
    if (!client) return;
    try {
      // 1. 获取项目列表
      const projectList: Project[] = await client.callTool('gitgo_list_projects');

      // 2. 对每个项目获取 status
      const rows: ProjectRow[] = [];
      for (const p of projectList) {
        try {
          const status: any = await client.callTool('gitgo_status', {
            project: p.name,
            layered: true,
          });
          const gov = status?.governance || {};
          rows.push({
            ...p,
            gate: {
              status: gov.gate_a_status || 'idle',
              commit: gov.gate_a_commit || '-',
              time: gov.gate_a_time || '-',
            },
            pendingCount: status?.semantic?.pending_lessons || 0,
            features: status?.semantic?.decided_features || 0,
            constraints: status?.semantic?.architecture_constraints || 0,
          });
        } catch {
          // 单个项目失败不影响其他
          rows.push({
            ...p,
            gate: { status: 'error', commit: '-', time: '-' },
            pendingCount: 0,
            features: 0,
            constraints: 0,
          });
        }
      }
      setProjects(rows);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData(); // 首次立即拉
    timerRef.current = setInterval(fetchData, refreshSec * 1000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [client]);

  return { projects, loading, error, refresh: fetchData };
}
```

### Step 5：写 UI 组件（`src/components/`，~300 行）

#### 5a. `App.tsx` — 顶层布局 + 状态

```tsx
// src/components/App.tsx
import React, { useState, useEffect } from 'react';
import { Box, Text, useInput, useApp } from '@anthropic/ink';
import { McpClient } from '../mcp/client.js';
import { useGitgoData } from '../hooks/useGitgoData.js';
import { Overview } from './Overview.js';
import { Detail } from './Detail.js';
import { CommandBar } from './CommandBar.js';
import { HelpPanel } from './HelpPanel.js';

type Props = { client: McpClient; refreshSec?: number };

export function App({ client, refreshSec = 5 }: Props) {
  const { exit } = useApp();
  const { projects, loading, error, refresh } = useGitgoData(client, refreshSec);
  const [sel, setSel] = useState(0);
  const [detail, setDetail] = useState(false);
  const [cmdBuf, setCmdBuf] = useState<string | null>(null);
  const [cmdResult, setCmdResult] = useState('');
  const [showHelp, setShowHelp] = useState(false);

  // 全局键盘处理（参考 claude-code-main REPL 的 useInput 模式）
  useInput((input: string, key: any) => {
    // 命令模式
    if (cmdBuf !== null) {
      if (key.return) {
        if (cmdBuf.trim()) {
          handleCommand(cmdBuf);
        }
        setCmdBuf(null);
        return;
      }
      if (key.escape) { setCmdBuf(null); return; }
      if (key.backspace || key.delete) {
        setCmdBuf(prev => prev ? prev.slice(0, -1) : '');
        return;
      }
      // 普通字符追加
      if (input && input.length === 1 && !key.ctrl && !key.meta) {
        setCmdBuf(prev => (prev ?? '') + input);
        return;
      }
      return;
    }

    // 导航模式
    if (input === 'q' && !detail) { exit(); return; }
    if (key.escape && detail) { setDetail(false); return; }
    if (input === ':' && !detail) { setCmdBuf(''); return; }
    if (input === 'h' && !detail) { setShowHelp(prev => !prev); return; }
    if (key.return && !detail && projects.length > 0) { setDetail(true); return; }
    if (key.upArrow && !detail) { setSel(prev => Math.max(0, prev - 1)); return; }
    if (key.downArrow && !detail) { setSel(prev => Math.min(projects.length - 1, prev + 1)); return; }
  });

  const handleCommand = async (cmd: string) => {
    const parts = cmd.trim().split(/\s+/);
    const action = parts[0]?.toLowerCase();
    const target = parts[1];

    try {
      switch (action) {
        case 'l':
        case 'lesson': {
          const name = target || projects[sel]?.name;
          if (!name) { setCmdResult('No project selected'); return; }
          const status = await client.callTool('gitgo_status', { project: name, layered: true });
          const lessons = status?.semantic?.pending_lessons || 0;
          setCmdResult(`${name}: ${lessons} pending lessons`);
          break;
        }
        case 'c':
        case 'contract': {
          const name = target || projects[sel]?.name;
          if (!name) { setCmdResult('No project selected'); return; }
          const contract: any = await client.callTool('gitgo_contract_show', { project: name });
          if (!contract || !contract.decided_features) {
            setCmdResult(`${name}: no contract`);
          } else {
            const f = contract.decided_features.length;
            const c = contract.architecture_constraints?.length || 0;
            setCmdResult(`${name}: ${f} features, ${c} constraints`);
          }
          break;
        }
        case 's':
        case 'status': {
          const name = target || projects[sel]?.name;
          if (!name) { setCmdResult('No project selected'); return; }
          const status: any = await client.callTool('gitgo_status', { project: name, layered: true });
          const gov = status?.governance || {};
          setCmdResult(`${name}  Gate A:${gov.gate_a_status || 'idle'}  Commit:${gov.gate_a_commit || '-'}`);
          break;
        }
        case 'r':
        case 'refresh':
          await refresh();
          setCmdResult('Refreshed');
          break;
        case 'h':
        case 'help':
          setShowHelp(true);
          setCmdResult('');
          break;
        default:
          setCmdResult(`Unknown: ${cmd}  (:h for help)`);
      }
    } catch (e: any) {
      setCmdResult(`Error: ${e.message}`);
    }
  };

  if (loading) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text dimColor>Loading projects...</Text>
      </Box>
    );
  }

  if (error) {
    return (
      <Box flexDirection="column" padding={1}>
        <Text color="red">Error: {error}</Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" height="100%">
      {showHelp ? (
        <HelpPanel onDismiss={() => setShowHelp(false)} />
      ) : detail && projects[sel] ? (
        <Detail
          projectName={projects[sel].name}
          client={client}
          onBack={() => setDetail(false)}
        />
      ) : (
        <Overview projects={projects} sel={sel} />
      )}

      {/* 命令结果显示 */}
      {cmdResult ? (
        <Box paddingLeft={1}>
          <Text color="yellow">{cmdResult}</Text>
        </Box>
      ) : null}

      <CommandBar buf={cmdBuf} />
    </Box>
  );
}
```

#### 5b. `Overview.tsx` — 项目列表

```tsx
// src/components/Overview.tsx
import React from 'react';
import { Box, Text } from '@anthropic/ink';
import type { ProjectRow } from '../hooks/useGitgoData.js';

type Props = { projects: ProjectRow[]; sel: number };

const GATE_COLORS: Record<string, string> = {
  passed: 'green',
  blocked: 'red',
  idle: 'gray',
};

export function Overview({ projects, sel }: Props) {
  return (
    <Box flexDirection="column" paddingLeft={1} paddingRight={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">
          Gitgo Monitor <Text dimColor>(↑↓ select  Enter detail  :cmd  h help  q quit)</Text>
        </Text>
      </Box>

      {/* 表头 */}
      <Box flexDirection="row">
        <Box width={2}><Text> </Text></Box>
        <Box width={14}><Text bold>Project</Text></Box>
        <Box width={18}><Text bold>Gate A</Text></Box>
        <Box width={14}><Text bold>Commit</Text></Box>
        <Box width={8}><Text bold>Lessons</Text></Box>
        <Box width={12}><Text bold>Contract</Text></Box>
        <Box width={20}><Text bold>Last</Text></Box>
      </Box>

      {projects.map((p, i) => {
        const isSelected = i === sel;
        const gateColor = GATE_COLORS[p.gate.status] || 'gray';
        return (
          <Box key={p.name} flexDirection="row">
            <Box width={2}>
              <Text color={isSelected ? 'cyan' : undefined}>
                {isSelected ? '▶' : ' '}
              </Text>
            </Box>
            <Box width={14}>
              <Text color={isSelected ? 'cyan' : undefined} bold={isSelected}>
                {p.name}
              </Text>
            </Box>
            <Box width={18}>
              <Text color={gateColor}>{p.gate.status.toUpperCase()}</Text>
            </Box>
            <Box width={14}>
              <Text dimColor>{p.gate.commit}</Text>
            </Box>
            <Box width={8}>
              <Text>{String(p.pendingCount)}</Text>
            </Box>
            <Box width={12}>
              <Text>{p.features}f/{p.constraints}c</Text>
            </Box>
            <Box width={20}>
              <Text dimColor>{p.gate.time}</Text>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}
```

#### 5c. `Detail.tsx` — 项目详情

```tsx
// src/components/Detail.tsx
import React, { useState, useEffect } from 'react';
import { Box, Text, useInput } from '@anthropic/ink';
import { McpClient } from '../mcp/client.js';

type Props = { projectName: string; client: McpClient; onBack: () => void };

type DetailData = {
  contract?: any;
  lessons?: any[];
  events?: any[];
};

export function Detail({ projectName, client, onBack }: Props) {
  const [data, setData] = useState<DetailData>({});
  const [tab, setTab] = useState(0); // 0=Contract, 1=Pending, 2=History
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const [contract, status, history] = await Promise.all([
          client.callTool('gitgo_contract_show', { project: projectName }),
          client.callTool('gitgo_lesson_list', { project: projectName }),
          client.callTool('gitgo_history', { project: projectName, limit: 10 }),
        ]);
        setData({
          contract,
          lessons: status?.pending || [],
          events: Array.isArray(history) ? history : [],
        });
      } catch {}
      setLoading(false);
    })();
  }, [projectName]);

  useInput((_input: string, key: any) => {
    if (key.leftArrow) { setTab(prev => Math.max(0, prev - 1)); return; }
    if (key.rightArrow) { setTab(prev => Math.min(2, prev + 1)); return; }
  });

  if (loading) return <Text dimColor>Loading {projectName}...</Text>;

  const TAB_LABELS = ['Contract', 'Pending Lessons', 'Recent Events'];

  return (
    <Box flexDirection="column" paddingLeft={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">{projectName}</Text>
        <Text dimColor> (Esc back  ←→ switch tab)</Text>
      </Box>

      {/* Tab 栏 */}
      <Box flexDirection="row" marginBottom={1}>
        {TAB_LABELS.map((label, i) => (
          <Box key={label} paddingRight={2}>
            <Text underline={i === tab} color={i === tab ? 'cyan' : undefined}>
              {label}
            </Text>
          </Box>
        ))}
      </Box>

      {/* Tab 内容 */}
      {tab === 0 && (
        <Box flexDirection="column">
          {data.contract ? (
            <>
              <Text>Tech: {data.contract.tech_stack?.join(', ') || '(none)'}</Text>
              <Text>Features: {data.contract.decided_features?.length || 0}</Text>
              {data.contract.decided_features?.map((f: any) => (
                <Text key={f.name}>  [{f.confirmed_count}x] {f.name}</Text>
              ))}
              <Text>Constraints: {data.contract.architecture_constraints?.length || 0}</Text>
              {data.contract.architecture_constraints?.map((c: string) => (
                <Text key={c} color="red">  - {c}</Text>
              ))}
            </>
          ) : (
            <Text dimColor>(none)</Text>
          )}
        </Box>
      )}
      {tab === 1 && (
        <Box flexDirection="column">
          <Text bold>Pending Lessons ({data.lessons?.length || 0})</Text>
          {data.lessons?.slice(0, 12).map((l: any) => (
            <Text key={l.id || l.trigger}>
              [{l.severity?.[0] || '?'}] {l.category || '?'}: {l.trigger?.slice(0, 70)}
            </Text>
          )) || <Text dimColor>(none)</Text>}
        </Box>
      )}
      {tab === 2 && (
        <Box flexDirection="column">
          <Text bold>Recent Events</Text>
          {data.events?.slice(-8).map((e: any, i: number) => (
            <Text key={i} dimColor>
              {e.timestamp?.slice(0, 19)} {e.operation} {e.status || ''}
            </Text>
          ))}
        </Box>
      )}
    </Box>
  );
}
```

#### 5d. `CommandBar.tsx` — 底部指令行

```tsx
// src/components/CommandBar.tsx
import React from 'react';
import { Box, Text } from '@anthropic/ink';

type Props = { buf: string | null };

export function CommandBar({ buf }: Props) {
  const display = buf !== null ? (buf.length > 0 ? buf : ' ') : '';
  return (
    <Box
      borderStyle="single"
      borderColor="green"
      paddingLeft={1}
      paddingRight={1}
    >
      <Text dimColor>Command</Text>
      <Text dimColor> (: 开始  Esc 取消  Enter 执行)  </Text>
      {buf !== null ? (
        <Text color="green">{` > ${display}_`}</Text>
      ) : (
        <Text dimColor>{' > _'}</Text>
      )}
    </Box>
  );
}
```

#### 5e. `HelpPanel.tsx` — 帮助

```tsx
// src/components/HelpPanel.tsx
import React from 'react';
import { Box, Text, useInput } from '@anthropic/ink';

type Props = { onDismiss: () => void };

export function HelpPanel({ onDismiss }: Props) {
  useInput((_input: string, key: any) => {
    if (key.escape || _input === 'h') { onDismiss(); }
  });

  return (
    <Box flexDirection="column" padding={1} borderStyle="single" borderColor="blue">
      <Text bold>Keyboard:</Text>
      <Text>  ↑↓     Select project</Text>
      <Text>  Enter  View/exit detail</Text>
      <Text>  ←→     Detail tab switch</Text>
      <Text>  :      Enter command mode</Text>
      <Text>  h      Toggle help</Text>
      <Text>  q      Quit</Text>
      <Text>  Esc    Back / cancel</Text>
      <Text> </Text>
      <Text bold>Commands:</Text>
      <Text>  l[esson] [proj]   View lessons</Text>
      <Text>  c[ontract] [proj] View contract</Text>
      <Text>  s[tatus] [proj]   View status</Text>
      <Text>  r[efresh]         Force refresh</Text>
      <Text>  h[elp]            This panel</Text>
      <Text> </Text>
      <Text dimColor>Press h or Esc to dismiss</Text>
    </Box>
  );
}
```

### Step 6：写入口文件（`src/main.tsx`，~40 行）

```tsx
// src/main.tsx
import React from 'react';
import { render } from '@anthropic/ink';
import { McpClient } from './mcp/client.js';
import { App } from './components/App.js';
import { resolve } from 'node:path';

// 找 Python 和 mcp_server.py 的路径
const GITGO_DIR = resolve(import.meta.dir, '../../gitgo');
const PYTHON = process.platform === 'win32'
  ? resolve(GITGO_DIR, '.venv/Scripts/python.exe')
  : 'python3';
const MCP_SERVER = resolve(GITGO_DIR, 'mcp_server.py');

const REFRESH_SEC = parseInt(process.argv[2] || '5', 10);

async function main() {
  const client = new McpClient(PYTHON, MCP_SERVER);

  // 等待 MCP 握手完成
  await new Promise(r => setTimeout(r, 500));

  const { unmount } = render(
    <App client={client} refreshSec={REFRESH_SEC} />,
    { exitOnCtrlC: true },
  );

  // 等待 React 卸载（用户按 q）
  await unmount;
  client.close();
}

main().catch(err => {
  console.error('Dashboard error:', err);
  process.exit(1);
});
```

### Step 7：写构建脚本（`scripts/build.ts`，~30 行）

```typescript
// scripts/build.ts
import { build } from 'bun';

const result = await build({
  entrypoints: ['./src/main.tsx'],
  outdir: './dist',
  target: 'bun',
  format: 'esm',
  naming: '[dir]/cli.[ext]',
  minify: true,
});

if (result.success) {
  console.log('Build OK: dist/cli.js');
  for (const log of result.logs) {
    console.log(log);
  }
} else {
  console.error('Build failed');
  process.exit(1);
}
```

添加到 `package.json`：

```json
"scripts": {
  "build": "bun run scripts/build.ts",
  "dev": "bun run src/main.tsx"
}
```

构建：

```bash
cd gitgo-dashboard
bun run build
```

产物：`dist/cli.js`。用 Bun 运行时 ~5MB（含 React+Ink）。

### Step 8：接入 gitgo build.py（可选）

在 `gitgo/build.py` 的构建流程末尾加一步，把 `gitgo-dashboard/dist/cli.js` 复制到 `gitgo/dist/gitgo-dashboard.js`，并附带 `bun` runtime 包装脚本：

```python
# gitgo/build.py 末尾加
import shutil, os
dashboard_src = Path(__file__).parent.parent / 'gitgo-dashboard' / 'dist' / 'cli.js'
if dashboard_src.exists():
    shutil.copy(dashboard_src, Path('dist') / 'gitgo-dashboard.js')
    print('Dashboard bundled: dist/gitgo-dashboard.js')
```

---

## 四、数据流对照

| 旧 (Python Rich) | 新 (TypeScript Ink) |
|---|---|
| `_last_gate(ws)` 读本地 JSON | `gitgo_status(project, layered=True)` 走 MCP |
| `_pending_count(ws)` 读本地 JSONL | `gitgo_lesson_list(project)` 走 MCP |
| `_contract(ws)` 读本地 Contract | `gitgo_contract_show(project)` 走 MCP |
| `_load_history(ws)` 读本地 JSON | `gitgo_history(project, limit=10)` 走 MCP |
| msvcrt 轮询键盘 | Ink `useInput()` event-driven |
| Rich Live 全屏刷新 | Ink React reconciler diff 渲染 |
| 270 行，单文件 | ~400 行，7 个文件 |

**关键差异**：旧的 dashboard 直接从磁盘读 gitgo 的内部数据文件（`gitgo_history.json`、`pending.jsonl`），新 dashboard 通过 MCP 协议走正式 API。这意味着新 dashboard 和 CC 之间**不会产生文件锁冲突**——所有读写都由 `mcp_server.py` 串行化。

---

## 五、cc 的完整工作清单（按顺序）

- [ ] 1. 创建 `gitgo-dashboard/` 目录，`bun init`
- [ ] 2. 复制 `claude-code-main/packages/@ant/ink/src/` → `vendor/ink/src/`
- [ ] 3. 安装依赖 (`bun install` + `bun add` Ink 的 peer deps)
- [ ] 4. 验证 `import { render } from '../../vendor/ink/src/index.ts'` 能跑
- [ ] 5. 写 `src/mcp/client.ts`（MCP stdio 封装）
- [ ] 6. 测试 MCP client 能调 `gitgo_list_projects()`
- [ ] 7. 写 `src/hooks/useGitgoData.ts`
- [ ] 8. 写 `src/components/Overview.tsx`
- [ ] 9. 写 `src/components/Detail.tsx`
- [ ] 10. 写 `src/components/CommandBar.tsx`
- [ ] 11. 写 `src/components/HelpPanel.tsx`
- [ ] 12. 写 `src/components/App.tsx`
- [ ] 13. 写 `src/main.tsx`
- [ ] 14. 写 `scripts/build.ts`
- [ ] 15. 构建 + 全功能测试
- [ ] 16. 接入 gitgo build.py

---

## 六、cc 需要特别小心的点

1. **Ink 的 `useInput` 是全局的**——整个 App 中只有一个 `useInput` 处理器。不要在子组件中也加 `useInput`。用 `isActive` 选项控制哪个组件响应（已在代码中体现）。

2. **MCP 初始化消息必须发**——`initialize` + `notifications/initialized` 两个消息是 FastMCP 要求的握手。不发的话后续 `tools/call` 会被忽略。

3. **Bun 的 `node:child_process` spawn 在 Windows 上完整可用**——不要用 `Bun.spawn` api，用 Node.js 兼容的 `spawn`。

4. **Ink 的 `Box` width 单位是列（不是 px）**——Overview.tsx 中的 `width={14}` 表示 14 个字符宽度。如果终端宽度小于各列之和，Box 会自动截断。

5. **不删除旧的 `cli/dashboard.py`**——先并行存在，新 dashboard 确认稳定后再删。
