# Gitgo Runtime Constitution

> 版本：v0.27 | 基于 v0.26 RuntimeDiscipline | 显式化已有结构

---

## 定位

本文档声明 gitgo Runtime 的根本规则。不是"将来应该做"，而是"现在就是这样做的，违反即 bug"。

v0.26 的 RuntimeDiscipline 覆盖了 Canonical Events / State Authority / Derivation Rules / Observer Constraint 四条。
v0.27 补全 Layer Mutation / Gate Extension / Semantic Reversibility / Event Taxonomy 四条。共八条。

---

## 1. Canonical Events

9 种 governance event 是闭集。新增 governance event 类型必须修改本文档。

| Event | 位置 | 触发 |
|-------|------|------|
| `governance_synced` | `sync_session.py:816` | `step_sync()` 成功，Gate A 通过 |
| `governance_pushed` | `sync_session.py:945` | `step_push()` 成功，Gate B 通过 |
| `governance_dissolved` | `sync_session.py:730` | `step_dissolve_formal()` 成功 |
| `governance_edited` | `sync_session.py:673` | `step_edit_formal_message()` 成功 |
| `governance_renumbered` | `sync_session.py:702` | `step_edit_formal_number()` 成功 |
| `governance_drift` | `sync_session.py:802` | `step_sync()` 中 Gate A 漂移检测 |
| `governance_contract_updated` | `sync_session.py:845` | 合约自动更新后 |
| `governance_lesson` | `sync_session.py:856` | lesson harvest 后 |
| `governance_memory_snapshot` | `sync_session.py:829` | memory snapshot 后 |

操作级 event（`scan`/`formalize`/`sync`/`push`/`triage_*`/`delete_formal`/`dissolve_formal`）是独立的 workflow log——不与 governance event 混用。

区分标准：
- governance event 的 detail 包含**状态变更内容**（如 `{"commit": "[MYAPP-1]"}`、`{"commits": [...]}`）
- 操作级 event 的 detail 包含**操作范围**（如 `{"entries_total": 45}`、`{"file_count": 12}`）

---

## 2. State Authority

每个持久化 state 有且仅有一个写入源。

| State 字段 | Authority | 禁止直接写入者 |
|-----------|----------|---------------|
| `formal_commits[].synced` | `step_sync()` | 任何其他方法 |
| `formal_commits[].pushed` | `step_push()` | 任何其他方法 |
| `formal_commits[].message` | `step_edit_formal_message()` | 直接赋值 |
| `formal_commits[].number` | `step_edit_formal_number()` | 直接赋值 |
| `contract.tech_stack` | `ContractManager` | 直接读写 contract.yaml |
| `contract.decided_features` | `ContractManager` | 直接读写 contract.yaml |
| `*.jsonl` (lesson) | `LessonManager` | 直接读写文件 |
| `.gitgo/memories/` | `snapshot_tool_memories()` | 直接读写文件 |
| `gitgo_history.json` | `HistoryManager.add_operation/add_entry` | 直接读写文件 |

违反 authority 的代码是 bug——即使测试通过，它造成了"同一个 state 被两个不协调的写入者修改"的语义风险。

---

## 3. Derivation Rules

Semantic 层的所有字段从 operational + governance 层数据计算，**不能引入新的持久化位置**。

| Semantic 字段 | 计算来源 | 持久化 |
|--------------|---------|--------|
| `workspace_entropy` | `entries_changed`（operational） | 否 |
| `suggested_next_action` | `trial_pending + entries_changed + formal_synced + formal_pushed` | 否 |
| `action_queue` | 优先级链: triage > formalize > push | 否 |
| `blocked_reason` | `formal_synced vs formal_pushed + entries_changed` | 否 |
| `trial_requires_review` | `trial_pending > 0` | 否 |
| `safe_to_formalize` | `entries_changed > 0 and stage == IDLE` | 否 |
| `safe_to_publish` | `formal_synced > 0 and formal_synced > formal_pushed` | 否 |
| governance/quality | `HistoryManager suggest_* entries` | 否 |
| governance/patterns | `HistoryManager formalize entries` | 否 |
| governance/graph | `HistoryManager formalize/triage_accept/push entries` | 否 |

