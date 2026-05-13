# Gitgo Governance State Machine

> gitgo 的治理层状态机 — 描述变更单元从"高熵开发"到"已发布"的生命周期。

---

## 双层状态体系

Gitgo 有两层状态，服务于不同消费者：

| 层 | 枚举 | 位置 | 描述 |
|----|------|------|------|
| **Operational** | `SessionStage` (10 states) | `core/sync_session.py` | 系统当前在执行什么操作 |
| **Governance** | Governance state (6 states) | 本文件 + 从字段计算 | 变更单元处于生命周期的哪个治理阶段 |

两者是互补的，**不混在同一个数据结构里**。Governance state 从 SyncSession 现有字段（entries/commits/formal_commits 的 synced/pushed + trial incoming 状态）**计算得出**。

---

## Governance 状态定义

### workspace（高熵开发区）

变更由开发者或 AI 在 workspace 中自由产生。无约束，无编号，无正式 message 要求。

**状态特征：**
- 文件可任意增删改
- Commit 格式无要求
- 可以是非 Conventional Commits

### trial（待治理输入层）

变更已进入 trial 仓库，等待审查和决策。关键安全约束：**不可直接从 trial 进入 published**。

**状态特征：**
- IncomingChange 列表填充
- 每个 change 的 triage 为 PENDING
- 需要人类或 agent 做出三叉决策

### curated（trial 已决策）

trial 中的变更已经过三叉决策（accept / promote / discard）。

**状态特征：**
- 所有 PENDING 的 IncomingChange 已被处理
- accepted → 已 cherry-pick 到 release，等待 formal commit 整合
- promoted → 已 fetch 到 workspace incoming/* 分支
- discarded → 已标记忽略
- `processed_incoming` 字典已持久化

### formalized（语义单元已建立）

变更已整合为 formal commit（有编号、有语义分组、有结构化的 message）。尚未 sync 到 release 仓库。

**状态特征：**
- `formal_commits` 列表中至少有一个条目
- 该 formal commit 的 `synced=False`
- 来自 `step_create_formal_commit()` 成功调用

### release_ready（已同步到正式仓库）

Formal commit 已 sync 到 release 仓库（备份仓库已有该 commit）。尚未 push 到远程。

**状态特征：**
- `formal_commits` 中至少有一个 `synced=True, pushed=False`
- 备份仓库的 HEAD 包含该 commit
- `sync_base` 已更新

### published（已发布，不可逆）

Formal commit 已 push 到远程仓库。**这是 governance 的最终态，不可逆。**

**状态特征：**
- `formal_commits` 中 `synced=True, pushed=True`
- 远程仓库已包含该 commit

---

## 合法转移

```
workspace  ──→ trial            (promote: git fetch trial → workspace incoming/*)
workspace  ──→ formalized       (formalize: 选中 workspace commit → formal commit)
trial      ──→ curated          (accept / promote / discard 三叉决策)
curated    ──→ formalized       (accept 的变更整合为 formal commit)
formalized ──→ release_ready    (sync: 同步到 release 仓库)
release_ready → published       (push: 推送到远程)
```

## 非法转移

| 非法转移 | error code | 错误消息 |
|----------|-----------|---------|
| trial → published | `TRIAL_CANNOT_PUBLISH` | "trial cannot publish directly — must accept and formalize first" |
| workspace → published | `NO_FORMALIZED_BOUNDARY` | "no formalized boundary — must formalize first" |
| workspace → release_ready | `NO_FORMALIZED_BOUNDARY` | "no formalized boundary — must formalize first" |
| formalized → published | `MUST_SYNC_BEFORE_PUBLISH` | "must sync before publish" |
| curated → published | `MUST_FORMALIZE_AFTER_ACCEPT` | "must formalize after accept" |
| release_ready → release_ready | `NO_SYNCED_COMMITS` | "no synced formal commits to push" |

## 转移守卫实现位置

- **`step_push()`** → 检查 formal commit 是否已 synced（`if fc.synced and not fc.pushed`），已有
- **`step_triage_incoming("accept")`** → 检查 release 仓库已配置，已有
- **`_cmd_push()` in `__main__.py`** → CLI 层前置检查 `NO_SYNCED_COMMITS`，已实现
- **`step_create_formal_commit()`** → 检查有 workspace commit 可选，已有（返回 None）
- **未来：** `_cmd_push()` 可在 CLI 层增加更多 governance guard 检查

---

## 转移图

```
                    ┌─────────┐
                    │ 外部输入  │
                    └────┬────┘
                         │
                         ▼
                   ┌──────────┐
                   │  TRIAL    │ (待治理输入层)
                   │  审查·决策  │
                   └────┬─────┘
                        │ 三叉决策
              ┌─────────┼─────────┐
              │accept   │promote  │discard
              ▼         ▼         ▼
              │    ┌─────────┐    │
              │    │WORKSPACE │    │
              │    │ (继续开发) │    │
              │    └─────────┘    │
              │         │         │
              ▼         │         │
         ┌─────────┐    │         │
         │ CURATED  │    │         │
         │ (已决策)  │    │         │
         └────┬─────┘    │         │
              │          │         │
              ▼          │         │
       ┌───────────┐     │         │
       │ FORMALIZED │◄────┘         │
       │  (语义单元) │               │
       └─────┬─────┘               │
             │                     │
             ▼                     │
      ┌─────────────┐              │
      │RELEASE_READY│              │
      │  (已同步)    │              │
      └──────┬──────┘              │
             │                     │
             ▼                     │
      ┌──────────┐                 │
      │PUBLISHED  │                 │
      │(已发布·终态)│                 │
      └──────────┘                 │
                                   │
        WORKSPACE ──→ formalize ───┘
           ↑
           │ 自由开发（高熵）
```

---

## Agent 使用指南

Agent 可通过以下 CLI 命令查询 governance state：

```bash
gitgo status --project X --json
```

输出中的关键字段映射到 governance state：

| JSON 字段 | Governance 含义 |
|-----------|----------------|
| `commits.formal_total == 0` | 处于 workspace 阶段 |
| `trial.pending > 0` | 有待处理的 trial incoming |
| `commits.formal_total > 0 && formal_synced == 0` | 处于 formalized 阶段 |
| `commits.formal_synced > 0 && formal_pushed == 0` | 处于 release_ready 阶段 |
| `commits.formal_synced == commits.formal_pushed > 0` | 处于 published 阶段 |

Agent 操作：

```bash
gitgo trial list --project X --json      # 查看 trial incoming
gitgo trial accept --project X --index N --json   # accept → curated
gitgo formalize --project X --json       # workspace → formalized
gitgo scan --project X --json            # 查看变更（不同步）
gitgo push --project X --json            # release_ready → published
```
