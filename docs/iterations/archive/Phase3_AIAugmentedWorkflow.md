# Gitgo Phase 3: AI-Augmented Workflow

> 设计日期：2026-05-13 | 基于 v0.14 源码审计 | P2 完成后
> 审阅意见已综合：合并 P3-B/P3-C、明确定义 diff_summary、增加 rejection 记录、统一 CLI 子命令模式

---

## Phase 3 的定位

P1 让 agent 能 **调用** Gitgo（CLI verbs + JSON）。P2 让 agent 能 **连接** Gitgo（daemon + MCP + semantic state）。

P3 让 agent 能 **参与** Gitgo——不仅执行操作，而且**提出语义判断**。

核心约束来自宪法文档 Stage 3：

> AI 能 propose grouping / naming / semantic boundary / triage，但 **不自动 publish**。
> 最终的 governance 决策仍由人确认。

这意味着 P3 不是"让 AI 接管工作流"，而是给工作流增加一个 **AI 建议层**——agent 提出判断，
人审核确认，Gitgo 执行。AI 是 advisor，不是 operator。

---

## 当前基线：已有的扩展点

Gitgo 已有完整的插件系统，7 个 hook 在工作流关键节点触发：

| Hook | 触发时机 | 返回 | P3 相关 |
|------|---------|------|---------|
| `on_scan_complete` | 文件扫描完成后 | 过滤/标注条目 | ✗ |
| `on_commit_select` | commit 选择界面前 | 建议选中哪些 commit 索引 | ✅ **AI 分组建议** |
| `on_commit_message` | 生成 commit message 前 | 建议 message 字符串 | ✅ **AI message 生成** |
| `on_sync_start` | sync 复制前 | 阻塞/放行 | ✗ |
| `on_sync_complete` | sync 完成后 | 无 | ✗ |
| `on_push_start` | push 前 | 阻塞/放行 | ✗ |
| `on_push_complete` | push 完成后 | 无 | ✗ |

**缺失的 hook：** trial triage 建议。当前 `on_triage_decision` 是 SyncSession 的决策钩子（被 UI 覆盖），
但不是 Plugin API。需要在 Plugin 基类中新增 `on_triage_recommend`。

现有参考实现——`plugins/auto_merge.py`（30 行）——按 type 聚类推荐同类型 commit 合并。
这是一个纯规则引擎的例子，展示了 hook 的接口模式。
P3 要做的是用同样的接口，接入 AI 的判断能力。

---

## 阶段结构

| Stage | 名称 | 核心产出 | 预估 |
|-------|------|---------|------|
| P3-A | Triage Hook + AI Plugin 基础设施 | 新 hook + suggest verb 框架 + AI_Protocol.md | 1 周 |
| P3-B | AI Commit Proposal（分组 + message 合并） | `gitgo suggest formalize` context + agent 协议 | 1-2 周 |
| P3-C | AI Triage Recommendation | `gitgo suggest triage` context + agent 协议 | 1 周 |
| P3-D | AI Change Summary（低优先级） | `gitgo suggest summary` context | 0.5 周 |

P3-B 和 P3-C 合并的理由：分组和 message 生成是同一个语义判断的两面——commit 的 message 由分组决定，
分组的质量由 message 反映。拆开会造成 agent 两次 LLM 调用 + Gitgo 端重复收集 context。

P3-D 优先级降低：本质是 `status_dict().semantic` 块的扩展，不涉及新 hook 或复杂协议，可作为附带小项。

---

## P3-A: Triage Hook + AI Plugin 基础设施

### 目标

新增 `on_triage_recommend` 到 Plugin API，建立 `gitgo suggest` CLI verb 框架，并写出 AI 协议规范文档。

### 新增 hook

```python
# backend/core/plugin.py — SyncPlugin 新增方法

def on_triage_recommend(
    self, incoming_changes: list[dict], project_config: dict
) -> list[dict] | None:
    """对 trial incoming changes 推荐三叉决策。

    - ``incoming_changes``: IncomingChange 的 dict 列表
      ``{"index", "hash", "message", "author", "date", "body"}``
    - ``project_config``: ProjectConfig 的 dict 表示
    - 返回值：每个 change 的推荐，格式：
      ``[{"index": 0, "action": "accept", "reason": "安全补丁"}, ...]``
    - 返回 None = 不干预
    """
    return None
```

