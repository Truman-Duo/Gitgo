# 迭代计划

> 更新日期：2026-07-05

---

## 当前状态（v0.30）

### 已完成

- **Phase 1 Runtime Foundation** — P1-A~P1-E ✅（v0.10–v0.11）
- **Phase 2 Agent-Ready Runtime** — P2-A~P2-D ✅（v0.12–v0.13）
  - Semantic State + Streaming / Unified History / Persistent Daemon / MCP Server
- **Phase 3 AI-Augmented Workflow** — P3-A~P3-D ✅（v0.15）
  - Triage Hook + Suggest CLI + AI Protocol + Commit Proposal + Diff Summary
- **Phase 4 Governance Layer** — P4-Pre~P4-D ✅（v0.16–v0.20）
  - Quality Metrics + Change Patterns + Semantic Graph + Release Reasoning
- **Phase 5 Protocol & Ecosystem** — P5-A~P5-D ✅（v0.21）
  - Protocol v1.0 + Reference Agent + Plugin API + State Bundle
- **Phase 6 Embedded Phase Gate** — ✅（v0.22–v0.25）
  - Identity + Authorship + Contract + Lesson + Bootstrap + State Convergence
- **v0.28 Dashboard 异构重写** — Python Rich → TypeScript Ink ✅
- **v0.29 Policy Engine 可插拔 + Governance Tab** ✅
- **v0.30 Dispatch Layer + LLM Config** ✅
  - DaemonClient（subprocess 通信）+ daemon_registry
  - LLMConfigManager + 4 MCP tools + Ink 终端 Provider 面板
  - Agent Loop A→B 通路真实化

### 当前进行中

_无活跃迭代。_

### 待启动

- **Auto-failover** — 参考 cc-switch circuit breaker 模式，多 Provider 自动切换
- **Dispatch 超时修复** — Daemon 异步事件循环（避免 scan 阻塞其他命令）
- **P0 GUI Track** — Qt GUI 长期搁置

---

## v0.30 新增文档

- `docs/iterations/v0.30_Implementation_Plan.md` — Dispatch + LLM Config 实施计划
- `docs/iterations/v0.30_Loop_Research.md` — Agent Loop 调研笔记
- `docs/iterations/v0.30_AgentProcessManager_Design.md` — AgentProcessManager 设计
- `docs/iterations/v0.30_CC_Instructions.md` — Claude Code 使用说明
- `docs/iterations/v0.30_Deferred_Designs.md` — 推迟的设计（auto-failover 等）

---

## 架构总览

```
git_url + file_access → RepoNode
                         ├── workspace (高熵开发区，Daemon 持续监控)
                         ├── release  (结构化历史，Gate A/B 拦截)
                         └── trial    (待治理输入) ──→ IncomingChange
                                                        ├── accept  → release (cherry-pick)
                                                        ├── promote → workspace (incoming/*)
                                                        └── discard → 忽略

Dispatch Layer (v0.30):
  MCP Tool → DaemonClient → daemon stdin JSON → main loop → ToolDispatcher
             ↑                                    ↓
             └──── daemon stdout JSON ←──────────┘

Agent Loop (v0.30):
  A-level MCP → fork_agent → AgentProcessManager → B-level subprocess
                  → dispatch_tool (scan/sync/push)
                  → llm_call → LLMProvider (OpenAI-compatible API)

适配器模式:
  FileAdapter (ABC)          GitRunner (ABC)
   ├── LocalFileAdapter       ├── LocalGitRunner
   ├── SSHFileAdapter         └── SSHGitRunner
   └── SMBFileAdapter

共享状态机 (core/sync_session.py):
  Operational: IDLE → TRIAL_CHECKING → SCANNING → SELECTING → ...
  Governance:  workspace → trial → curated → formalized → release_ready → published

前端: Ink Dashboard (TypeScript + Bun) / Qt GUI (搁置) / CLI
```

## 已完成迭代归档

全部已完成迭代的详细设计文档见 `docs/iterations/archive/`。
