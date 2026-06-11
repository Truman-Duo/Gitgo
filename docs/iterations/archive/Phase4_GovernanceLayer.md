# Gitgo Phase 4: Governance Layer

> 设计日期：2026-05-13 | 基于 v0.15 源码 | P3 完成后 | 审阅修订版

---

## Phase 4 的定位

P1 让 agent 能调用。P2 让 agent 能连接。P3 让 agent 能建议。

P4 让治理从"每次人判断"升级为"有积累的、可度量的体系"。

这个递进的逻辑是：P3 的 `add_suggestion()` 已经记录了 AI 建议什么、人决策什么。
P4 不再收集更多建议——它要做的是**从已积累的数据中提取模式**，让 governance 不依赖特定的人或特定的 agent。

一句话：**从 "AI 建议 + 人确认" 到 "治理是可度量、可改进的"。**

---

## 审阅修订记录

相对于用户原始设计，本修订版做了以下调整：

| # | 调整 | 原因 |
|---|------|------|
| 1 | **去掉 `_messages_similar` 黑盒** | message 文本相似度比较不可靠、不可解释。质量度量仅用 indices Jaccard 重叠度——agent 建议的 indices 分组 vs 人实际执行的 indices。可解释、可复现。 |
| 2 | **新增 `correlation_id`** | 跨记录关联分析（suggest → formalize → sync → push 同一次工作流）需要 correlation_id。无此字段时，将 suggest 对与后续 formalize 记录关联是不可靠的。 |
| 3 | **新增 `files_changed` 到 formalize detail** | P4-C 构建 change graph 需要知道每个 formal commit 改了哪些文件。当前 formalize detail 只有 commit tag + source_indices，缺少文件列表。 |
| 4 | **`step_push()` 改为批量推送** | 当前 step_push 只推第一个 synced+unpushed commit。P4-C 的 same_push 边和 P4-D 的多 commit 发布单元都需要同一次 push 关联多个 formal commit。改为一次推送所有待推送 commit。 |
| 5 | **P4-D 命名修正** | `gitgo release note` 与现有 `gitgo --mode release` 冲突。改为 `gitgo governance release-note --project X --message "..."`。同时 CLI verb 使用 `--governance-type release-note`。 |
| 6 | **新增 P4-Pre 前置阶段** | correlation_id + batch push + files_changed 三项是 P4-A/B/C/D 的数据基础，必须在分析功能之前完成。 |

---

## 当前基线：P4 可用的数据源

P1-P3 已经积累了三种数据：

| 数据源 | 位置 | 内容 |
|--------|------|------|
| Operation History | `gitgo_history.json` | 200 条全操作记录（scan/formalize/sync/push/triage_*/delete/dissolve） |
| Suggestion Records | `gitgo_history.json` (operation=`suggest_*`) | AI proposal vs human decision 对 |
| Session State | `.gitgo/session.json` | formal_commits 当前状态（synced/pushed/source_indices） |

P4-Pre 增强后：

| 增强 | 说明 |
|------|------|
| `correlation_id` | 每条 history entry 带 session 级 UUID，跨操作可关联 |
| `files_changed` in formalize detail | formalize 记录包含变更文件列表 |
| batch push detail | push 记录包含本次推送的所有 commit 列表 |

---

## 阶段结构

| Stage | 名称 | 核心产出 | 预估 |
|-------|------|---------|------|
| **P4-Pre** | **数据基础增强** | correlation_id + batch push + files_changed | 0.5 周 |
| P4-A | Suggestion Quality Metrics | AI 建议采纳率/修改率/拒绝率，按类型和模块分布 | 1-2 周 |
| P4-B | Change Pattern Detection | 共变模块、commit 类型聚类、trial 后续影响 | 1-2 周 |
| P4-C | Semantic Change Graph | formal commit 关联图（文件重叠 + 时序 + trial 溯源） | 1-2 周 |
| P4-D | Release Reasoning | 多 commit 发布单元的理由记录和查询 | 1 周 |

---

## P4-Pre: 数据基础增强

### 目标

