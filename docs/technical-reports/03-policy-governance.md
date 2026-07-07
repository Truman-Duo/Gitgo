# 报告三：Policy Engine 与治理管线深度解析

> gitgo v0.33 | 2026-07-07 | 完全透底技术报告

---

## 概述

Policy Engine 是 gitgo 的**实时规则检查系统**。在 daemon 检测到文件变更后，Policy Engine 立即运行 4 个 PolicyCheck，结果通过 SignalNormalizer 归一化为 GovernanceSignal，最终通过 SignalBus 注入 Agent Loop 的三层 Harness。

治理管线由三部分组成：(1) Policy Engine（规则检查），(2) HistoryManager（事件溯源存储），(3) Governance 度量系统（事后分析）。

**核心文件**：

| 文件 | 行数 | 职责 |
|------|------|------|
| `policy/__init__.py` | 100 | PolicyEngine 主类 + 消息构建 + 条件收割 |
| `policy/base.py` | 23 | PolicyCheck ABC |
| `policy/lessons.py` | 87 | LessonTriggerCheck |
| `policy/contract.py` | 44 | ContractDriftCheck |
| `policy/identity.py` | 19 | IdentityIntegrityCheck |
| `policy/dependency.py` | 39 | DependencyChainCheck |
| `policy/registry.py` | 41 | 策略注册表 + contract.yaml 加载 |
| `history.py` | 150 | HistoryManager 事件溯源 |
| `contract.py` | 423 | ProjectContract + 漂移检测 + 依赖图 |
| `governance/quality.py` | 231 | AI 建议采纳率分析 |
| `governance/patterns.py` | 158 | 共变模块 + 类型聚类检测 |
| `governance/graph.py` | 126 | 语义变更图构建 |
| `governance/releases.py` | 47 | 发布历史管理 |
| `governance/state_bundle.py` | 98 | 治理状态快照导出 |
| `fact/__init__.py` | 48 | Event→Fact 推导入口 |
| `fact/file_patterns.py` | 52 | 文件模式 Fact 推导 |
| `fact/workflow_patterns.py` | 43 | 工作流模式 Fact 推导 |
| `fact/contract_patterns.py` | 27 | 合约模式 Fact 推导 |

---

## 一、Policy Engine 执行链路

### 1.1 完整调用链

```
daemon: _do_workspace_scan()
  │
  ├─ derive_facts(project.name)
  │     │
  │     ├─ HistoryManager.load() 读取最近 50 条 event
  │     ├─ derive_file_facts()     → 连续 policy warnings
  │     ├─ derive_workflow_facts() → rejection chain / burst formalize
  │     ├─ derive_contract_facts() → repeated drift
  │     └─ 去重 + HistoryManager.add_operation(fact_refs=...)
  │
  ├─ engine = PolicyEngine()  # _defaults() 创建 4 个检查
  │
  └─ results = engine.run(session, project)
        │
        ├─ for check in checks:
        │    results[check.name] = check.check(session, project)
        │
        ├─ build_policy_message(results) → Agent 可读文本
        │
        └─ should_harvest()？
             └─ run_harvest_if_needed() → harvest_lessons()
```

### 1.2 PolicyEngine 初始化

```python
class PolicyEngine:
    def __init__(self, checks=None, contract=None, lessons=None):
        self._checks = checks or self._defaults(contract, lessons)

    @staticmethod
    def _defaults(contract, lessons):
        return [
            LessonTriggerCheck(),
            ContractDriftCheck(),
            IdentityIntegrityCheck(),
            DependencyChainCheck(),
        ]

    @classmethod
    def from_project(cls, project_name, workspace_path):
        """从 contract.yaml 加载启用/禁用配置。"""
        checks = load_checks(project_name, workspace_path)
        contract = ContractManager.load(workspace_path)
        lessons = LessonManager.load_instance(workspace_path, project_name)
        return cls(checks=checks, contract=contract, lessons=lessons)
```

### 1.3 PolicyEngine.run() 核心逻辑

```python
def run(self, session, project):
    results = {}
    for check in self._checks:
        try:
            alerts = check.check(session, project)
            if alerts:
                results[check.name] = alerts
        except Exception:
            pass  # 单个检查失败不影响其他检查
    return results
```

**设计决策**：单个 PolicyCheck 异常不阻塞其他检查——每个检查独立运行，异常被静默捕获。这是"最大努力检查"模式。

### 1.4 条件收割