### AI Plugin 接入协议

不内置 LLM。Gitgo 通过 **标准化 JSON 协议** 让外部 AI agent 接入 hooks。
模式分两种：

**模式 A：内置 Python 插件（同步调用外部 API）**

插件直接调用外部 LLM API，返回建议。仅限 CLI/daemon 场景。
⚠️ GUI 场景禁止使用模式 A——同步 HTTP 调用会阻塞 Qt 事件循环，导致 UI 冻结。

**模式 B：CLI suggest 命令（异步）** — agent 先获取 context JSON，自行调用 LLM 分析，人确认后再执行。
这是 P3 的核心模式，适用于所有场景（CLI / daemon / GUI / MCP）。

```
agent 发起:  gitgo suggest formalize --project X --json
            → Gitgo 收集 context (commits + diff 统计 + prefix/number)
            → 输出 context JSON 到 stdout
            → agent 拿到 context，调用 LLM 分析
            → agent 展示建议给人
            → 人确认/修改 → agent 调用 gitgo formalize --indices 0,2 --message "..."
```

Gitgo 本身**不持有 LLM API key**，不管理 LLM 连接。AI 建议由调用方（agent）完成。
Gitgo 的角色是：**收集 context → 输出结构化 JSON → 接收执行指令 → 执行**。

### diff_summary 定义

context 中的 `diff_summary` 是 Gitgo 生成的**轻量统计摘要**，不是原始 diff 也不是 LLM 摘要。
每个文件的摘要包含：

```json
{
  "path": "adapters/ssh.py",
  "added": 120,
  "removed": 0,
  "status": "new",
  "top_level_symbols": ["SSHFileAdapter", "SSHGitRunner"]
}
```

- `status`: `"new" | "modified" | "deleted" | "renamed"`
- `top_level_symbols`: 从 diff 中提取的顶层类名/函数名（正则匹配 `^[+-]\s*(class|def)\s+(\w+)`），最多 10 个
- 不含行级 diff 内容——token 量可控

### CLI 设计

统一使用 `--mode suggest --suggest-type <type>` 模式，与现有 `--mode trial --trial-action list` 保持一致：

```bash
gitgo suggest formalize --project X --json     # 分组 + message 建议
gitgo suggest triage --project X --json        # triage 建议
gitgo suggest summary --project X --json       # 变更摘要
```

`__main__.py` 新增：`--mode suggest` + `--suggest-type {formalize,triage,summary}` + `--indices`。

### AI 建议的 rejection 记录

在 `HistoryManager` 中新增 `add_suggestion()` 方法，记录完整的 AI 建议 + 人的最终决策：

```python
@classmethod
def add_suggestion(cls, project_name: str, suggest_type: str,
                   ai_proposal: dict, human_decision: dict) -> None:
    """记录 AI 建议与人的最终决策差异，供 P4 质量度量使用。
    
    - ai_proposal: agent 返回的完整建议 JSON
    - human_decision: 人修改后的最终执行参数（如 {"indices": [0,2], "message": "..."}）
    """
```

仅在建议被执行（或明确拒绝）时记录。不被采纳但也不执行的建议不记录，避免噪音。

### 产出

- `backend/core/plugin.py` 新增 `on_triage_recommend` hook
- `cli/commands.py` 新增 `_cmd_suggest` — suggest verb 入口 + 三个子动作
- `__main__.py` 新增 `--mode suggest`、`--suggest-type`、`--indices` flag
- `backend/core/history.py` 新增 `add_suggestion()` 方法
- `docs/AI_Protocol.md` — JSON schema 规范（context / suggest / error）

### P3-A 认证标准

