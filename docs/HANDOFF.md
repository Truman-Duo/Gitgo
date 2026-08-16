# gitgo v0.41 交接文档

> 日期：2026-08-17

---

## 当前状态

**v0.41 架构阶段完成。** 自 v0.35（Knowledge System 三期）以来，按功能主题推进了 6 个版本（v0.36–v0.41）。其中 **v0.36 上下文管理已落地**（代码 + 38 测试）；**v0.37–v0.41 为架构阶段**——架构设计 + 骨架代码已就位，具体子任务仍在迭代计划中待实施。

### 版本一览

| 版本 | 主题 | 状态 |
|---|---|---|
| v0.36 | 上下文管理：九层 Context + 压缩链 + Assembler/Transcript | 已落地（38 测试 → 539） |
| v0.37 | 多 Agent 运行时：Actor Model + 结构化通信 + 契约驱动 | 架构 |
| v0.38 | 完成判断：任务完成语义判定 | 架构 |
| v0.39 | 错误恢复：四维分类 + 事务回滚 + 重试引擎 | 架构（40 测试 → 579） |
| v0.40 | 流式响应：流式事件管线 | 架构 |
| v0.41 | 前端工作：组件矩阵 + god module 解耦收尾 | 架构 |

### 当前工作流

- **Agent**: 每轮结束调 MCP `gitgo_round_complete("project")` → Gate A 检查 → passed 继续 / blocked 修复
- **Agent Loop**: 多 Agent 架构（Scheduler 编排 TaskSlot）——基础设施已就位，端到端接线待迭代
- **人**: `cd cli/dashboard && bun run src/main.tsx` 实时查看所有项目状态
- **LLM 配置**: Dashboard 内打开 ConfigPanel（Providers / Publish / Bin 多标签）
- **Daemon**: 后台 Policy Engine 持续扫描 + 积累 governance event（可选）
- **Release**: `gitgo push --project X --strip-authorship` 手动发布

### 模块结构（v0.41 现状）

- `backend/core/loop/` — Agent 循环（**已大幅扩展**）：
  - 上下文管理：`context_window` / `signals` / `signal_normalizer` / `signal_bus` / `session` / `transcript` / `harness/`（completion / pre_dispatch / registry / retention / tool_history）
  - 多 Agent：`scheduler` / `task_slot` / `decomposition` / `interface_contract` / `event_bus` / `execution_context` / `llm_adapter` / `agent_tool`
  - 执行：`tool_pipeline` / `tool_execution` / `tool_wrappers` / `process_tool_runner` / `executor` / `llm` / `manager`
  - 可靠性：`error_taxonomy` / `loop_guard` / `gate` / `task_gate`
- `backend/core/tools/` — 工具注册中心（`registrations` + `runner`）
- `backend/core/daemon/` — **已拆包**：`dispatch.py`（`COMMAND_HANDLERS` registry）+ `executors` / `emit` / `persist` / `cleanup` / `pidfile` / `policy_helpers`
- `backend/core/sync_session/` — **已拆包**：`base` / `commit` / `finalize` / `scan` / `syncpush` / `triage` / `persist` / `hooks` / `models` / `session`
- `backend/core/policy/` — Policy Engine（contract / dependency / identity / lessons / registry）
- `backend/core/knowledge/` — Knowledge System（harvest / recall / models / manager）
- `backend/core/cache/` — 文件哈希缓存 + `.stats()` 观测
- `cli/dashboard/src/` — 前端模块化：
  - `components/config/`（ProvidersTab / PublishTab / BinTab）
  - `input/overlays/`（13 个 overlay 子模块）
  - `mock/`（11 个数据域）
  - `chat/` / `effects/` / `daemon/`（流式事件管线）

### 已知限制 / 待实施子任务

- **多 Agent**：Phase 2 数据驱动迭代未实施（SSHBackend / RemoteLLMBackend / Escalate 多分支 / DAG 深度可配置）——见 `multi-agent-architecture.md` §十二
- **错误恢复**：Rust PyO3 ToolRunner（6 个月后评估）、遥测持久化（P3）延期——见 `error-recovery-architecture.md` §十五
- **流式响应**：流式事件管线骨架就位，后端 LLM 流式 → 前端渲染端到端接通待迭代
- **前端**：面板交互子任务待迭代
- **推送**：本地 `master` 领先 `origin/master` 18 commit，尚未推送（推送由 gitgo MCP server 接管，本地不 push）

---

## 必读文件

- `docs/CLAUDE.md` — 完整项目指南（架构 + 模块布局 + 设计约束 + 分支与工作流）
- `docs/VERSION.md` — 版本历史（含 v0.36–v0.41 详细条目）
- `README.md` — 项目概览（架构图 + 能力表 + 快速开始）
- `docs/technical-reports/context-management-architecture.md` — 上下文管理架构（v0.36）
- `docs/technical-reports/multi-agent-architecture.md` — 多 Agent 执行架构（Phase 1/2/3）
- `docs/technical-reports/error-recovery-architecture.md` — 错误恢复架构（含延期项 §十五）
- `docs/technical-reports/error-recovery-survey.md` — 跨项目错误恢复调研
- `docs/technical-reports/runtime-constitution.md` — Runtime 宪法级约束（7 条）
- `cli/dashboard/docs/PROJECT.md` — Dashboard CLI 项目文档

---

## 当前进度总览（v0.41）

| 区域 | 完成度 | 状态 |
|------|--------|------|
| Runtime Kernel | 100% | SyncSession 拆包 + step orchestration ✅ |
| Policy Engine | 100% | 可插拔策略 + registry ✅ |
| Context Management | 100% | 九层 Context + 压缩链 + Assembler/Transcript ✅（38 测试） |
| Multi-Agent 运行时 | 架构 | Scheduler + TaskSlot + InterfaceContract 骨架就位；Phase 2 待迭代 |
| Completion Judgment | 架构 | 完成判定骨架（loop_guard / gate / task_gate）就位 |
| Error Recovery | 架构 | error_taxonomy + 事务回滚 + 重试引擎骨架（40 测试）；延期项待评估 |
| 流式响应 | 架构 | streamEvents / StreamingMessage 骨架就位 |
| 前端工作 | 架构 | 组件矩阵 + config / input / mock 拆包完成 |
| LLM Config | 100% | 多标签面板（Providers / Publish / Bin）✅ |
| 测试 | 100% | 579 passed / 1 skipped ✅ |

## 执行优先级

1. **多 Agent 端到端接线** — Scheduler / TaskSlot 与 daemon 真实通路接通
2. **流式响应端到端** — 后端 LLM 流式 → 前端 StreamingMessage 渲染
3. **完成判断策略细化** — BUSINESS 结果判定策略落地
4. **错误恢复 P1 项** — LLM 重试引擎 + 会话持久化（见 `error-recovery-architecture.md` §十八）
5. **推送 18 commit** — 由 gitgo MCP server 接管（本地不 push）
