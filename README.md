# Gitgo

**Development Semantic Runtime** — AI 协作开发过程中的项目状态治理系统。

334 个测试 | 43 个 MCP 工具 | 22 个 CLI 模式 | v0.27

---

## 不是什么

不是一个 Git GUI。不是一个 CI/CD。不是一个 agent 外挂。

**是一个运行在 workspace 内部的状态治理层**——在 Agent 写完代码和代码被发布之间，提供不可跳过的合法性边界。

---

## 架构

```mermaid
%%{init: {'theme':'base', 'themeVariables': {
  'primaryTextColor':'#000',
  'primaryColor':'#f0f0f0',
  'lineColor':'#000000',
  'secondaryColor':'#e0e0e0',
  'tertiaryColor':'#fff',
  'clusterBkg':'#f9f9f9',
  'clusterBorder':'#000',
  'edgeLabelBackground':'#ffffff'
}}}%%
flowchart TB
    subgraph RuntimeKernel["Runtime Kernel (调度层)"]
        Kernel["SyncSession / step orchestration<br>state transition / lifecycle control"]
        Scheduler["Semantic Scheduler<br>next_action / action_queue"]
    end

    subgraph EventBus["Event Bus (信号层)"]
        HistoryManager["HistoryManager<br>append-only governance event log"]
        Events["pre_scan | post_sync | drift_detected<br>governance_synced | governance_pushed"]
    end

    subgraph StateStore["State Store (状态层)"]
        RuntimeState["RuntimeState<br>operational / governance / semantic<br>integrity / memory / lessons / releases"]
        Persistent["session.json | contract.yaml<br>lessons.jsonl | formal commits"]
    end

    subgraph PolicyEngine["Policy Engine (规则层)"]
        Contract["contract validation"]
        Identity["identity guard"]
        Authorship["authorship cleanup"]
        Lesson["lesson inheritance"]
        Drift["drift detection"]
        Privacy["privacy scan / secret detection"]
    end

    subgraph External["外部世界"]
        GitHub[("GitHub / GitLab")]
        Human["用户 / Reviewer"]
    end

    subgraph Trial["Trial Space (物理测试区)"]
        Incoming["incoming/*<br>外部代码唯一入口"]
        Triage{"triage()"}
        Discard["discard (标记已读)"]
    end

    subgraph Workspace["Mutable Workspace (工作区)"]
        AgentDev["Agent 日常开发<br>自由 commit / 实验"]
        GateA{"Gate A<br>语义合法性边界"}
        GateA_fail["不通过 → Agent 原地改"]
        UserGateA["用户审查 / 更新检查集"]
        UserPublish["用户确认发布"]
    end

    subgraph Validated["Validated State (逻辑边界内)"]
        direction LR
        AfterGateA["已通过 Gate A 的状态"]
    end

    subgraph GateB_Check["Gate B 发布合法性"]
        GateB{"Gate B<br>发布合法性边界"}
        GateB_fail["发布拒绝 → 返回 Workspace"]
        UserConfirm["用户确认发布摘要"]
    end

    subgraph Release["Canonical Release Space (物理备份区)"]
        CanonicalState[("Canonical State<br>formal commits / immutable<br>event log / contract / lessons")]
        PublishToGitHub["发布到 GitHub"]
    end

    %% 外部 → Trial
    GitHub --> Incoming
    Human -- PR/接手项目 --> Incoming
    Incoming --> Triage
    Triage -- discard --> Discard
    Triage -- accept --> GateB
    Triage -- promote --> AgentDev

    %% Workspace → Gate A → Validated
    AgentDev --> GateA
    GateA -- 不通过 --> GateA_fail
    GateA_fail --> AgentDev
    GateA -- 通过 --> AfterGateA
    AfterGateA --> UserGateA
    UserGateA -- 需要修改 --> AgentDev
    UserGateA -- 满意准备发布 --> UserPublish

    %% Validated → Gate B
    UserPublish --> GateB
    GateB -- 通过 --> UserConfirm
    GateB -- 不通过 --> GateB_fail
    GateB_fail --> AgentDev
    UserConfirm --> CanonicalState

    %% Canonical → 外部
    CanonicalState --> PublishToGitHub
    PublishToGitHub --> GitHub

    %% 治理信号闭环
    GateA -.-> Events
    GateB -.-> Events
    Events -.-> HistoryManager
    HistoryManager -.-> RuntimeState
    RuntimeState -.-> Kernel

    %% Policy Engine 接入
    PolicyEngine -.-> GateA
    PolicyEngine -.-> GateB

    %% Scheduler
    Scheduler -.-> AgentDev

    %% 用户反馈
    Human -- 更新 contract / lesson --> PolicyEngine

    classDef boundary fill:#FFF9C4,stroke:#FBC02D,stroke-width:2px,stroke-dasharray:5 5,color:#000
    classDef kernel fill:#E1F5FE,stroke:#0277BD,color:#000
    classDef physical fill:#E8F5E9,stroke:#388E3C,color:#000
    classDef external fill:#F5F5F5,stroke:#9E9E9E,color:#000
    classDef gate fill:#FFE0B2,stroke:#F57C00,color:#000
    classDef event fill:#D1C4E9,stroke:#512DA8,color:#000
    class Workspace,Validated,GateB_Check boundary
    class RuntimeKernel,EventBus,StateStore,PolicyEngine kernel
    class Trial,Workspace,Release physical
    class External,Human,GitHub external
    class GateA,GateB gate
    class Events,HistoryManager event
```