- [ ] `on_triage_recommend` hook 在 Plugin API 中定义并可被插件覆盖
- [ ] `gitgo suggest --help` 显示 formalize / triage / summary 三个子动作
- [ ] `docs/AI_Protocol.md` 完整定义 context / suggest / error 三种 JSON schema
- [ ] `add_suggestion()` 在 HistoryManager 中可用，记录 ai_proposal + human_decision
- [ ] 现有 7 个 hook + `auto_merge` 插件行为不受影响

---

## P3-B: AI Commit Proposal（分组 + message 合并）

### 现状

- `step_create_formal_commit()` 通过 `on_commit_select` hook 获取选中索引。默认实现全选。
- `step_create_formal_commit()` 通过 `on_commit_message_edit` hook 获取 message。默认用 `build_commit_template()`。
- `auto_merge` 插件按 type 聚类——纯规则，无语义理解。

分组和 message 是同一个判断的两面：不知道分组就写不出 message，message 质量反映分组是否合理。
因此合并为一个 `gitgo suggest formalize`。

### 目标

Agent 分析 workspace commits 的 diff 统计，建议语义分组及每条 message。

### Context 输出

```bash
gitgo suggest formalize --project X --json
```

```json
{
  "suggest": "formalize",
  "project": "MyProject",
  "context": {
    "commits": [
      {
        "index": 0, "hash": "abc123", "type": "feat",
        "subject": "add SSH adapter",
        "body": "implement SSHFileAdapter with 14 methods...",
        "files_changed": [
          {"path": "adapters/ssh.py", "added": 120, "removed": 0,
           "status": "new", "top_level_symbols": ["SSHFileAdapter"]},
          {"path": "adapters/factory.py", "added": 15, "removed": 2,
           "status": "modified", "top_level_symbols": ["create_adapters_for_node"]}
        ]
      },
      {
        "index": 1, "hash": "def456", "type": "feat",
        "subject": "add SSH tests",
        "body": "full test coverage for SSH operations",
        "files_changed": [
          {"path": "tests/test_ssh.py", "added": 200, "removed": 0,
           "status": "new", "top_level_symbols": ["TestSSHFileAdapter", "TestSSHGitRunner"]}
        ]
      },
      {
        "index": 2, "hash": "ghi789", "type": "fix",
        "subject": "fix login timeout",
        "body": "resolve 30s timeout in GitHub connector",
        "files_changed": [
          {"path": "remote/github.py", "added": 3, "removed": 1,
           "status": "modified", "top_level_symbols": ["GitHubConnector"]}
        ]
      }
    ],
    "prefix": "MYAPP",
    "next_number": 6
  }
}
```

### Agent 响应

Agent 调用 LLM 分析 context 后，返回建议：

```json
{
  "suggest": "formalize",
  "groups": [
    {
      "indices": [0, 1],
      "message": "[MYAPP-6] feat: add SSH file adapter with full test coverage\n\n- SSHFileAdapter with 14 methods (read/write/walk/compare)\n- factory create_adapters_for_node supports SSH nodes\n- full test coverage for SSH operations",
      "rationale": "SSH adapter implementation and its tests form a semantic unit"
    },
    {
      "indices": [2],
      "message": "[MYAPP-7] fix: resolve login timeout in GitHub connector\n\n- increase httpx timeout from 10s to 30s",
      "rationale": "unrelated login fix, should be separate commit"
    }
  ]
}
```

### 执行流程

```bash
# 1. 获取 context
gitgo suggest formalize --project X --json > context.json

# 2. Agent 调用 LLM 获取建议分组

# 3. 人确认后逐组执行
gitgo formalize --project X --indices 0,1 --message "..."
gitgo formalize --project X --indices 2 --message "..."

# 4. 记录建议（供 P4 质量度量）
# Gitgo 内部通过 add_suggestion() 记录 ai_proposal vs human_decision
```

### MCP

```python
@mcp.tool(description="获取 commit 分组+message 建议的 context。包含 commits 列表、diff 统计、编号信息。agent 自行调用 LLM 分析后使用 formalize 执行。")
def gitgo_suggest_formalize(project: str) -> dict:
    """返回 context JSON（commits + diff_summary + prefix/number），供 agent 做分组+message 分析。"""
```

### P3-B 认证标准

