# gitgo v0.30 交接文档

> 日期：2026-07-05

---

## 当前状态

**v0.30 完成。** Dispatch Layer + LLM Provider 配置面板 + Agent Loop A→B 通路真实化。

### 当前工作流
- **Agent**: 每轮结束调 MCP `gitgo_round_complete("project")` → Gate A 检查 → passed 继续 / blocked 修复
- **Agent Loop**: MCP `gitgo_fork_agent` → DaemonClient → daemon → AgentProcessManager → B-level Agent 执行
- **人**: `cd cli/dashboard && bun run src/main.tsx` 实时查看所有项目状态
- **LLM 配置**: Dashboard 内按 `L` 或 `:llm` 打开 Provider 管理面板，支持多 Provider 切换 + CRUD
- **Daemon**: 后台 Policy Engine 持续扫描 + 积累 governance event（可选）
- **Release**: `gitgo push --project X --strip-authorship` 手动发布

### v0.30 新增

#### Dispatch Layer
- `backend/core/daemon/client.py` — DaemonClient：subprocess-based daemon 通信（~200 行）
- `mcp_tools/daemon_registry.py` — 单例缓存 + atexit shutdown
- `mcp_tools/loop.py` — 重写：A→B 三个工具全部走 daemon 真实通路
- `backend/core/dispatch/dispatcher.py` — ToolDispatcher 命令分发 + RingGate

#### LLM Provider 配置
- `backend/core/llm_config.py` — LLMConfigManager：`.gitgo/llm_config.json` CRUD
- `mcp_tools/llm_config.py` — 4 个 MCP 工具（status/save/switch/delete）
- `cli/dashboard/src/components/LLMConfigPanel.tsx` — Ink 终端 Provider 管理面板
- `cli/dashboard/src/hooks/useLLMConfig.ts` — React hook 封装

#### 模块结构
- `backend/core/loop/` — Agent 循环（context/gate/llm/manager/models/tools）
- `backend/core/policy/` — Policy Engine 可插拔（base/contract/dependency/identity/lessons/registry）
- `backend/core/steps/` — 纯函数管线（commits/scan/sync）
- `backend/core/fact/` — 模式匹配（contract/file/workflow patterns）
- `backend/core/cache/` — 文件哈希缓存

#### Bug 修复
- `daemon/__init__.py`: UnboundLocalError — `evq` 初始化移至引用之前
- DaemonClient CWD: `project_root` → `project_root.parent` 修复模块发现

### 已知限制
- Daemon 的同步事件循环在长时间操作（scan）期间阻塞。formalize dispatch 在 watcher 触发 rescan 时超时。
- LLM 调用需要配置环境变量或使用新的配置面板（推荐 Groq 免费层）。
- Auto-failover 功能已推迟到迭代计划中（参考 cc-switch 的 circuit breaker 模式）。

---

## 必读文件
- `docs/CLAUDE.md` — 完整项目指南（架构 + 模块布局 + 设计约束）
- `docs/VERSION.md` — 版本历史 + v0.30 详细条目
- `README.md` — 项目概览（架构图 + 能力表 + 快速开始）
- `docs/RuntimeConstitution.md` — Runtime 根本规则（8 条）
- `docs/StateLog_Design_Discussion.md` — StateLog 完整设计
- `docs/iterations/v0.30_Implementation_Plan.md` — v0.30 实施计划
- `docs/iterations/v0.30_Loop_Research.md` — Agent Loop 调研笔记
- `docs/iterations/v0.30_AgentProcessManager_Design.md` — AgentProcessManager 设计
- `cli/dashboard/docs/PROJECT.md` — Dashboard CLI 项目文档
- `memory/ink-dashboard-pitfalls.md` — Dashboard 重写踩坑记录

---

## 当前进度总览（v0.30）

| 区域 | 完成度 | 状态 |
|------|--------|------|
| Runtime Kernel | 100% | SyncSession + step orchestration ✅ |
| Event Bus | 100% | HistoryManager append-only governance event log ✅ |
| State Store | 100% | StateReader unified query + persistent files ✅ |
| Policy Engine | 100% | 可插拔策略（5 种 check）+ registry ✅ |
| Dispatch Layer | 90% | DaemonClient + daemon_registry + ToolDispatcher ✅（scan 超时已知） |
| Agent Loop | 80% | A→B fork + dispatch + LLM call ✅；auto-failover 推迟 |
| LLM Config | 100% | 后端 CRUD + MCP 工具 + Ink 终端面板 ✅ |
| 测试 | 100% | 334 passed / 24 测试文件 ✅ |
| Constitution | 100% | 8 条规则 + 三层状态机 ✅ |

## 执行优先级
1. **Auto-failover** — 参考 cc-switch circuit breaker 模式实现多 Provider 自动切换
2. **Dispatch 超时修复** — Daemon 异步事件循环（避免 scan 阻塞其他命令）
3. **真实 LLM 测试** — 配置 Groq 免费 API 验证端到端 A→B Agent 通路
4. **P0（GUI Track）** — 前端架构调整（B-1 + F-1），Qt GUI 长期搁置
