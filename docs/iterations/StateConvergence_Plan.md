# Gitgo State Convergence — 状态模型收敛迭代计划

> 2026-05-26 | 基于 RuntimeStateModel.md 诊断 | 不扩功能，只收拢

---

## 原则

本阶段**不新增任何子系统**。不添加 task memory、agent profiling、semantic replay、workflow scoring 等新概念。
只做一件事：让已有的 9 个子系统的状态语义统一，变更审计统一，持久化查询接口统一。

---

## 阶段结构

| Stage | 名称 | 核心产出 | 预估 |
|-------|------|---------|------|
| C1 | Governance Event Completeness | formal_commits 变更 + contract drift + lesson harvest 走 HistoryManager | 1 周 |
| C2 | Three-Layer State Distinction | Operational / Governance / Derived 三层显式区分的 status_dict | 0.5 周 |
| C3 | Unified State Query | 替代直接读文件路径的 StateReader | 0.5 周 |

---

## C1: Governance Event Completeness

### 问题

RuntimeStateModel §3.4 暴露：`FormalCommit` 的 `synced`/`pushed` 变更只在 `session.json` 中以可变字段存在，
**不下发到 HistoryManager** 作为不可变事件。Contract drift 检测结果是 transient 返回值，不入 history。
Lesson harvest 不入 history。

只有 identity warnings（`operation="integrity_warning"`）已经走 HistoryManager。这是一个正确的模式，
其他 governance state 变更应该跟上。

### 目标

让以下 governance state 变更全部作为不可变 history event 写入 HistoryManager：

| governance 变更 | 当前状态 | 目标 |
|---------------|---------|------|
| formal commit synced | 只在 session.json 更新 `synced=True` | 追加 `operation="governance_synced"` entry |
| formal commit pushed | 只在 session.json 更新 `pushed=True` | 追加 `operation="governance_pushed"` entry |
| formal commit dissolved | 只在 session.json 删除 | 追加 `operation="governance_dissolved"` entry |
| formal commit deleted | 已有 `operation="delete_formal"` | ✅ 已完成 |
| formal commit edited (message) | 无记录 | 追加 `operation="governance_edited"` entry |
| formal commit edited (number) | 无记录 | 追加 `operation="governance_renumbered"` entry |
| contract drift detected | transient 返回值 | 追加 `operation="governance_drift"` entry |
| contract updated (sync 后) | .gitgo/contract.yaml 覆盖 | 追加 `operation="governance_contract_updated"` entry |
| lesson harvested | 无记录 | 追加 `operation="governance_lesson"` entry |
| identity warning | 已有 `operation="integrity_warning"` | ✅ 已完成 |
| memory snapshot | 无记录 | 追加 `operation="governance_memory_snapshot"` entry |

### 实现方法

在每个 `step_*()` 方法的 governance state 变更发生后，追加一行 `HistoryManager.add_operation()` 调用。

```python
# sync_session.py — step_sync() 成功后，追加 governance event
if success:
    fc.synced = True
    HistoryManager.add_operation(
        self.project.name, "governance_synced",
        "success",
        {"commit": f"[{fc.prefix}-{fc.number}]"},
        correlation_id=self._correlation_id,
    )
```

各写入点：

| 位置 | 追加的 add_operation |
|------|---------------------|
| `step_sync()` 成功后 | `governance_synced` |
| `step_push()` 成功后 | `governance_pushed` (包含 `commits` 列表) |
| `step_dissolve_formal()` 成功后 | `governance_dissolved` |
| `step_edit_formal_message()` 成功后 | `governance_edited` |
| `step_edit_formal_number()` 成功后 | `governance_renumbered` |
| contract drift 检测到漂移时 | `governance_drift` |
| contract 自动更新后 | `governance_contract_updated` |
| lesson harvest 后 | `governance_lesson` (含 harvested_count) |
| memory snapshot 后 | `governance_memory_snapshot` |

### 认证标准

- [ ] 全部 9 种 governance event 写入 HistoryManager
- [ ] `gitgo history --op governance_synced --project X --json` 可查询
- [ ] governance/quality、governance/patterns、governance/graph 可从 event log 完整推导所有分析（不需要读 session.json）
- [ ] 现有 334 测试全绿

---

## C2: Three-Layer State Distinction

### 问题

RuntimeStateModel §3.1 暴露：`status_dict()` 把 Operational State（`stage`）、Governance State（`synced/pushed` 计数）、Derived State（`semantic` 块）混在同一个 JSON 输出里。Agent 无法区分"当前在执行什么"和"项目处于什么 phase"。

### 目标

`status_dict()` 输出增加显式的 `layer` 标识：

```json
{
  "project": "MyProject",
  "layers": {
    "operational": {
      "stage": "IDLE",
      "entries_total": 45
    },
    "governance": {
      "formal_total": 2,
      "formal_synced": 1,
      "formal_pushed": 0,
      "trial_pending": 3,
      "contract_drift": false,
      "identity_status": "ok"
    },
    "semantic": {
      "workspace_entropy": "medium",
      "suggested_next_action": "triage",
      "action_queue": ["triage", "formalize", "push"],
      "blocked_reason": null
    }
  }
}
```