- [ ] `gitgo suggest formalize --project X --json` 输出完整 context
- [ ] Context 含每个 commit 的 `files_changed`（含 diff_summary）
- [ ] Context 含 `prefix` 和 `next_number`
- [ ] Agent 可基于 context 生成分组+message 建议
- [ ] 人确认后通过 `formalize --indices --message` 逐组执行
- [ ] `add_suggestion()` 记录 ai_proposal 与 human_decision 差异
- [ ] 现有 `build_commit_template()` 和 `auto_merge` 插件行为不受影响

---

## P3-C: AI Triage Recommendation

### 现状

Trial incoming changes 的三叉决策（accept/promote/discard）完全由人做。
SyncSession 有 `on_triage_decision` hook 但仅用于 UI 覆盖，不在 Plugin API 中。

### 目标

Agent 分析 trial incoming change 的 commit 内容，建议三叉决策及理由。

### Context 输出

```bash
gitgo suggest triage --project X --json
```

```json
{
  "suggest": "triage",
  "project": "MyProject",
  "context": {
    "incoming_changes": [
      {
        "index": 0, "hash": "abc123",
        "message": "fix: resolve CVE-2026-1234 in auth module",
        "author": "bot@security", "date": "2026-05-12",
        "body": "added input validation to prevent injection",
        "files_changed": [
          {"path": "auth/login.py", "added": 2, "removed": 1,
           "status": "modified", "top_level_symbols": ["validate_input"]}
        ]
      },
      {
        "index": 1, "hash": "def456",
        "message": "experiment: try new sorting algorithm",
        "author": "dev@team", "date": "2026-05-11",
        "body": "just testing, not for merge",
        "files_changed": [
          {"path": "utils/sort.py", "added": 200, "removed": 50,
           "status": "modified", "top_level_symbols": ["quick_sort", "merge_sort"]}
        ]
      }
    ],
    "release_context": {
      "recent_formal_commits": [
        "[MYAPP-4] feat: add user dashboard",
        "[MYAPP-5] fix: correct pagination offset"
      ]
    }
  }
}
```

### Agent 响应

```json
{
  "suggest": "triage",
  "recommendations": [
    {
      "index": 0,
      "action": "accept",
      "confidence": "high",
      "reason": "Security fix for CVE-2026-1234 — critical, minimal change, clearly beneficial"
    },
    {
      "index": 1,
      "action": "discard",
      "confidence": "high",
      "reason": "Explicitly marked as experiment/not-for-merge by author"
    }
  ]
}
```

### 执行流程

```bash
# 1. 获取 context
gitgo suggest triage --project X --json > context.json

# 2. Agent 调用 LLM 分析，返回 triage 建议

# 3. 人确认后逐项执行
gitgo trial --project X --trial-action accept --index 0
gitgo trial --project X --trial-action discard --index 1

# 4. 记录建议
# 通过 add_suggestion("triage", ai_proposal, human_decision)
```

### MCP

```python
@mcp.tool(description="获取 trial triage 建议的 context。包含 incoming changes + diff 统计 + release context。agent 分析后建议 accept/promote/discard，人确认后执行。")
def gitgo_suggest_triage(project: str) -> dict:
    """返回 context JSON（incoming changes + diff_summary + release_context），供 agent 做 triage 分析。"""
```

### P3-C 认证标准

- [ ] `gitgo suggest triage --project X --json` 输出含 diff_summary 的 context
- [ ] Context 包含 `release_context`（最近的 formal commit 历史）
- [ ] `on_triage_recommend` 插件 hook 在 Plugin API 中可用
- [ ] Agent 建议中包含 `confidence`（high/medium/low）和 `reason` 字段
- [ ] `add_suggestion()` 记录 triage 建议与最终决策差异

---

## P3-D: AI Change Summary（低优先级）

### 目标

在 `status_dict()` 的 semantic 块中，新增可选的 `change_narrative` 字段。
这不是文件列表的翻译，而是对"这次变更在做什么"的语义总结。

```bash
gitgo suggest summary --project X --json
```

Context 输出包含三段数据（workspace / trial / release），agent 自行生成叙述：