为 P4-A/B/C/D 的分析功能补齐三项数据基础。

### Pre-1: correlation_id

```python
# backend/core/history.py

@dataclass
class HistoryEntry:
    timestamp: str
    project_name: str
    operation: str
    status: str = "success"
    detail: dict = field(default_factory=dict)
    correlation_id: str = ""  # NEW — session 级关联 ID
```

- `SyncSession.__init__` 中生成 `self._correlation_id = str(uuid.uuid4())`
- 所有 `add_operation()` 和 `add_suggestion()` 调用传入 `correlation_id=`
- CLI 命令（`_cmd_*`）在自己内部生成一次性 correlation_id
- 不做全局唯一约束——仅用于关联同一次工作流的记录

### Pre-2: Batch Push

```python
# sync_session.py — step_push 改为推送所有 synced+unpushed
def step_push(self, skip_scan: bool = False) -> tuple[bool, list[dict]]:
    targets = [fc for fc in self.formal_commits if fc.synced and not fc.pushed]
    if not targets:
        return False, []
    # ... push operation ...
    if success:
        for fc in targets:
            fc.pushed = True
    # detail 记录所有 pushed commit refs
    HistoryManager.add_operation(
        ..., "push", "success",
        {"commits": [f"[{fc.prefix}-{fc.number}]" for fc in targets]},
    )
```

### Pre-3: files_changed in formalize detail

```python
# sync_session.py — step_create_formal_commit
HistoryManager.add_operation(
    self.project.name, "formalize", "success",
    {"commit": f"[{prefix}-{fc.number}]",
     "source_indices": list(selected_indices),
     "files_changed": [
         {"path": e.rel_path, "status": e.status}
         for e in self.entries if e.selected
     ]},
)
```

### P4-Pre 认证标准

- [ ] `HistoryEntry` 有 `correlation_id` 字段，向后兼容（空字符串默认值）
- [ ] `step_push()` 一次推送所有 `synced=True, pushed=False` 的 formal commit
- [ ] push history detail 中的 `commits` 列表包含所有被推送的 commit
- [ ] formalize history detail 包含 `files_changed` 列表
- [ ] 现有测试全绿

---

## P4-A: Suggestion Quality Metrics

### 目标

分析 `add_suggestion()` 记录的 `ai_proposal` vs `human_decision` 差异，计算三个核心指标：

- **采纳率**（acceptance rate）：AI 建议被完全采纳的比例
- **修改率**（modification rate）：AI 建议被修改后采纳的比例
- **拒绝率**（rejection rate）：AI 建议被拒绝的比例

并按维度切片：按 suggest_type（formalize/triage）、按 commit type（feat/fix/docs/...）、按变更模块。

### 差异计算逻辑（修订版）

**不使用 message 文本相似度。** 仅用 indices 集合的 Jaccard 重叠度判断。

```python
def compute_formalize_suggestion_quality(ai_proposal: dict, human_decision: dict) -> dict:
    """对比 AI 建议的 grouping 与人的实际执行。
    
    ai_proposal: {"groups": [{"indices": [...], ...}]}
    human_decision: {"indices": [...], ...}
    """
    # AI 建议的所有 indices（跨所有 group 的并集）
    ai_indices = set()
    for g in ai_proposal.get("groups", []):
        ai_indices.update(g.get("indices", []))
    human_indices = set(human_decision.get("indices", []))
    
    # Jaccard 重叠度
    union = ai_indices | human_indices
    index_jaccard = len(ai_indices & human_indices) / max(len(union), 1)
    
    # 分组粒度比较
    ai_group_count = len(ai_proposal.get("groups", []))
    human_is_single = len(human_indices) <= len(ai_indices) * 0.5  # 人拆得更细
    
    if index_jaccard >= 0.8:
        return {"verdict": "accepted", "index_jaccard": index_jaccard}
    elif index_jaccard >= 0.3:
        return {"verdict": "modified", "index_jaccard": index_jaccard}
    else:
        return {"verdict": "rejected", "index_jaccard": index_jaccard}
```

**triage 建议的判断**（逐项 action 匹配）：