```python
def should_harvest(workspace_path, project_name, warning_threshold=3):
    """连续 N 次 policy_check 有 warning → 触发收割。"""
    entries = HistoryManager.load()
    recent = [e for e in entries[-10:]
              if e.operation == "policy_check_result"]
    if len(recent) < warning_threshold:
        return False
    return all(e.status == "warning" for e in recent[-warning_threshold:])

def run_harvest_if_needed(workspace_path, project_name, tech_stack, warning_threshold=3):
    if should_harvest(workspace_path, project_name, warning_threshold):
        return len(harvest_lessons(workspace_path, project_name, tech_stack))
    return 0
```

---

## 二、四个 PolicyCheck 逐一深度解析

### 2.1 LessonTriggerCheck（lessons.py 87 行）

**目的**：检查当前变更是否触发了已知的"前科"模式。

```python
class LessonTriggerCheck(PolicyCheck):
    name = "lesson_triggers"
    description = "匹配变更文件与已保存的 lesson trigger 模式"

    def check(self, session, project):
        alerts = []
        changed_files = [e.rel_path for e in session.entries if e.status != "same"]
        lessons = LessonManager.load_instance(session.workspace_path, project.name)
        lessons += LessonManager.load_pending(session.workspace_path, project.name)

        for lesson in lessons:
            trigger = lesson.trigger or ""
            if not trigger:
                continue

            for f in changed_files:
                # 子字符串匹配
                if trigger in f:
                    alerts.append({...})
                    break

                # 正则匹配（如果 lesson 有 check.pattern）
                if hasattr(lesson, 'check') and lesson.check and lesson.check.get("pattern"):
                    try:
                        content = read_file(f)
                        if re.search(lesson.check["pattern"], content):
                            alerts.append({...})
                            break
                    except Exception:
                        pass

        return alerts
```

**匹配算法**：
1. 遍历所有 lesson（instance + pending）
2. 每个 lesson 的 `trigger` 字段作为子字符串匹配变更文件路径
3. 如果 lesson 有 `check.pattern`，读取文件内容做正则匹配

**产生信号的字段**：`dangerous_tools`, `prerequisite_tools`, `required_tools`——这些字段在后续的 Harness 检查中被消费。

### 2.2 ContractDriftCheck（contract.py 44 行）

**目的**：检查变更是否违反了项目合约（contract.yaml）。

```python
class ContractDriftCheck(PolicyCheck):
    name = "contract_drift"
    description = "检测变更是否与项目合约产生偏差"

    def check(self, session, project):
        contract = ContractManager.load(session.workspace_path)
        if not contract:
            return []

        changed = [e.rel_path for e in session.entries if e.status != "same"]
        return detect_drift(session.workspace_path, changed, contract)
```

**委托给 `detect_drift()`**，执行三个检测维度：

#### 维度 1：功能签名丢失
```python
check_feature_signatures(workspace_path, changed_files, contract):
    for feature in contract.decided_features:
        if feature.location not in changed_files:
            continue
        content = read_file(feature.location)
        if feature.signature not in content:
            alerts.append({"rule": "feature_signature_lost", ...})
```

#### 维度 2：技术栈漂移
```python
_detect_new_imports(workspace_path, changed_files):
    for f in changed_files:
        imports = _PY_IMPORT_RE.findall(content)
        new_imports = [i for i in imports
                      if i not in contract.tech_stack and i not in _BUILTIN_MODULES]
        if new_imports:
            alerts.append({"rule": "tech_stack_drift", ...})
```

#### 维度 3：架构约束违反
```python
_CONSTRAINT_CHECKS = [
    (r'\.move\(|\.setGeometry\(', "禁止绝对定位"),
    (r'--no-verify|--no-gpg-sign', "禁止跳过 git hooks"),
    (r'\._entries\s*=', "禁止直接 mutation core 状态"),
    (r'\.exec_\(\)', "禁止 Qt4 遗留命名"),
]

_check_architecture_constraint(content):
    for pattern, description in _CONSTRAINT_CHECKS:
        if re.search(pattern, content):
            alerts.append({"rule": description, ...})
```

### 2.3 IdentityIntegrityCheck（identity.py 19 行）

**目的**：检查身份文件是否被大规模修改或删除。

委托给 `identity/guard.py` 的 `_run_integrity_checks()`，三条规则：
1. **全量覆盖检测**：变更文件占比 ≥ 80%（可配置）→ CRITICAL
2. **身份文件删除检测**：CLAUDE.md / .claude/ / .gitignore 等被删除 → HIGH
3. **目录骨架崩塌检测**：顶级目录 Jaccard 相似度 < 0.3 → HIGH

