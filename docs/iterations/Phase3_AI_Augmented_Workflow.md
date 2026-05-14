# Gitgo Phase 3: AI-Augmented Workflow

> 设计日期：2026-05-13 | 基于 v0.13 源码审计 | P2 完成后

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
| P3-A | Triage Hook + AI Plugin 基础设施 | 新 hook + AI agent 接入协议 | 1 周 |
| P3-B | AI Commit Grouping | agent 建议 commit 分组方案 | 1-2 周 |
| P3-C | AI Commit Message Generation | agent 基于 diff 生成 message | 1 周 |
| P3-D | AI Triage Recommendation | agent 建议 trial 三叉决策 | 1 周 |
| P3-E | AI Change Summary | agent 生成变更语义叙述 | 1 周 |

---

## P3-A: Triage Hook + AI Plugin 基础设施

### 目标

新增 `on_triage_recommend` 到 Plugin API，并建立 AI plugin 的标准接入模式。

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

**模式 A：内置 Python 插件（同步）** — 插件直接调用外部 LLM API，返回建议。
适合 latency 不敏感的场景（LLM 调用 < 5 秒）。

**模式 B：CLI suggest 命令（异步）** — agent 先获取 suggest 结果，人工审核后再执行。
适合需要人工确认的场景。这是 P3 的核心模式。

```
agent 发起:  gitgo suggest grouping --project X --json
            → Gitgo 收集 context (commits + diffs)
            → 输出 suggest JSON
            → agent 拿到 suggest，展示给人
            → 人确认/修改 → agent 调用 gitgo formalize --indices 0,2,3
```

Gitgo 本身**不持有 LLM API key**，不管理 LLM 连接。AI 建议由调用方（agent）完成。
Gitgo 的角色是：**收集 context → 输出结构化 suggest prompt → 接收结构化 suggest response → 校验 → 展示**。

这意味着 P3 的核心 deliverables 是一组 JSON schema 和 CLI/MCP verbs，而非 LLM 集成代码。

### 产出

- `backend/core/plugin.py` 新增 `on_triage_recommend` hook
- `docs/AI_Protocol.md` — JSON schema 规范（context 格式、suggest 响应格式、错误码）
- `cli/commands.py` 新增 `_cmd_suggest` — suggest verb 入口
- `__main__.py` 新增 `--mode suggest` 和相关 flag

### P3-A 认证标准

- [ ] `on_triage_recommend` hook 在 Plugin API 中定义并可被插件覆盖
- [ ] `docs/AI_Protocol.md` 完整定义 context / suggest / error 三种 JSON schema
- [ ] `gitgo suggest grouping --project X --json` 输出符合 schema 的 context JSON

---

## P3-B: AI Commit Grouping

### 现状

`step_create_formal_commit()` 通过 `on_commit_select` hook 获取选中索引。
默认实现是全选（`set(range(len(commits)))`）。GUI 模式下用户手动勾选。

`auto_merge` 插件按 type 聚类——如果连续 N 个 commit 同类型，只推荐第一个。
这是一个规则引擎，语义理解弱（不知道 feat 和 fix 之间是否有语义关联）。

### 目标

Agent 分析 workspace commits 的 diff 内容，建议语义分组。输出格式：

```json
{
  "suggest": "grouping",
  "project": "MyProject",
  "context": {
    "commits": [
      {"index": 0, "hash": "abc123", "type": "feat", "subject": "add SSH adapter",
       "body": "...", "files_changed": ["adapters/ssh.py", "adapters/factory.py"]},
      {"index": 1, "hash": "def456", "type": "feat", "subject": "add SSH tests",
       "body": "...", "files_changed": ["tests/test_ssh.py"]},
      {"index": 2, "hash": "ghi789", "type": "fix", "subject": "fix login timeout",
       "body": "...", "files_changed": ["remote/github.py"]}
    ],
    "available_diffs": true
  }
}
```

Agent 返回建议：