---

## 能力

### 工作流

| 功能 | CLI | MCP |
|------|-----|-----|
| `scan` — 文件变更扫描（SHA256 + EOL 归一化） | ✅ | ✅ |
| `formalize` — workspace commit → formal commit 聚合 | ✅ | ✅ |
| `sync` — 同步到 release 仓库（Gate A 拦截） | ✅ | ✅ |
| `push` — 推送至 GitHub（Gate B） | ✅ | ✅ |
| `trial` — 外部代码 triage（accept/promote/discard） | ✅ | ✅ |
| `daemon` — 持久守护进程（watchdog + trial 轮询 + Policy Engine） | ✅ | — |
| `dashboard` — 实时项目状态面板 | ✅ | — |

### 治理

| 系统 | 说明 |
|------|------|
| **Identity Guard** | 全量覆盖检测 / 身份文件删除告警 / 目录骨架崩塌检测 |
| **Memory Snapshot** | sync 时自动快照 `.claude/` `.codex/` `.codebuddy/` → backup |
| **Project Contract** | 项目合约自动维护 + push 前漂移检测（功能删除/技术栈漂移/架构违反） |
| **Lesson System** | 抽象层+实例层知识传承，sync 后自动收割（CLAUDE.md + git log + governance signals） |
| **Authorship** | push 前 AI 痕迹清洗（commit message + 代码注释 + AI 配置文件排除） |
| **Template System** | 多套命名 commit message 模板，`str.format()` 8 变量填充 |
| **Discipline** | 8 条 Runtime Constitution 规则 + 三层状态机 |

### 9 种 Governance Event

`governance_synced` / `governance_pushed` / `governance_dissolved` / `governance_edited` / `governance_renumbered` / `governance_drift` / `governance_contract_updated` / `governance_lesson` / `governance_memory_snapshot`

全部写入 append-only HistoryManager。

---

## 快速开始

```bash
pip install -r requirements.txt

# 自举配置（把 gitgo 自己注册为项目）
python -m gitgo --mode bootstrap

# 实时 dashboard
python -m gitgo --mode dashboard --refresh 5

# Agent 交付检查
# MCP: gitgo_round_complete(project="myproject")

# 发布
python -m gitgo --mode push --project myproject --strip-authorship
```

---

## 项目结构

```
gitgo/
├── backend/                   # 引擎层
│   ├── core/                  #   Runtime Kernel + State Store + Policy Engine
│   │   ├── sync_session.py    #   状态机（18 step_* 方法）
│   │   ├── state_reader.py    #   统一状态查询
│   │   ├── config.py          #   项目配置
│   │   ├── history.py         #   HistoryManager (append-only event log)
│   │   ├── governance/        #   quality / patterns / graph / releases
│   │   ├── identity/          #   guard (完整性检测) + snapshot (记忆快照)
│   │   ├── knowledge/         #   lesson (知识传承)
│   │   ├── operations/        #   scan / git / sync / security / diff
│   │   ├── daemon/            #   持久守护进程
│   │   ├── contract.py        #   项目合约 + 漂移检测
│   │   ├── authorship.py      #   AI 痕迹清洗
│   │   └── template_manager.py #  commit 模板系统
│   ├── adapters/              #   Local / SSH / SMB 三实现
│   ├── models/                #   数据模型
│   └── remote/                #   GitHub / GitLab API
├── cli/                       # CLI verbs + dashboard
├── mcp_server.py              # FastMCP server (43 tools)
├── frontend/                  # Qt GUI
├── cui/                       # Rich 终端界面
├── tests/                     # 334 测试 + 1 skip
└── docs/                      # RuntimeConstitution / HANDOFF / VERSION
```

---

## 版本

| 版本 | 里程碑 |
|------|--------|
| v0.10–v0.20 | P1–P4: Foundation + Governance |
| v0.21 | P5: Protocol & Ecosystem |
| v0.22 | P6: Template + SMB + CLI/MCP |
| v0.23 | Identity Guard |
| v0.24 | Authorship + Contract + Lesson |
| v0.25 | State Convergence (C1–C3) |
| v0.26 | Runtime Discipline |
| **v0.27** | **Constitution + Policy Engine daemon + Dashboard + MCP gate** |