```python
def compute_triage_suggestion_quality(ai_proposal: dict, human_decision: dict) -> dict:
    ai_action = {r["index"]: r["action"] for r in ai_proposal.get("recommendations", [])}
    human_index = human_decision.get("index")
    human_action = human_decision.get("action")
    
    if human_index in ai_action and ai_action[human_index] == human_action:
        return {"verdict": "accepted"}
    elif human_index in ai_action:
        return {"verdict": "modified",
                "ai_action": ai_action[human_index],
                "human_action": human_action}
    else:
        return {"verdict": "rejected"}
```

### 跨记录关联

通过 `correlation_id` 将 suggest 记录与后续的 formalize/triage 操作记录关联，自动匹配 `ai_proposal` 与 `human_decision`：

```
suggest_formalize (correlation_id=uuid-1) → ... → formalize (correlation_id=uuid-1)
                                                           ↑ human_decision
```

### 输出格式

```bash
gitgo governance quality --project X --json
```

```json
{
  "project": "MyProject",
  "period": {"from": "2026-05-01T00:00:00", "to": "2026-05-13T23:59:59"},
  "suggestion_count": 42,
  "by_type": {
    "formalize": {
      "total": 30,
      "accepted": 18, "modified": 8, "rejected": 4,
      "acceptance_rate": 0.60, "modification_rate": 0.27, "rejection_rate": 0.13,
      "avg_index_jaccard": 0.72
    },
    "triage": {
      "total": 12,
      "accepted": 9, "modified": 2, "rejected": 1,
      "acceptance_rate": 0.75, "modification_rate": 0.17, "rejection_rate": 0.08
    }
  },
  "by_commit_type": {
    "feat": {"total": 18, "acceptance_rate": 0.55},
    "fix": {"total": 14, "acceptance_rate": 0.71},
    "docs": {"total": 5, "acceptance_rate": 0.80},
    "chore": {"total": 3, "acceptance_rate": 0.33}
  },
  "by_module": {
    "adapters/": {"total": 12, "acceptance_rate": 0.50},
    "frontend/": {"total": 15, "acceptance_rate": 0.67},
    "backend/core/": {"total": 10, "acceptance_rate": 0.60}
  }
}
```

### 实现

新增 `backend/core/governance/quality.py`（~150行）：
- `load_suggestion_pairs()` — 从 HistoryManager 提取所有 `suggest_*` 条目，按 correlation_id 匹配执行记录
- `compute_quality_metrics()` — 聚合计算（仅用 indices Jaccard，不做 message 文本比较）
- `group_by_commit_type()` / `group_by_module()` — 切片

### P4-A 认证标准

- [ ] `gitgo governance quality --project X --json` 输出合法 JSON
- [ ] acceptance_rate + modification_rate + rejection_rate 之和 ≈ 1.0
- [ ] by_commit_type 和 by_module 切片正确
- [ ] 无 suggestion 记录时返回空报告而非报错
- [ ] 不依赖任何 message 文本相似度计算

---

## P4-B: Change Pattern Detection

### 目标

从 Operation History 中检测三种模式：

**1. 共变模块（Co-changing modules）**

哪些文件/目录倾向于在同一个 formal commit 中一起出现。从 formalize operation 的 detail 中的 `files_changed` 直接获取（P4-Pre 已补齐）。

```
adapters/ssh.py ⇄ tests/test_ssh.py  (8/10 formal commits together)
frontend/commit_box.py ⇄ frontend/commit_canvas.py  (6/8)
```

**2. Commit 类型聚类**

workspace commits 的 type 如何分布，哪些 type 更常被合并为一个 formal commit。

```
feat + feat → formalize (最常见)
fix + fix → formalize
feat + docs → formalize
```

**3. Trial 后续影响**

当一个 trial incoming change 被 accept 后，有多大概率触发新的 workspace 变更。

### 输出格式

```bash
gitgo governance patterns --project X --json
```