详见报告五对 Identity 系统的深度分析。

### 2.4 DependencyChainCheck（dependency.py 39 行）

**目的**：检查变更文件影响了哪些依赖方。

```python
class DependencyChainCheck(PolicyCheck):
    name = "dependency_chain"
    description = "检测哪些文件 import 了变更文件，可能受到影响"

    def check(self, session, project):
        changed = [e.rel_path for e in session.entries if e.status != "same"]
        dep_graph = load_dep_graph(session.workspace_path)

        alerts = []
        for f in changed:
            dependents = dep_graph.get(f, [])
            if dependents:
                alerts.append({
                    "file": f,
                    "affected_files": dependents,
                    "message": f"文件 {f} 被 {len(dependents)} 个文件 import",
                })
        return alerts
```

**依赖图来源**：`contract.py` 的 `build_dep_graph()` 扫描所有 .py 文件中的 import 语句，构建反向依赖图（文件 → 哪些文件 import 了它），缓存在 `.gitgo/dep_graph.json`。

---

## 三、Contract 合约系统（contract.py 423 行）

### 3.1 数据结构

```python
@dataclass
class DecidedFeature:
    name: str              # 功能名
    location: str          # 代码位置
    signature: str         # 函数/类签名
    confirmed_count: int   # 确认次数
    introduced: str        # 引入日期
    last_modified: str     # 最后修改日期

@dataclass
class ProjectContract:
    project: str
    updated: str
    tech_stack: list[str]              # 技术栈声明
    decided_features: list[DecidedFeature]
    architecture_constraints: list[str] # 架构约束
    harness: dict                      # HarnessPlugin 配置
```

### 3.2 依赖图构建

```python
def build_dep_graph(workspace_path):
    dep_graph = {}
    for py_file in workspace_path.rglob("*.py"):
        content = py_file.read_text()
        imports = _PY_IMPORT_RE.findall(content)
        for imp in imports:
            # 解析相对导入为实际文件路径
            resolved = resolve_import(workspace_path, py_file, imp)
            if resolved:
                dep_graph.setdefault(str(resolved), []).append(str(py_file))
    return dep_graph
```

### 3.3 ContractManager

全静态方法。CURD 操作 contract.yaml：
- `load(workspace_path)` → ProjectContract
- `save(workspace_path, contract)` → Path
- `update_feature(workspace_path, project, name, location, signature)` → 新增或更新功能

---

## 四、HistoryManager 事件溯源（history.py 150 行）

### 4.1 Event Sourcing 模式

gitgo 的治理数据采用 **append-only event log** 模式——所有操作和治理事件只追加不删除。

```python
@dataclass
class HistoryEntry:
    timestamp: str        # ISO 8601
    project_name: str
    operation: str        # 12 种 operation 类型
    status: str           # "success" | "warning" | "error"
    detail: dict          # 自由格式的附加数据
    correlation_id: str   # 用于关联同一工作流中的多个事件
    fact_refs: list[str]  # 关联的 Fact ID
    tags: list[str]
    parent_event_id: str  # 父事件 ID（形成事件链）
```

### 4.2 12 种 Operation 类型

| 分类 | Operation 类型 | 写入者 |
|------|---------------|--------|
| 操作 | `scan`, `formalize`, `sync`, `push` | SyncSession step_* 方法 |
| Trial | `triage_accept`, `triage_promote`, `triage_discard` | SyncSession step_triage_* |
| 管理 | `delete_formal`, `dissolve_formal` | SyncSession |
| 治理 | `policy_check_result` | daemon _do_workspace_scan |
| 治理 | `governance_drift`, `governance_synced`, `governance_pushed` | SyncSession / daemon |
| 治理 | `governance_lesson`, `governance_contract_updated` | LessonManager / ContractManager |
| 治理 | `governance_memory_snapshot` | identity.snapshot |
| Agent | `agent_forked`, `agent_killed`, `agent_reaped` | AgentProcessManager |
| 治理 | `workspace_state_snapshot` | _snapshot_workspace |
| 治理 | `rejection` | _handle_command("reject") |

### 4.3 存储与限制

- 存储位置：`.gitgo/gitgo_history.json`（项目级）
- 最大条目：**200 条**——超过后最旧的条目被移除（FIFO）
- 写入方式：每次写入时读取全量 → 追加新条目 → 截断到 200 条 → 写回

