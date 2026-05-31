# Gitgo 项目最新进展报告

> 版本：v0.27 | 日期：2026-05-30 | 状态：后端 100% · 334 测试 · 43 MCP · 22 CLI

---

## 一、项目定位

**Gitgo 是一个 Development Semantic Runtime**——运行在 workspace 内部，治理 AI 协作开发过程中项目状态的合法性演化。

不是 Git GUI，不是 CI/CD，不是 agent 外挂。

### 核心机制

- **Gate A**：Agent 写完 → 人看到之前。contract / lesson / drift / identity 全部在此执行
- **Gate B**：人确认发布 → 代码进入 Canonical Release Space 之前。authorship / privacy / security
- **Policy Engine**：daemon 内置，watchdog 触发，不依赖 Agent 调用
- **Semantic Scheduler**：从 governance 层推导 `suggested_next_action`（输出建议，不驱动调度）

---

## 二、Runtime 四层

```
Runtime Kernel  →  SyncSession / step orchestration
Event Bus       →  HistoryManager (append-only governance event log)
State Store     →  session.json / contract.yaml / lessons.jsonl
Policy Engine   →  contract / identity / authorship / lesson / drift / privacy
```

---

## 三、能力矩阵

| 类别 | 内容 |
|------|------|
| **工作流** | scan · formalize · sync · push · trial · daemon · dashboard |
| **治理** | Identity Guard · Memory Snapshot · Project Contract · Lesson System · Authorship · Discipline |
| **Governance Event** | 9 种（synced/pushed/dissolved/edited/renumbered/drift/contract/lesson/snapshot） |
| **Lesson 系统** | 抽象层 + 实例层，CLAUDE.md + git log + governance signals 三源自收割 |
| **Drift 检测** | Gate A 阻塞：功能删除 / 签名丢失 / 架构违反；Gate B：authorship / 隐私 / 安全 |
| **模板** | commit-config.json 多套模板，str.format() 8 变量 |
| **Dashboard** | Rich 实时面板，多项目 Gate A 状态 + governance event |

---

## 四、测试

| 指标 | 数值 |
|------|------|
| 测试文件 | 24 |
| 测试函数 | 335 |
| 通过 | 334 |
| 跳过 | 1（需网络） |
| 失败 | 0 |

---

## 五、迭代历史

| 版本 | 日期 | 里程碑 |
|------|------|--------|
| v0.10–v0.20 | 2026-05 | P1–P4: Foundation + Governance |
| v0.21 | 05-16 | P5: Protocol & Ecosystem |
| v0.22 | 05-17 | Template + SMB + CLI/MCP |
| v0.23 | 05-19 | Identity Guard |
| v0.24 | 05-19 | Authorship + Contract + Lesson |
| v0.25 | 05-29 | State Convergence (C1–C3) |
| v0.26 | 05-29 | Runtime Discipline + Lint |
| **v0.27** | **05-30** | **Constitution + Policy Engine daemon + Dashboard + MCP gate** |

---

## 六、唯一待办

| 优先级 | 内容 |
|--------|------|
| P0 | GUI Track — 前端架构调整 |
| P1 | Policy Engine 统一接口 (Policy.check) |
| P2 | Agent 框架 hook 依赖（非 gitgo 可控） |