```json
{
  "suggest": "grouping",
  "groups": [
    {"indices": [0, 1], "rationale": "SSH adapter + tests form a semantic unit"},
    {"indices": [2], "rationale": "unrelated login fix"}
  ]
}
```

### CLI

```bash
# Step 1: 获取分组建议的 context
gitgo suggest grouping --project X --json

# Step 2: agent 拿到 context，调用 LLM 分析，得到建议分组

# Step 3: 人确认后，创建 formal commit（每个 group 一个）
gitgo formalize --project X --indices 0,1 --message "feat: add SSH adapter with tests"
gitgo formalize --project X --indices 2 --message "fix: resolve login timeout in GitHub connector"
```

### MCP

```python
@mcp.tool(description="获取 commit 分组建议的 context。agent 拿到后自行调用 LLM 分析，再使用 formalize 执行。")
def gitgo_suggest_grouping(project: str) -> dict:
    """返回 context JSON，供 agent 做分组分析。不执行任何操作。"""
```

### P3-B 认证标准

- [ ] `gitgo suggest grouping --project X --json` 输出完整 context（commits + files_changed）
- [ ] Context JSON 含每个 commit 的 body、files_changed、diff 摘要
- [ ] Agent 可基于 context 生成分组建议，通过 `formalize --indices` 执行
- [ ] 现有 `auto_merge` 插件行为不受影响

---

## P3-C: AI Commit Message Generation

### 现状

`step_create_formal_commit()` 通过 `on_commit_message_edit` hook 获取 message。
默认实现用 `build_commit_template()` 生成模板（基于 selected commits 的 type+subject），
GUI 模式下用户手动编辑。

### 目标

Agent 基于选中的 commits + 对应文件的 diff 内容，生成符合 Conventional Commits 格式
且有语义准确性的 message。输出格式：

```json
{
  "suggest": "message",
  "project": "MyProject",
  "context": {
    "selected_commits": [
      {"index": 0, "hash": "abc123", "type": "feat", "subject": "add SSH adapter",
       "files_changed": ["adapters/ssh.py", "adapters/factory.py"]}
    ],
    "diff_summary": {
      "adapters/ssh.py": "+120 lines, new file: SSHFileAdapter class with 14 methods",
      "adapters/factory.py": "+15 lines, added SSH branch in create_adapters_for_node"
    },
    "prefix": "MYAPP",
    "number": 1
  }
}
```

Agent 返回建议：

```json
{
  "suggest": "message",
  "message": "[MYAPP-1] feat: add SSH file adapter with SFTP support\n\n- SSHFileAdapter with 14 methods (read/write/walk/compare)\n- factory create_adapters_for_node supports SSH nodes\n- full test coverage for SSH operations"
}
```

### CLI

```bash
# Step 1: 获取 message 建议的 context
gitgo suggest message --project X --indices 0,1 --json

# Step 2: agent 调用 LLM 生成 message

# Step 3: 人确认后执行
gitgo formalize --project X --indices 0,1 --message "..."
```

### P3-C 认证标准

- [ ] `gitgo suggest message --project X --indices 0,1 --json` 输出含 diff 摘要的 context
- [ ] Context 包含 prefix、number 等编号信息
- [ ] Agent 可基于 context 生成 Convential Commits 格式的 message
- [ ] 现有 `build_commit_template()` 行为不受影响

---

## P3-D: AI Triage Recommendation

### 现状

Trial incoming changes 的三叉决策（accept/promote/discard）完全由人做。
SyncSession 有 `on_triage_decision` hook 但仅用于 UI 覆盖，不在 Plugin API 中。

### 目标

Agent 分析 trial incoming change 的 commit 内容，建议三叉决策及理由。