```json
{
  "project": "MyProject",
  "co_changing_modules": [
    {"modules": ["adapters/ssh.py", "tests/test_ssh.py"], "co_occurrence": 8, "total_formal": 10},
    {"modules": ["frontend/commit_box.py", "frontend/commit_canvas.py"], "co_occurrence": 6, "total_formal": 8}
  ],
  "commit_type_clusters": [
    {"types": ["feat", "feat"], "count": 15},
    {"types": ["fix", "fix"], "count": 9},
    {"types": ["feat", "docs"], "count": 7}
  ],
  "trial_impact": {
    "total_accepted": 12,
    "triggered_workspace_change_within_3_scans": 4,
    "avg_trigger_rate": 0.33
  }
}
```

### 实现

新增 `backend/core/governance/patterns.py`（~200行）：
- `detect_co_changing()` — 从 formalize detail 的 `files_changed` 直接提取
- `detect_type_clusters()` — 从 formalize 的 source_indices + workspace commits 提取
- `detect_trial_impact()` — 从 triage_accept + 后续 scan 记录，按 correlation_id + timestamp 关联

### P4-B 认证标准

- [ ] `gitgo governance patterns --project X --json` 输出合法 JSON
- [ ] 共变模块的 co_occurrence ≤ total_formal
- [ ] commit 类型聚类不重复
- [ ] trial_impact 的 triggered_workspace_change 计数正确

---

## P4-C: Semantic Change Graph

### 目标

从 formal commit 的 `files_changed`（P4-Pre 补齐）和 push 的 batch commit 列表（P4-Pre 补齐），构建轻量级语义关联图。

关联规则：

1. **文件重叠**：两个 formal commit 修改了相同的文件（Jaccard ≥ 0.3）
2. **Trial 溯源**：formal commit 是通过 accept trial incoming 产生的（is_incoming 标记）
3. **时序相邻 + 同次 push**：两个 formal commit 被同一次 push 发布（batch push 的 commits 列表）

### 输出格式

```bash
gitgo governance graph --project X --json
```

```json
{
  "project": "MyProject",
  "nodes": [
    {"id": "[MYAPP-1]", "type": "formal", "synced": true, "pushed": true,
     "files_changed": ["adapters/ssh.py", "adapters/factory.py"],
     "source_commits": 2, "created_at": "2026-05-10T10:00:00"},
    {"id": "[MYAPP-2]", "type": "formal", "synced": true, "pushed": true,
     "files_changed": ["tests/test_ssh.py", "adapters/ssh.py"],
     "source_commits": 1, "created_at": "2026-05-10T14:00:00"}
  ],
  "edges": [
    {"from": "[MYAPP-1]", "to": "[MYAPP-2]", "type": "file_overlap",
     "overlap_files": ["adapters/ssh.py"], "overlap_ratio": 0.5},
    {"from": "[MYAPP-1]", "to": "[MYAPP-2]", "type": "same_push",
     "pushed_at": "2026-05-10T15:00:00"}
  ]
}
```

### 实现

新增 `backend/core/governance/graph.py`（~150行）：
- `build_graph(project_name)` — 从 HistoryManager 读取所有 formalize record（含 files_changed），构建 nodes + edges
- 文件重叠用 Jaccard 系数，阈值 0.3
- same_push 边从 batch push 的 commits 列表直接获得

### P4-C 认证标准

- [ ] `gitgo governance graph --project X --json` 输出合法 JSON
- [ ] nodes 包含所有 formal commit（从 history 中的 formalize 记录）
- [ ] edges 中 file_overlap type 的 overlap_files 非空
- [ ] same_push type 的 edges 关联被同一次 push 发布的 commit（batch push 支持后）

---

## P4-D: Release Reasoning

### 目标

当多个 formal commit 被同一次 push 发布时，记录"为什么它们一起发布"。
**不与现有 `gitgo --mode release` 冲突。** 使用独立的 governance 子命令。

```bash
gitgo governance release-note --project X --message "Dashboard + SSH adapter: both needed for remote management MVP"
```

CLI 完整路径：`gitgo --mode governance --governance-type release-note --project X --message "..."`

这个 message 关联到最近一次 push 操作中的所有 formal commit（batch push 后可以关联多个）。

### 输出格式