---

## 4. Observer Constraint

当前没有 observer 链。`step_*()` 使用硬编码调用序列。在显式解决 observer 循环检测之前，**不引入 event-driven dispatch**。硬编码调用序列是天然的循环安全阀。

---

## 5. Layer Mutation Rules

```
mutability:
  operational layer:  mutable, transient, session-scoped
  governance layer:   append-only, cross-session
  semantic layer:     pure derivation, never persisted

具体规则:
  - operational 层的任何字段可以随 step_*() 执行而改变
  - governance 层的任何字段只能通过 HistoryManager 追加（append-only）
  - semantic 层的任何字段不能写入 session.json 或任何持久化文件
  - 如果在 session.json 中发现 semantic 字段，这是 bug
```

---

## 6. Gate Extension Policy

```
Gate A (Semantic Legitimacy) 可扩展规则:
  - 扩展方式: 新增 Policy 模块
  - Policy 必须由 Runtime Kernel 在 transition 前调用
  - 不允许: Policy 直接调用 step_*() 或修改 formal_commits

Gate B (Publication Legitimacy) 可扩展规则:
  - 扩展方式: 同上
  - 不允许: Policy 在 Gate B 阶段修改代码内容（只能检查/清洗）
```

---

## 7. Semantic Reversibility

```
semantic 层的字段不具备可逆性:
  - suggested_next_action 是瞬时推导，不保证跨 session 一致
  - workspace_entropy 随 entries 变化，历史值不保留
  - 如果需要历史 semantic 状态，从 governance event log 重新推导

canonical state 具备可逆性:
  - 进入 Canonical Release Space 的 formal commit 不可删除（只能 dissolve 回 workspace）
  - dissolve 本身产生 governance_dissolved event，不消除历史
```

---

## 8. Event Taxonomy

所有 event 归类为 6 种之一：

| 类型 | 含义 | 示例 |
|------|------|------|
| `operational` | workflow 生命周期 | scan, formalize, sync, push, triage_*, delete_formal, dissolve_formal |
| `governance` | 长期治理变化 | governance_synced, governance_pushed, governance_dissolved, governance_edited, governance_renumbered |
| `integrity` | 连续性警告 | integrity_warning |
| `publication` | 发布合法性 | governance_drift, governance_contract_updated |
| `knowledge` | lesson/contract 演化 | governance_lesson, governance_memory_snapshot |
| `discipline` | 约束违反 | discipline_violation |

新增 event 必须:
1. 在本文档的 taxonomy 中声明类型
2. detail 字段包含状态变更内容（governance）或操作范围（operational）
3. 不能同时属于两个类型

---

## 三层状态机

### Layer 1: Operational State Machine

代码位置: `sync_session.py`, `SessionStage` enum

```
IDLE → SCANNING → SELECTING → COMMITTING → SYNCING → PUSHING → IDLE
          ↘ TRIAL_CHECKING → TRIAL_REVIEWING → INCOMING_CONFIRMING
```

18 个 `step_*()` 方法驱动转移。禁止直接修改 `self.stage`。

### Layer 2: Semantic State Machine

代码中不存在显式 enum。由 Gate A / Gate B 的检查逻辑隐式定义。

```
Workspace State → Gate A → Validated State → Gate B → Canonical State
```

- Gate A: `step_sync()` 中 `sync_to_backup()` 调用前。Policy Engine 全部 policy 检查。
- Gate B: `step_push()` 中 `push_to_backup()` 调用前。Authorship / Privacy / Security 检查。

### Layer 3: Governance State Machine

分布在多个 subsystem 中，无统一 enum。

```
Formal Commit: created → synced → pushed
                       ↘ dissolved

Trial:         incoming → pending → accepted / promoted / discarded

Contract:      feature introduced → confirmed (N times)
```