```python
def add_operation(self, project_name, operation, status, detail, correlation_id=""):
    entries = self.load()
    entry = HistoryEntry(...)
    entries.append(entry)
    if len(entries) > 200:
        entries = entries[-200:]
    self.save(entries)
```

### 4.4 事件密度与 Commit 密度

daemon 每次 workspace_dirty 写入一条 `policy_check_result`（高频），而 workspace commit 只在 `round_complete` 时发生（低频）。200 条事件可能覆盖数小时的高频 daemon 活动，但只覆盖少数几个人工/Agent 触发的 commit。

---

## 五、Fact 推导引擎（fact/）

### 5.1 derive_facts() 入口

```python
def derive_facts(project_name):
    entries = HistoryManager.load()
    recent = entries[-50:]  # 最近 50 条

    facts = []
    facts.extend(derive_file_facts(recent, project_name, datetime.now()))
    facts.extend(derive_workflow_facts(recent, project_name, datetime.now()))
    facts.extend(derive_contract_facts(recent, project_name, datetime.now()))

    # 去重 + 写回 HistoryManager
    existing_ids = {e.fact_refs for e in entries if e.fact_refs}
    for fact in facts:
        if fact.fact_id not in existing_ids:
            HistoryManager.add_operation(..., fact_refs=[fact.fact_id])
```

### 5.2 三个 Fact 推导函数

**derive_file_facts**：连续 ≥3 条 `policy_check_result` → `consecutive_policy_warnings` fact

**derive_workflow_facts**：
- ≥3 条连续 rejection → `rejection_chain` fact
- ≥5 条短时间内 formalize → `burst_formalize` fact

**derive_contract_facts**：≥5 条 `governance_drift` → `repeated_contract_drift` fact

**Fact 数据结构**：
```python
@dataclass
class Fact:
    fact_id: str        # UUID
    fact_type: str      # "consecutive_policy_warnings" | "rejection_chain" | ...
    summary: str        # 人类可读摘要
    related_events: list[str]  # 关联的 HistoryEntry correlation_id
    derived_at: str     # ISO timestamp
    project_name: str
    severity: str       # "critical" | "high" | "medium" | "low"
```

---

## 六、Governance 度量系统

### 6.1 Quality Metrics（quality.py 231 行）

**目的**：分析 AI Agent 的建议被人类采纳/修改/拒绝的比率。

```python
def load_suggestion_pairs(project_name):
    """按 correlation_id 配对 AI 建议和人类决策。"""
    entries = HistoryManager.load()
    suggestions = [e for e in entries if e.operation.startswith("suggest_")]
    decisions = [e for e in entries if e.operation in ("formalize", "triage_accept", ...)]

    pairs = []
    for sug in suggestions:
        matching = [d for d in decisions if d.correlation_id == sug.correlation_id]
        pairs.append({"suggestion": sug, "decision": matching[0] if matching else None})
    return pairs

def compute_quality_metrics(pairs):
    """计算按类型/commit_type/模块切片的采纳率。"""
    metrics = {"total": len(pairs), "accepted": 0, "modified": 0, "rejected": 0}
    for pair in pairs:
        verdict = _judge_formalize(pair) if pair["suggestion"].operation == "suggest_formalize"
             else _judge_triage(pair)
        metrics[verdict] += 1
    return metrics
```

**_judge_formalize 的判定逻辑**：
- AI 建议的 source_indices 与人类实际选择的 indices 做 Jaccard 重叠度
- Jaccard ≥ 0.8 → accepted（采纳）
- Jaccard ≥ 0.3 → modified（修改后采纳）
- Jaccard < 0.3 → rejected（拒绝）

### 6.2 Pattern Detection（patterns.py 158 行）

**detect_co_changing**：通过顶层目录共现检测共变模块。两个目录的文件经常在同一批 formalize 中出现 → 它们之间存在耦合。

**detect_type_clusters**：按 commit type（feat/fix/docs/refactor 等）分组，计算平均源文件数和多源合并率。

**detect_trial_impact**：统计 trial accept 后触发工作区变更的频率。

### 6.3 Semantic Graph（graph.py 126 行）