```bash
gitgo governance releases --project X --json
```

```json
{
  "project": "MyProject",
  "releases": [
    {
      "pushed_at": "2026-05-10T15:00:00",
      "commits": ["[MYAPP-1]", "[MYAPP-2]"],
      "reason": "Dashboard + SSH adapter: both needed for remote management MVP"
    }
  ]
}
```

### 实现

- 在 push history detail 中增加可选的 `release_note` 字段
- `_cmd_governance` 的 `release-note` 子动作：找到最近一次 push 记录，更新其 detail 中的 `release_note`
- `releases` 子动作：从 HistoryManager 提取所有含 release_note 的 push 记录

### P4-D 认证标准

- [ ] `gitgo governance releases --project X --json` 输出合法 JSON
- [ ] 无 release note 的 release 也出现在列表中（reason 为 null）
- [ ] 同一次 push 的多个 formal commit 被正确分组（batch push 的 commits 列表）
- [ ] `gitgo governance release-note` 不覆盖 `gitgo --mode release`

---

## Phase 4 完成标准

| 条件 | 必须 |
|------|------|
| `HistoryEntry` 有 `correlation_id` 字段 | 是 |
| `step_push()` 支持批量推送（所有 synced+unpushed） | 是 |
| formalize history detail 包含 `files_changed` | 是 |
| `gitgo governance quality --project X --json` 输出建议质量度量 | 是 |
| `gitgo governance patterns --project X --json` 输出变更模式 | 是 |
| `gitgo governance graph --project X --json` 输出语义关联图 | 是 |
| `gitgo governance releases --project X --json` 输出发布理由 | 是 |
| `gitgo governance release-note --project X --message "..."` 记录发布理由 | 是 |
| 所有 governance 命令为 headless（仅读取 HistoryManager，无 Qt 依赖） | 是 |
| MCP 新增 4 个 governance tool | 是 |
| 0 个新的磁盘文件——全部数据来自已有的 `gitgo_history.json` | 是 |

---

## 新增文件清单

| 文件 | 内容 | 预估行数 |
|------|------|---------|
| `backend/core/governance/__init__.py` | 门面 re-export | 10 |
| `backend/core/governance/quality.py` | 建议质量度量（仅 indices Jaccard） | 150 |
| `backend/core/governance/patterns.py` | 变更模式检测 | 200 |
| `backend/core/governance/graph.py` | 语义变更图 | 150 |
| `backend/core/governance/releases.py` | 发布理由查询 | 80 |
| `cli/commands.py` | 新增 `_cmd_governance` + 5 个子动作 (quality/patterns/graph/releases/release-note) | +150 |
| `__main__.py` | `--mode governance` + `--governance-type` | +15 |
| `mcp_server.py` | 新增 4 个 governance tool | +60 |

## 零新增依赖

所有 governance 计算是对 `HistoryManager.load()` 的纯 Python 分析。
不引入数据库、图计算库、ML 库。

---

## Phase 4 完成后的里程碑

此时 Gitgo 不仅是一个 workflow runtime（P1-P2），也不仅是一个 AI 建议平台（P3）。
它开始具备**治理自省能力**——能回答：

- "我们的 AI 建议质量在提升还是在下降？"
- "哪些模块总是一起变更？为什么？"
- "这个 formal commit 和哪些历史变更有关？"
- "这一批发布为什么是一起推送的？"

这些问题的答案不依赖任何特定的人或 agent。它们存储在 `gitgo_history.json` 和 `.gitgo/session.json` 中，
通过 governance 命令查询。**治理知识留在了数据里，而不是在人的脑子里。**

---

## 审阅记录

- **2026-05-13（初始）**：用户提交完整 P4 设计
- **2026-05-13（审阅修订）**：
  - 去掉 `_messages_similar` — 质量度量仅用 indices Jaccard
  - 新增 `correlation_id` 跨记录关联
  - 新增 `files_changed` 到 formalize detail
  - `step_push()` 改为批量推送
  - P4-D 命名修正：`release note` → `governance release-note`
  - 新增 P4-Pre 前置阶段