三层定义：

| 层 | `layer` key | 内容 | 含义 |
|----|------------|------|------|
| Operational | `operational` | `stage`, `entries_total` | 当前在执行什么操作——瞬态，session 内有效 |
| Governance | `governance` | formal counts, trial pending, contract drift, identity status | 项目处于什么治理阶段——持久态，跨 session 有效 |
| Semantic | `semantic` | entropy, next_action, action_queue, blocked_reason | 从 governance 层计算出的 agent 可消费判断——派生态 |

### 向后兼容

旧 `--json` 输出格式保留，`--json --layered` 使用新格式。
旧格式的 5 个顶级 key（`project`, `stage`, `workspace`, `commits`, `trial`, `semantic`）不变。

### 认证标准

- [ ] `--json --layered` 输出含三层标识
- [ ] `operational.stage` 与 `SessionStage` 枚举一致
- [ ] `governance` 层字段全部来自持久化状态，不包含操作瞬态
- [ ] `semantic` 层字段全部为推导值，不包含原始计数
- [ ] 旧 `--json` 输出格式不变

---

## C3: Unified State Query

### 问题

RuntimeStateModel §4.2 暴露：6 个持久化位置（session.json / history.json / contract.yaml / abstract.jsonl / instance.jsonl / memories/），4 种格式（JSON / YAML / JSONL / 文件目录）。Subsystem 直接访问文件路径读取状态，没有统一的查询入口。

### 目标

新增 `backend/core/state_reader.py`，提供一个 `StateReader` 类封装所有持久化位置的读取逻辑：

```python
# backend/core/state_reader.py — 新文件，~80 行

class StateReader:
    """统一的治理状态查询接口。
    
    不引入新文件格式，不替代 HistoryManager 或 ConfigManager。
    所有方法从已有持久化文件读取，封装路径逻辑。
    """
    
    @staticmethod
    def get_formal_commits(project_name: str) -> list[dict]:
        """从 session.json + history.json 重建 formal commits 当前状态。
        
        session.json 提供当前 snapshot，history.json 中的 governance_synced/
        governance_pushed 等 event 提供变更审计。
        """
    
    @staticmethod
    def get_contract(workspace_path: str) -> ProjectContract | None:
        """从 .gitgo/contract.yaml 读取合约。"""
    
    @staticmethod
    def get_lessons(workspace_path: str, layer: str = "instance") -> list[dict]:
        """从 .gitgo/knowledge/{layer}.jsonl 读取 lesson。"""
    
    @staticmethod
    def get_integrity_warnings(project_name: str, limit: int = 20) -> list[dict]:
        """从 HistoryManager 查询 integrity_warning 记录。"""
    
    @staticmethod
    def get_memory_snapshots(backup_path: str) -> list[dict]:
        """列出 .gitgo/memories/ 下的快照。"""
```

### 改动范围

- 新增 `backend/core/state_reader.py`（~80 行）
- 现有 governance/* 和 CLI commands 的 `HistoryManager.load()` 调用改为 `StateReader.get_*()` —— 渐进式，不是一次性全部替换
- `contract.py` 的 `ContractManager` 已有读取逻辑，不需要改动——`StateReader.get_contract()` 是对它的薄包装

### 认证标准

- [ ] `StateReader.get_formal_commits("X")` 返回与 `SyncSession.formal_commits` 一致的列表
- [ ] `StateReader.get_contract(ws)` 返回 `ProjectContract` 对象
- [ ] `StateReader.get_integrity_warnings("X")` 返回列表
- [ ] 所有 `StateReader` 方法不需要 `SyncSession` 实例作为参数（可以独立于 workflow 调用）

---

## 不做的事

- 不建 event bus —— C1 是在已有的 HistoryManager 上加 event type，不改变 step_*() 的调用结构
- 不建 reactive pipeline —— C1-C3 是状态语义统一，不是架构重写
- 不拆 FormalCommit 为 immutable + mutable 两个对象 —— 复杂性收益比不够。synced/pushed 变更走 event log 就达到了审计可追溯的目的
- 不加新 CLI mode —— C2 的 `--layered` 是已有 `--json` flag 的扩展参数，C3 不新增 CLI
- 不改 SyncSession API —— 所有改动是内部实现变化

## 总计

| 文件 | 改动 | 行数 |
|------|------|------|
| `sync_session.py` | 9 个 step_*() 方法追加 `add_operation("governance_*")` | +30 |
| `status_dict()` | 新增 `--layered` 格式，旧格式不变 | +40 |
| `backend/core/state_reader.py` | 新建 | +80 |
| `__main__.py` | `--layered` flag | +5 |
| **总计** | | **~155 行** |

零新依赖。零新子系统。334 个测试全绿是前提条件。