```python
def build_graph(project_name):
    entries = HistoryManager.load()
    nodes = []
    edges = []

    # 节点：formal (来自 formalize) + incoming (来自 triage_accept)
    for e in entries:
        if e.operation == "formalize":
            nodes.append({"id": ..., "type": "formal", ...})
        elif e.operation == "triage_accept":
            nodes.append({"id": ..., "type": "incoming", ...})

    # 边：file_overlap (Jaccard ≥ 0.3), same_push, trial_source
    for i, n1 in enumerate(nodes):
        for n2 in nodes[i+1:]:
            overlap = jaccard(n1["files"], n2["files"])
            if overlap >= 0.3:
                edges.append({"source": n1["id"], "target": n2["id"],
                             "type": "file_overlap", "weight": overlap})

    return {"nodes": nodes, "edges": edges}
```

### 6.4 State Bundle（state_bundle.py 98 行）

`collect_state_bundle(session, minimal=False, include_identity=False)` 导出完整治理状态快照：
- project 信息
- status（stage, formal commits）
- governance_summary（quality_metrics, patterns, graph）
- recent_history（最近 20 条事件）
- identity（可选：memory snapshots）
- suggestions

用途：一次性导出给外部系统（AI 分析、监控面板）。

---

## 七、Module 间数据流

```
PolicyEngine.run()
    │
    ├─ LessonTriggerCheck     ←─ LessonManager
    ├─ ContractDriftCheck      ←─ ContractManager.load() + detect_drift()
    ├─ IdentityIntegrityCheck  ←─ identity._run_integrity_checks()
    └─ DependencyChainCheck    ←─ load_dep_graph()
    │
    ▼
results dict → build_policy_message() → Agent 可读文本
             → SignalNormalizer.normalize() → GovernanceSignal[]
             → HistoryManager.add_operation("policy_check_result")
             → should_harvest()？ → run_harvest_if_needed()

Fact Engine (周期性):
    HistoryManager.load() → derive_*_facts() → 去重 → HistoryManager (fact_refs)

Governance Metrics (按需):
    HistoryManager.load() → compute_quality_metrics / detect_patterns / build_graph
```

---

## 八、测试覆盖

| 测试文件 | 测试内容 |
|----------|----------|
| `test_contract.py` | ContractManager CRUD、detect_drift、feature_signatures、依赖图 |
| `test_lesson.py` | Lesson 数据模型、LessonManager CRUD、harvest_lessons |
| `test_identity_guard.py` | 三条完整性规则、directory_skeleton |
| `test_quality.py` | load_suggestion_pairs、compute_quality_metrics、Jaccard 判定 |
| `test_patterns.py` | co_changing、type_clusters、trial_impact |
| `test_graph.py` | build_graph 节点/边、Jaccard 重叠 |
| `test_releases.py` | list_releases、add_release_note |
| `test_state_bundle.py` | collect_state_bundle 结构完整性 |
| `test_protocol_schema.py` | status_dict 顶层键验证 |

全部为纯单元测试，使用 Mock HistoryManager.load 返回构造的 HistoryEntry 列表。

---

## 九、已知限制与潜在问题

1. **HistoryManager 全量读写的性能**：每次 add_operation 都读取全量 JSON → 追加 → 写回。200 条记录时性能可接受，但如果上限增大，需要改为追加写入（JSONL 格式）。

2. **PolicyCheck 异常静默**：单个检查失败不通知调用方，可能导致检查"悄悄失败"——如果某个检查一直抛异常，用户不会知道。

3. **依赖图不实时更新**：`build_dep_graph()` 的结果缓存到文件。如果文件结构变化（新增 import），缓存的 dep_graph 不会自动更新，需要手动重新构建。

4. **contract.yaml 无 Schema 验证**：ContractManager 没有验证 YAML 结构是否合法，错误字段可能被静默忽略。

5. **Fact 推导的硬编码阈值**：连续 3 次 policy warning、5 次 formalize、5 次 drift——这些阈值不可配置，需要修改代码。

6. **事件上限 200 条对治理分析不足**：quality metrics 和 pattern detection 都需要历史数据，200 条对于长期项目可能不够（可能只覆盖几天的数据）。

---

## 十、设计审查总结

### ✅ 已实现
- 4 个可插拔 PolicyCheck
- contract.yaml 驱动策略启用/禁用
- 条件 lesson 收割
- Event Sourcing 模式的 HistoryManager
- Event→Fact 推导引擎
- 完整的 Governance 度量（quality / patterns / graph）
- 依赖图自动构建

### ⚠️ 部分实现
- HistoryManager 性能（全量读写在 200 条上限下可接受）
- 依赖图缓存更新策略
- PolicyCheck 错误处理

### ❌ 未实现
- contract.yaml Schema 验证
- 可配置的 Fact 推导阈值
- HistoryManager 增量写入（JSONL）
- Policy Engine 的监控/告警端点