```json
{
  "suggest": "triage",
  "project": "MyProject",
  "context": {
    "incoming_changes": [
      {"index": 0, "hash": "abc123", "message": "fix: resolve CVE-2026-1234 in auth module",
       "author": "bot@security", "body": "...", "files_changed": ["auth/login.py"],
       "diff_summary": "auth/login.py: 2-line change, added input validation"},
      {"index": 1, "hash": "def456", "message": "experiment: try new sorting algorithm",
       "author": "dev@team", "body": "just testing, not for merge", "files_changed": ["utils/sort.py"],
       "diff_summary": "utils/sort.py: complete rewrite, 200+ lines changed"}
    ],
    "release_context": {"recent_formal_commits": ["[MYAPP-5] feat: add dashboard"]}
  }
}
```

Agent 返回建议：

```json
{
  "suggest": "triage",
  "recommendations": [
    {"index": 0, "action": "accept", "confidence": "high",
     "reason": "Security fix for CVE-2026-1234 — critical, minimal change, clearly beneficial"},
    {"index": 1, "action": "discard", "confidence": "high",
     "reason": "Explicitly marked as experiment/not-for-merge by author"}
  ]
}
```

### MCP

```python
@mcp.tool(description="获取 trial triage 建议的 context。agent 分析后建议 accept/promote/discard，人确认后执行。")
def gitgo_suggest_triage(project: str) -> dict:
    """返回 context JSON（incoming changes + diff 摘要），供 agent 做 triage 分析。"""
```

### P3-D 认证标准

- [ ] `gitgo suggest triage --project X --json` 输出含 diff 摘要的 context
- [ ] Context 包含 `release_context`（最近的 formal commit 历史）
- [ ] `on_triage_recommend` 插件 hook 在 Plugin API 中可用
- [ ] Agent 建议中包含 `confidence` 和 `reason` 字段

---

## P3-E: AI Change Summary

### 目标

在 `status_dict()` 的 semantic 块中，新增一个可选的 `change_narrative` 字段——
对当前变更的自然语言叙述。这不是文件列表的翻译，而是对"这次变更在做什么"的语义总结。

```json
{
  "semantic": {
    "workspace_entropy": "medium",
    "suggested_next_action": "triage",
    "action_queue": ["triage", "formalize", "sync", "push"],
    "change_narrative": {
      "workspace": "3 changes adding SSH file transfer support with full test coverage",
      "trial": "1 critical security fix (CVE-2026-1234), 1 experimental sort rewrite (not for merge)",
      "release_ready": "2 formal commits waiting for push: add dashboard, fix login timeout"
    }
  }
}
```

### 实现

`change_narrative` 字段不存储在 SyncSession 中——它是 `status_dict()` 的可选扩展。
由调用方（agent）在拿到 status 后自行生成，或由 `gitgo suggest summary` 提供 context。

```bash
gitgo suggest summary --project X --json
    # 输出 context，agent 生成 narrative 后注入到 status 展示中
```

### P3-E 认证标准

- [ ] `gitgo suggest summary --project X --json` 输出含 workspace/trial/release 三段 context
- [ ] Context 包含足够信息供 agent 生成语义叙述

---

## Phase 3 完成标准

| 条件 | 必须 |
|------|------|
| `on_triage_recommend` hook 在 Plugin API 中可用 | 是 |
| `docs/AI_Protocol.md` 定义 context / suggest / error 三种 JSON schema | 是 |
| `gitgo suggest grouping --json` 输出完整 commit context | 是 |
| `gitgo suggest message --json` 输出含 diff 摘要的 message context | 是 |
| `gitgo suggest triage --json` 输出含 release context 的 triage context | 是 |
| `gitgo suggest summary --json` 输出三段式 summary context | 是 |
| 所有 suggest 命令仅输出 context/proposal，不执行任何变更操作 | 是 |
| MCP 新增 4 个 suggest tool（grouping / message / triage / summary） | 是 |
| 现有 7 个 hook 行为不受影响 | 是 |
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
P3 为 P4 提供 AI 建议的数据积累（哪些建议被采纳/拒绝，rejection reason 是什么），
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