```json
{
  "suggest": "summary",
  "project": "MyProject",
  "context": {
    "workspace": {
      "entries_changed": 3,
      "commits_since_base": 5,
      "top_changed_dirs": ["adapters/", "tests/", "remote/"]
    },
    "trial": {
      "pending": 2,
      "incoming_summary": [
        {"hash": "abc123", "subject": "fix: CVE-2026-1234", "author": "bot@security"},
        {"hash": "def456", "subject": "experiment: sorting", "author": "dev@team"}
      ]
    },
    "release": {
      "formal_commits_waiting_push": 2,
      "recent_tags": ["[MYAPP-4]", "[MYAPP-5]"]
    }
  }
}
```

Agent 返回后可注入到 `status_dict().semantic.change_narrative`。

### P3-D 认证标准

- [ ] `gitgo suggest summary --project X --json` 输出三段式 context
- [ ] Context 含 workspace/trial/release 三段的统计信息

---

## Phase 3 完成标准

| 条件 | 必须 |
|------|------|
| `on_triage_recommend` hook 在 Plugin API 中可用 | 是 |
| `docs/AI_Protocol.md` 定义 context / suggest / error 三种 JSON schema | 是 |
| `gitgo suggest formalize --json` 输出含 diff_summary + prefix/number 的 context | 是 |
| `gitgo suggest triage --json` 输出含 release_context 的 triage context | 是 |
| `gitgo suggest summary --json` 输出三段式 summary context | 是 |
| `add_suggestion()` 记录 ai_proposal + human_decision 差异 | 是 |
| 所有 suggest 命令仅输出 context，不执行任何变更操作 | 是 |
| MCP 新增 3 个 suggest tool（formalize / triage / summary） | 是 |
| 现有 7 个 hook + `auto_merge` 插件行为不受影响 | 是 |
| AI Protocol 的 JSON schema 可作为 agent 开发的接口契约 | 是 |

---

## Phase 3 完成后的里程碑

Agent 不再只是 Gitgo 的"远程操作员"。它变成了 Gitgo 的"分析顾问"——

- workspace 有新 commit 时，agent 建议如何分组、如何命名
- trial 有新 incoming 时，agent 分析后建议 accept/promote/discard
- 任何时候，agent 可以生成变更的语义叙述

但 publish 按钮始终在人的手里。Governance 约束没有被绕过——agent 的所有建议
通过 `suggest` → 人确认 → `execute` 的异步循环完成。

---

## 与 Phase 4 的边界

P3 是"AI 做建议"。P4（Governance Layer）是"建议的质量评估和决策审计"——
semantic change graph、architecture-aware formalization、release reasoning。
P3 为 P4 提供 AI 建议的数据积累（`add_suggestion()` 记录采纳/拒绝/修改），
P4 在此基础上建立治理质量度量。

---

## 全局架构位置（Phase 1 → Phase 3）

```
                    ┌──────────────────┐
                    │   Gitgo Runtime   │
                    │                   │
 Phase 1: CLI verbs │  sync_session.py  │  MCP tools     Phase 2
 Agent "calls"      │  step_* methods   │  agent "connects"
                    │                   │
                    │  Plugin Hooks     │
 Phase 3:  AI suggests ──────────────────► human confirms ──► execute
 Agent "participates"    (suggest verbs)    (existing verbs)
```

---

## 审阅记录

- **2026-05-13**：初始设计完成。审阅调整：
  - P3-B + P3-C 合并为 P3-B（Commit Proposal），消除重复 LLM 调用和 context 收集
  - 明确定义 `diff_summary` 为 Gitgo 端轻量统计（新增/删除行数 + 顶层符号）
  - 新增 `add_suggestion()` rejection 记录机制，为 P4 质量度量积累数据
  - CLI sub-action 统一为 `--mode suggest --suggest-type formalize|triage|summary`
  - P3-D（Change Summary）降为低优先级，三阶段结构改为 P3-A/B/C/D
  - 新增并发安全说明：模式 A 仅限 CLI/daemon，GUI 必须走模式 B
