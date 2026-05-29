# Gitgo Runtime State Model

> v0.24.1 源码审计 | 状态碎片化诊断与收敛路径

---

## 一、目的

列出 Gitgo 当前所有子系统维护的状态，标注其**生产者**、**消费者**、**更新策略**和**持久化位置**。
暴露状态碎片化的具体位置，为统一 state model 提供基线。

---

## 二、状态清单（按子系统）

### 2.1 SyncSession — 操作级状态

| 状态字段 | 类型 | 位置 | 生产者 | 消费者 | 更新策略 | 持久化 |
|---------|------|------|--------|--------|---------|--------|
| `stage` | `SessionStage` enum (10 states) | `sync_session.py:111` | step_*() 方法 | GUI/CUI/CLI/MCP 前端 | 每个 step 方法进入时设置 | `.gitgo/session.json` (save_session) |
| `entries` | `list[FileEntry]` | `sync_session.py:114` | step_scan() | step_sync(), step_create_formal_commit(), status_dict() | scan 时全量替换 | 摘要写入 session.json |
| `commits` | `list[CommitInfo]` | `sync_session.py:115` | step_load_commits() | step_create_formal_commit(), status_dict() | load 时全量替换 | 摘要写入 session.json |
| `formal_commits` | `list[FormalCommit]` | `sync_session.py:116` | step_create_formal_commit(), step_triage_incoming("accept") | step_sync(), step_push(), status_dict(), governance/* | 追加 | `.gitgo/session.json` |
| `selected_workspace` | `set[int]` | `sync_session.py:117` | step_toggle_workspace_selection(), step_create_formal_commit() | step_create_formal_commit() | toggle/single/range 模式 | 否 |
| `incoming_changes` | `list[IncomingChange]` | `sync_session.py:120` | step_check_trial() | step_triage_incoming(), status_dict(), suggest triage | check 时全量替换 | 否 (derived from trial git) |
| `_correlation_id` | `str` (UUID4) | `sync_session.py:140` | __init__() | HistoryManager.add_operation/add_suggestion | 构造时生成 | 写入每次 history entry |
| `_last_op` | `dict` | sync_session.py (scattered) | 每个 step_*() 成功后 | save_session() | 覆盖 | session.json |
| `_pending_accept` | `IncomingChange \| None` | `sync_session.py:137` | step_start_accept_confirm() | step_confirm_accept(), step_cancel_accept() | 覆盖 | 否 |

### 2.2 Governance — 派生分析状态

Governance 模块**不持有自己的状态**。它是从 HistoryManager 读取原始记录，计算派生指标。
所有 governance 函数是纯函数 (load → compute → return)，无副作用，无持久化状态。

| 派生指标 | 类型 | 生产者 | 消费者 | 数据源 |
|---------|------|--------|--------|--------|
| `suggestion_quality` | `dict` (acceptance_rate 等) | governance/quality.py: compute_quality_metrics() | CLI/MCP governance quality | HistoryManager suggest_* entries |
| `co_changing_modules` | `list[dict]` (module pairs + counts) | governance/patterns.py: detect_co_changing() | CLI/MCP governance patterns | HistoryManager formalize entries |
| `commit_type_clusters` | `list[dict]` (type + avg_sources) | governance/patterns.py: detect_type_clusters() | CLI/MCP governance patterns | HistoryManager formalize entries |
| `trial_impact` | `dict` (triggered_workspace_change) | governance/patterns.py: detect_trial_impact() | CLI/MCP governance patterns | HistoryManager triage_accept + scan entries |
| `change_graph` | `dict` (nodes + edges) | governance/graph.py: build_graph() | CLI/MCP governance graph | HistoryManager formalize/triage_accept/push entries |
| `releases` | `dict` (pushed_at + commits + reason) | governance/releases.py: list_releases() | CLI/MCP governance releases | HistoryManager push entries |

### 2.3 Contract — 合约状态

| 状态字段 | 类型 | 位置 | 生产者 | 消费者 | 更新策略 | 持久化 |
|---------|------|------|--------|--------|---------|--------|
| `tech_stack` | `list[str]` | contract.py: ProjectContract | ContractManager (sync 后扫描 import 语句) | detect_drift (push 前) | sync 后自动追加新发现 | `.gitgo/contract.yaml` |
| `decided_features` | `list[DecidedFeature]` | contract.py: ProjectContract | ContractManager (sync 后扫描新文件+签名) | detect_drift (push 前) | sync 后自动追加 confirmed_count+=1 的新特征 | `.gitgo/contract.yaml` |
| `architecture_constraints` | `list[str]` (正则规则) | contract.py: ProjectContract | **人工** 编辑 YAML | detect_drift (push 前) | 人工维护 | `.gitgo/contract.yaml` |
| `drift_state` | 返回值 `list[dict]` | contract.py: detect_drift() | detect_drift (push 前调用) | push 流程 (告警或阻断) | 每次 push 前重新计算 | 否 (实时检测) |

### 2.4 Identity — 完整性状态

| 状态字段 | 类型 | 位置 | 生产者 | 消费者 | 更新策略 | 持久化 |
|---------|------|------|--------|--------|---------|--------|
| `integrity_warnings` | 写入 HistoryManager | identity/guard.py: _run_integrity_checks() | step_scan() 末尾 | HistoryManager query, CLI/MCP | 每次 scan 重新检测 | gitgo_history.json (operation="integrity_warning") |
| `tool_memory_snapshots` | 文件目录 | identity/snapshot.py: snapshot_tool_memories() | step_sync() 成功后 | restore_tool_memories(), CLI memory list | 增量快照，保留 5 次 | `.gitgo/memories/` (backup repo) |
| `directory_skeleton` | `list[str]` | identity/guard.py: _save_directory_skeleton() | step_sync() 成功后 | _detect_structure_collapse() (下次 scan) | 覆盖 | `.gitgo/directory_skeleton.json` |

### 2.5 Lesson — 知识状态

| 状态字段 | 类型 | 位置 | 生产者 | 消费者 | 更新策略 | 持久化 |
|---------|------|------|--------|--------|---------|--------|
| `lessons` (abstract) | `list[Lesson]` | knowledge/lesson.py | harvest_lessons() (sync 后), 人工 promote | CLI/MCP lesson search/verify | JSONL 追加 | `.gitgo/knowledge/abstract.jsonl` |
| `lessons` (instance) | `list[InstanceLesson]` | knowledge/lesson.py | harvest_lessons() (sync 后) | CLI/MCP lesson search/verify | JSONL 追加 | `.gitgo/knowledge/instance.jsonl` |
| `pendings` | 农割过程中临时列表 | knowledge/lesson.py: harvest_lessons() | harvest_lessons() | lesson verify (人工确认) | harvest 时追加 | 否 (pending 在当前 harvest 调用内) |

### 2.6 Authorship — 发布净化状态

Authorship **不持有自己的持久化状态**。它是 push 前的**过滤器**——输入 commit message + 代码内容，输出清洗后的版本。

| 操作 | 类型 | 位置 | 调用者 | 调用时机 |
|------|------|------|--------|---------|
| `strip_coauthors(message)` | 正则替换 | authorship.py | step_push() 前 | commit message 清洗 |
| `strip_code_comments(content)` | 正则替换 | authorship.py | step_push() 前 (--aggressive) | 代码注释清洗 |
| `exclude_tool_configs(files)` | 文件过滤 | authorship.py | step_push() 前 | 排除 CLAUDE.md/.claude/ 等 |

### 2.7 Status Dict — 聚合状态视图

| 字段 | 来源状态 | 计算位置 |
|------|---------|---------|
| `workspace.entries_total` | SyncSession.entries | sync_session.py:163 |
| `workspace.entries_changed` | SyncSession.entries (status != "same") | sync_session.py:164 |
| `commits.workspace_total` | SyncSession.commits | sync_session.py:167 |
| `commits.formal_total` | SyncSession.formal_commits | sync_session.py:168 |
| `commits.formal_synced` | SyncSession.formal_commits (synced=True) | sync_session.py:169 |
| `commits.formal_pushed` | SyncSession.formal_commits (pushed=True) | sync_session.py:170 |
| `trial.configured` | ProjectConfig.trial | sync_session.py:173 |
| `trial.pending` | SyncSession.incoming_changes (PENDING) | sync_session.py:175 |
| `semantic.workspace_entropy` | entries_changed count | sync_session.py:188-193 |
| `semantic.suggested_next_action` | entries_changed + formal_synced/pushed + trial_pending | sync_session.py:195-203 |
| `semantic.blocked_reason` | formal_synced/pushed vs entries_changed | sync_session.py:206-209 |

---

## 三、碎片化诊断

### 3.1 同名不同义

"状态"一词在系统中有三种不同的身份：

| 身份 | 例子 | 特征 |
|------|------|------|
| **操作状态** | `SessionStage` (IDLE/SCANNING/SYNCING...) | 瞬态，描述"当前在执行什么"，session 内有效 |
| **治理状态** | formal_commits[].synced / pushed | 持久态，描述"变更单元处于什么 phase"，跨 session 有效 |
| **派生状态** | governance quality/patterns/graph 输出 | 从 HistoryManager 计算，无自己的持久化 |

### 3.2 更新策略不统一

| 策略 | 使用场景 | 问题 |
|------|---------|------|
| 全量替换 | SyncSession.entries, .commits | scan/load 时必须覆盖，但旧的 entries 在替换前没有 snapshot |
| 追加 | formal_commits, HistoryManager | 不可变日志模式，但 formal_commits 的 synced/pushed 字段是可变的 |
| 覆盖 | _last_op, directory_skeleton | 丢失历史，不可追溯 |
| 实时计算 | governance/*, contract drift | 每次调用重新扫描全部 history——O(n) 200 条 |

### 3.3 消费者分散

同一个状态字段被多个子系统读取，但没有任何一个子系统知道"谁还在读"：

| 状态 | 读取者 |
|------|--------|
| formal_commits[].synced | step_push(), status_dict(), governance releases |
| formal_commits[].pushed | status_dict(), semantic._build_semantic_layer(), governance graph |
| HistoryManager entries | governance/quality, governance/patterns, governance/graph, governance/releases, lesson/harvest |
| .gitgo/contract.yaml | ContractManager, detect_drift |

### 3.4 不可变 vs 可变字段混在同一对象

`FormalCommit` dataclass 中：

| 字段 | 可变性 | 更新时机 |
|------|--------|---------|
| `message` | 可变 | step_edit_formal_message() |
| `number` | 可变 | step_edit_formal_number() |
| `synced` | 可变 | step_sync() 成功 |
| `pushed` | 可变 | step_push() 成功 |
| `source_indices` | 可变 | step_dissolve_formal() |
| `prefix` | **不可变** | 构造时 |
| `created_at` | **不可变** | 构造时 |

synced/pushed 是 governance state 的核心标志，但它们和 message（文本编辑）在同一个可变对象上。这意味着没有"immutable governance log"层——formal commit 的语义状态和文本内容在同一个可变容器里。

---

## 四、收敛目标

### 4.1 第一层收敛：区分三层状态

| 层 | 定义 | 当前对应 |
|----|------|---------|
| **Operational State** | 瞬态，描述执行上下文 | `SessionStage`, `entries`, `commits`, `_pending_accept` |
| **Governance State** | 持久态，描述变更单元的 phase | `formal_commits[].synced/pushed`, `contract.tech_stack/decided_features`, `lesson.status` |
| **Derived State** | 从 governance state 计算，不持久化 | `governance quality/patterns/graph`, `semantic` block, `drift_state` |

### 4.2 第二层收敛：统一 Governance State 的持久化

当前的 6 个持久化位置：

| 位置 | 内容 |
|------|------|
| `.gitgo/session.json` | formal_commits (synced/pushed + message + number) |
| `gitgo_history.json` | 全操作日志 + suggestion records + integrity warnings |
| `.gitgo/contract.yaml` | project tech_stack + decided_features + constraints |
| `.gitgo/knowledge/abstract.jsonl` | 跨项目通用 lesson |
| `.gitgo/knowledge/instance.jsonl` | 单项目具体 lesson |
| `.gitgo/memories/` | 工具记忆快照 |
| `.gitgo/directory_skeleton.json` | 目录骨架 |

6 个位置，4 种格式（JSON / YAML / JSONL / 文件目录）。不是需要合并为一个文件——而是需要**统一的查询接口**，让 subsystem 不直接访问文件路径。

### 4.3 第三层收敛：统一 Immutable Event Log

HistoryManager 已经是系统中唯一的不可变事件日志。但目前：

- formal_commits 的 synced/pushed 变更**没有**作为 history event 写入——只在 session.json 中以可变字段存在
- contract drift 检测结果**没有**作为 history event 写入——是 transient 返回值
- identity warnings **已经**写入 HistoryManager（operation="integrity_warning"）——这是正确的模式
- lesson harvest **没有**作为 history event 写入

应该让所有 governance state 的变更都通过 HistoryManager 的不可变日志记录。这会让 governance/quality 和 governance/patterns 可以直接从 event log 推导所有分析——不需要访问 session.json 或 contract.yaml。

---

## 五、不做的事

- **不引入新的持久化文件。** 不建 `state.db` 或 `governance.json`。HistoryManager 的 JSON 文件 + session.json 已经够用。
- **不建 central state object。** 三层状态是不同性质的东西，不需要一个 God class 包在一起。收敛目标是**状态语义统一** + **变更审计统一**，不是**存储位置统一**。
- **不改 SyncSession 的 API。** 这个文档是诊断，不是重构指令。SyncSession 的 step_*() 方法签名保持不变。
