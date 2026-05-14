# Gitgo Protocol v1.0

> 版本：v1.0 | 日期：2026-05-16 | 基于 gitgo v0.20 源码

---

## 版本化策略

| 变更类型 | 处理方式 |
|----------|---------|
| 新增字段 | 不 bump 版本。Agent 必须忽略未知字段。 |
| 删除字段 | bump 主版本（→ v2.0）。被删字段标注 `@deprecated` 保留一个版本。 |
| 重命名字段 | 视为删除 + 新增。bump 主版本。 |
| 新增操作/事件 | 不 bump 版本。Agent 忽略未知操作名。 |

每种 schema 有独立版本标签，共享主版本号。

---

## 1. State Schema

`gitgo status --project <name> --json` 的输出格式。

### 1.1 完整状态 (`--json`)

```json
{
  "project": "<string>",
  "stage": "<string: IDLE|SCANNING|TRIAL_CHECKING|...>",
  "workspace": {
    "path": "<string, absolute path>",
    "entries_total": "<int>",
    "entries_changed": "<int>"
  },
  "commits": {
    "workspace_total": "<int>",
    "formal_total": "<int>",
    "formal_synced": "<int>",
    "formal_pushed": "<int>"
  },
  "trial": {
    "configured": "<bool>",
    "pending": "<int>",
    "total": "<int>"
  },
  "semantic": {
    "workspace_entropy": "<string: low|medium|high>",
    "trial_requires_review": "<bool>",
    "safe_to_formalize": "<bool>",
    "safe_to_publish": "<bool>",
    "blocked_reason": "<string|null: unsynced_formal_commits|no_backup_configured>",
    "suggested_next_action": "<string: triage|formalize|push|idle>",
    "action_queue": ["<string>"]
  }
}
```

### 1.2 原始状态 (`--json --raw`)

不含 `semantic` 块，其余同上。

### 1.3 仅语义块 (`--json --semantic-only`)

仅输出 `semantic` 子对象。

---

## 2. Operation Schema

### 2.1 通用错误格式

```json
{
  "error": "<ERROR_CODE>",
  "message": "<string, human-readable>",
  "detail": "<string|null, optional context>"
}
```

### 2.2 错误码表

| 错误码 | 触发条件 |
|--------|---------|
| `PROJECT_NOT_FOUND` | 项目名不在配置中 |
| `UNKNOWN_SUGGEST_TYPE` | suggest-type 非法 |
| `NO_COMMITS` | workspace 无 commit |
| `NO_TRIAL_CONFIGURED` | 未配置 trial 仓库 |
| `NO_BACKUP_CONFIGURED` | 未配置 release 仓库 |
| `NO_SYNCED_COMMITS` | push 时无已同步 commit |
| `UNKNOWN_GOVERNANCE_TYPE` | governance-type 非法 |

---

### 2.3 list

```bash
gitgo list --json
```

```json
[
  {
    "name": "<string>",
    "workspace_path": "<string>",
    "backup_path": "<string>",
    "commit_prefix": "<string>"
  }
]
```

`--json` 不存在时（`config` mode 复用 `list`）：仅交互式编辑。无 JSON 输出。

---

### 2.4 history

```bash
gitgo history --project <name> --json
```

```json
[
  {
    "timestamp": "<string, ISO 8601>",
    "project": "<string>",
    "operation": "<string: scan|formalize|sync|push|triage_accept|triage_promote|triage_discard|delete_formal|dissolve_formal>",
    "status": "<string: success|failed|cancelled>",
    "detail": { "<operation-specific>" }
  }
]
```

---

### 2.5 scan

```bash
gitgo scan --project <name> --json
```

```json
{
  "result": "<string: ok>",
  "entries": [
    {
      "path": "<string, relative>",
      "status": "<string: new|modified|deleted|renamed|same>",
      "selected": "<bool>"
    }
  ]
}
```

---

### 2.6 formalize

```bash
gitgo formalize --project <name> [--indices 0,1,2] [--message "..."] --json
```

成功:
```json
{
  "result": "<string: ok>",
  "commit": {
    "message": "<string>",
    "number": "<int>",
    "prefix": "<string>",
    "source_indices": ["<int>"],
    "created_at": "<string, ISO 8601>"
  }
}
```

失败:
```json
{
  "result": "<string: fail>",
  "error": "<string>"
}
```

---

### 2.7 sync

```bash
gitgo sync --project <name> [--message "..."] --json
```

```json
{
  "result": "<string: ok|failed>",
  "project": "<string>"
}
```

---

### 2.8 push

```bash
gitgo push --project <name> [--skip-security] --json
```

```json
{
  "result": "<string: ok|fail>",
  "warnings": ["<string>"]
}
```

---

### 2.9 trial list

```bash
gitgo trial list --project <name> --json
```

```json
[
  {
    "index": "<int>",
    "hash": "<string>",
    "message": "<string>",
    "author": "<string>",
    "date": "<string, ISO date>"
  }
]
```

---

### 2.10 trial triage

```bash
gitgo trial --project <name> --trial-action <accept|promote|discard> --index <int> --json
```

```json
{
  "result": "<string: ok|fail>",
  "action": "<string: accept|promote|discard>",
  "index": "<int>"
}
```

---

### 2.11 session

```bash
gitgo session --project <name> [--session-action <action>] --json
```

| action | 输出 |
|--------|------|
| (无) | 返回 `status_dict()` 完整输出 |
| `status` | 同上 |
| `save` | `{"result": "ok", "path": "<path>"}` |
| `load` | `{"result": "ok", "formal_commits_restored": <int>, "stage": "<string>"}` |
| `clear` | `{"result": "ok"}` |

---

### 2.12 release

```bash
gitgo release --project <name> [--release-action create] [--body "..."] --json
```

| action | 输出 |
|--------|------|
| (无) | `{"result": "ok", "repo": {...}}` |
| `create` | `{"result": "ok|fail", "message": "..."}` |

---

### 2.13 daemon (CLI 单次模式)

```bash
gitgo daemon --project <name> --json
```

```json
{"result": "ok", "project": "<name>"}
```

`daemon start/stop/status` 子命令使用 `--daemon-action`。

---

### 2.14 suggest

```bash
gitgo suggest <formalize|triage|summary> --project <name> --json
```

输出格式见 **第 5 章 Suggestion Schema**。

---

### 2.15 governance

```bash
gitgo governance --governance-type <type> --project <name> --json
```

输出格式见 **第 6 章 Governance Schema**。

---

## 3. Stream Schema

`--stream` flag 使支持的操作输出 line-delimited JSON 事件流。
每行一个完整的 JSON 对象。

### 3.1 支持流式的操作

| 操作 | CLI 命令 |
|------|---------|
| scan | `gitgo scan --project X --json --stream` |
| sync | `gitgo sync --project X --json --stream` |
| push | `gitgo push --project X --json --stream` |
| daemon | `gitgo daemon --project X --json --stream` |

### 3.2 标准事件

#### operation_started

```json
{"event": "operation_started", "op": "<string: scan|sync|push>", "project": "<string>"}
```

`scan` / `push` 操作无 `project` 字段。

#### progress

```json
{"event": "progress", "op": "<string>", "current": "<int>", "total": "<int>", "message": "<string>"}
```

#### operation_complete

```json
{"event": "operation_complete", "op": "<string>", "status": "<string: success|failed|skipped>"}
```

根据操作类型可能包含额外字段（`entries`、`total`、`warnings`、`reason`）。

#### log

```json
{"event": "log", "message": "<string>"}
```

### 3.3 daemon 特有事件

daemon 在标准三事件之外还有生命周期事件：

```json
{"event": "daemon_started", "project": "<string>"}
{"event": "daemon_stopped", "project": "<string>", "status": "<string: ok|fail>"}
```

### 3.4 事件流示例

```
{"event": "operation_started", "op": "sync", "project": "MyProject"}
{"event": "log", "message": "扫描工作区..."}
{"event": "progress", "op": "sync", "current": 1, "total": 4, "message": "scanning"}
{"event": "progress", "op": "sync", "current": 2, "total": 4, "message": "formalizing"}
{"event": "progress", "op": "sync", "current": 3, "total": 4, "message": "syncing"}
{"event": "log", "message": "同步完成: f3a2b1c"}
{"event": "operation_complete", "op": "sync", "status": "success"}
```

---

## 4. Daemon Schema

### 4.1 Command 格式

通过 daemon stdin 发送，一行一个 JSON：

```json
{"cmd": "<command_name>", "<param>": "<value>", ...}
```

### 4.2 命令列表

| 命令 | 额外参数 | 说明 |
|------|---------|------|
| `shutdown` | — | 优雅关闭 daemon |
| `status` | — | 返回当前项目状态 |
| `scan` | — | 触发文件扫描 |
| `formalize` | `selected_indices: [int]`, `message: str` | 创建 formal commit |
| `sync` | `message: str` (可选) | 同步到备份仓库 |
| `push` | — | 推送到远程 |
| `trial` | `trial_index: int`, `action: str` | 执行 triage 决策 |
| `session` | `action: str` | save/load/clear 会话 |

### 4.3 Event 格式

daemon 通过 stdout 输出 line-delimited JSON 事件：

| 事件 | 字段 | 说明 |
|------|------|------|
| `daemon_started` | `project` | daemon 启动完毕 |
| `daemon_stopped` | `project`, `status` | daemon 退出 |
| `workspace_dirty` | `project` | watchdog 检测到文件变更 |
| `state_changed` | `stage` | 状态机阶段变更 |
| `operation_started` | `op` | 操作开始 |
| `operation_complete` | `op`, `status` | 操作结束 |
| `progress` | `current`, `total`, `message` | 进度更新 |
| `command_result` | `cmd`, `result` | 命令执行结果 |
| `log` | `message` | 日志消息 |
| `error` | `message` | 错误 |
| `shutdown_ack` | `message` | 确认关闭 |

### 4.4 生命周期

```
daemon_started
    │
    ├── workspace_dirty → operation_started(scan) → operation_complete(scan)
    ├── state_changed
    ├── [命令交互] command → ... → command_result
    │
    daemon_stopped
```

---

## 5. Suggestion Schema

> 本章迁移自原 `docs/AI_Protocol.md`（已删除），合并时补充了 `files_changed` 字段。

### 5.1 设计原则

1. Gitgo **仅输出 context JSON**，不调用 LLM
2. Agent 自行分析 context，返回 **suggest JSON**
3. Gitgo **仅执行**，不自动采纳 AI 建议
4. 所有 JSON 字段向后兼容新增（agent 忽略未知字段）

---

### 5.2 Context JSON（Gitgo → Agent）

#### 5.2.1 formalize context

```bash
gitgo suggest formalize --project <name> --json
```

```json
{
  "suggest": "formalize",
  "project": "<string>",
  "context": {
    "commits": [
      {
        "index": "<int>",
        "hash": "<string, 7-char short hash>",
        "type": "<string: feat|fix|docs|style|refactor|perf|test|chore>",
        "subject": "<string, first line of commit message>",
        "body": "<string, truncated to 500 chars>",
        "files_changed": [
          {
            "path": "<string, relative path>",
            "added": "<int, lines added>",
            "removed": "<int, lines removed>",
            "status": "<string: new|modified|deleted|renamed>",
            "top_level_symbols": ["<string, class/function names, max 10>"]
          }
        ]
      }
    ],
    "prefix": "<string, commit prefix e.g. MYAPP>",
    "next_number": "<int, next available commit number>"
  }
}
```

#### 5.2.2 triage context

```bash
gitgo suggest triage --project <name> --json
```

```json
{
  "suggest": "triage",
  "project": "<string>",
  "context": {
    "incoming_changes": [
      {
        "index": "<int>",
        "hash": "<string>",
        "message": "<string>",
        "author": "<string>",
        "date": "<string, ISO date>",
        "body": "<string, truncated to 500 chars>",
        "files_changed": [
          {
            "path": "<string, relative path>",
            "added": "<int>",
            "removed": "<int>",
            "status": "<string>"
          }
        ]
      }
    ],
    "release_context": {
      "recent_formal_commits": ["<string, [PREFIX-N] + first line of message>"]
    }
  }
}
```

> **v1.0 注**：`files_changed` 字段在 v0.20 代码中存在，但原 `AI_Protocol.md` 未收录。v1.0 协议补全。Agent 应兼容该字段可能为空数组。

#### 5.2.3 summary context

```bash
gitgo suggest summary --project <name> --json
```

```json
{
  "suggest": "summary",
  "project": "<string>",
  "context": {
    "workspace": {
      "entries_changed": "<int>",
      "commits_since_base": "<int>",
      "top_changed_dirs": ["<string, directory names, max 5>"]
    },
    "trial": {
      "pending": "<int, count of untriaged incoming>",
      "incoming_summary": [
        {"hash": "<string>", "subject": "<string>", "author": "<string>"}
      ]
    },
    "release": {
      "formal_commits_waiting_push": "<int>",
      "recent_tags": ["<string, [PREFIX-N]>"]
    }
  }
}
```

---

### 5.3 Suggest Response JSON（Agent → Gitgo）

Agent 分析 context 后，返回以下格式的建议。Gitgo 展示给人确认，**不自动执行**。

#### 5.3.1 formalize response

```json
{
  "suggest": "formalize",
  "groups": [
    {
      "indices": ["<int, commit index from context.commits>"],
      "message": "<string, full Conventional Commits message with [PREFIX-N] tag>",
      "rationale": "<string, one-line explanation, max 120 chars>"
    }
  ]
}
```

约束：
- `indices` 必须完全覆盖 context 中所有 commit（每个 commit 恰好属于一个 group）
- `message` 首行必须含 `[PREFIX-N]` 编号
- `rationale` 不超过 120 字符

#### 5.3.2 triage response

```json
{
  "suggest": "triage",
  "recommendations": [
    {
      "index": "<int, incoming change index from context>",
      "action": "<string: accept|promote|discard>",
      "confidence": "<string: high|medium|low>",
      "reason": "<string, one-line explanation, max 120 chars>"
    }
  ]
}
```

约束：
- 每个 incoming change 恰好一条推荐
- `confidence` = `high`：agent 高度确定；`low`：需人仔细判断
- `reason` 不超过 120 字符

#### 5.3.3 summary response

Agent 不返回 summary response JSON。Agent 自行生成 narrative 文本，注入到上层展示。

---

### 5.4 错误响应

```json
{
  "error": "<ERROR_CODE>",
  "message": "<string, human-readable>",
  "detail": "<string|null, optional context>"
}
```

| 错误码 | 含义 |
|--------|------|
| `PROJECT_NOT_FOUND` | 项目名不在配置中 |
| `UNKNOWN_SUGGEST_TYPE` | suggest-type 不是 formalize/triage/summary |
| `NO_COMMITS` | workspace 无 commit（formalize context 时） |
| `NO_TRIAL_CONFIGURED` | 未配置 trial 仓库（triage context 时） |
| `NO_BACKUP_CONFIGURED` | 未配置 release 仓库 |

---

### 5.5 执行流程

```
┌─────────┐     context JSON      ┌─────────┐
│  Gitgo   │ ──────────────────► │  Agent   │
│ suggest  │                      │  (LLM)   │
└─────────┘                      └────┬────┘
      ▲                               │
      │     suggest response JSON     │
      │                               │
      │    ┌─────────┐               │
      │    │  Human   │ ◄─────────────┘
      │    │ confirms │
      │    └────┬────┘
      │         │ execute command (formalize / trial)
      │    ┌────▼────┐
      └────┤  Gitgo   │
           │ execute  │
           └─────────┘
```

1. Agent 调用 `gitgo suggest <type> --project X --json`
2. Agent 将 context JSON 发给 LLM 分析
3. LLM 返回 suggest response JSON
4. Agent 展示建议给人
5. 人确认/修改后，Agent 调用 execute 命令（`formalize` / `trial`）
6. Gitgo 内部通过 `add_suggestion()` 记录 ai_proposal vs human_decision

### 5.6 Token 估算

| Context 类型 | 典型 commit 数 | 典型文件数 | 估算 token |
|-------------|---------------|-----------|-----------|
| formalize | 5 | 15 | ~800-1500 |
| triage | 3 | 5 | ~400-700 |
| summary | N/A | N/A | ~200-400 |

不含行级 diff 内容（仅统计 + 顶层符号），控制在大多数 LLM 上下文窗口内。

---

## 6. Governance Schema

### 6.1 quality

```bash
gitgo governance --governance-type quality --project <name> --json
```

```json
{
  "suggestion_count": "<int>",
  "by_type": {
    "<formalize|triage>": {
      "total": "<int>",
      "accepted": "<int>",
      "modified": "<int>",
      "rejected": "<int>",
      "acceptance_rate": "<float, 0.0-1.0>",
      "modification_rate": "<float, 0.0-1.0>",
      "rejection_rate": "<float, 0.0-1.0>",
      "avg_index_jaccard": "<float|null>"
    }
  },
  "by_commit_type": {
    "<type: feat|fix|docs|...>": {
      "total": "<int>",
      "acceptance_rate": "<float>"
    }
  },
  "by_module": {
    "<module>": {
      "total": "<int>",
      "acceptance_rate": "<float>"
    }
  }
}
```

空历史返回: `{"suggestion_count": 0, "by_type": {}, "by_commit_type": {}, "by_module": {}}`

### 6.2 patterns

```bash
gitgo governance --governance-type patterns --project <name> --json
```

```json
{
  "project": "<string>",
  "co_changing_modules": [
    {
      "modules": ["<string>", "<string>"],
      "co_occurrence": "<int>",
      "total_formal": "<int>"
    }
  ],
  "commit_type_clusters": [
    {
      "type": "<string>",
      "count": "<int>",
      "avg_sources": "<float>",
      "multi_source_ratio": "<float>"
    }
  ],
  "trial_impact": {
    "total_accepted": "<int>",
    "triggered_workspace_change": "<int>",
    "avg_trigger_rate": "<float>"
  }
}
```

### 6.3 graph

```bash
gitgo governance --governance-type graph --project <name> --json
```

```json
{
  "project": "<string>",
  "nodes": [
    {
      "id": "<string: [PREFIX-N]>",
      "type": "<string: formal|incoming>",
      "files_changed": ["<string>"],
      "source_commits": "<int>",
      "created_at": "<string, ISO 8601>",
      "correlation_id": "<string>"
    },
    {
      "id": "<string: incoming:<hash>>",
      "type": "incoming",
      "trial_hash": "<string>",
      "message": "<string>",
      "created_at": "<string, ISO 8601>",
      "correlation_id": "<string>"
    }
  ],
  "edges": [
    {
      "from": "<string, node id>",
      "to": "<string, node id>",
      "type": "<string: file_overlap|same_push|trial_source>",
      "<edge-type-specific fields>": "..."
    }
  ]
}
```

边类型特定字段：

| 边类型 | 额外字段 |
|--------|---------|
| `file_overlap` | `overlap_files: [string]`, `overlap_ratio: float` |
| `same_push` | `pushed_at: string` |
| `trial_source` | — |

### 6.4 releases

```bash
gitgo governance --governance-type releases --project <name> --json
```

```json
{
  "project": "<string>",
  "releases": [
    {
      "pushed_at": "<string, ISO 8601>",
      "commits": ["<string, [PREFIX-N]>"],
      "reason": "<string|null, release note>"
    }
  ]
}
```

### 6.5 release-note

```bash
gitgo governance --governance-type release-note --project <name> --message "..." --json
```

成功:
```json
{"ok": true, "message": "<string>"}
```

无 push 记录:
```json
{"ok": false, "message": "<string>"}
```

缺失 `--message`:
```json
{"error": "MISSING_MESSAGE", "detail": "release-note requires --message"}
```

---

## 7. MCP Tool Reference

MCP Server 暴露 17 个工具。所有工具通过 FastMCP 注册，Python 类型注解自动推导 JSON Schema。

### 7.1 项目与状态

| 工具名 | 参数 | 返回 | 对应 CLI |
|--------|------|------|---------|
| `gitgo_list_projects` | — | `list[dict]` | `list --json` |
| `gitgo_status` | `project: str` | `dict` (State Schema) | `status --json` |

### 7.2 操作执行

| 工具名 | 参数 | 返回 | 对应 CLI |
|--------|------|------|---------|
| `gitgo_scan` | `project: str` | `dict` (scan result) | `scan --json` |
| `gitgo_formalize` | `project: str`, `indices: list[int]`, `message: str` | `dict` (formalize result) | `formalize --json` |
| `gitgo_sync` | `project: str`, `message: str` (可选) | `dict` (sync result) | `sync --json` |
| `gitgo_push` | `project: str`, `skip_security: bool` (可选) | `dict` (push result) | `push --json` |
| `gitgo_run_workflow` | `project: str`, `message: str` (可选) | `dict` (workflow result) | `sync --json` |

### 7.3 Triage

| 工具名 | 参数 | 返回 | 对应 CLI |
|--------|------|------|---------|
| `gitgo_trial_list` | `project: str` | `list[dict]` | `trial list --json` |
| `gitgo_trial_triage` | `project: str`, `trial_index: int`, `action: str` | `dict` | `trial --json` |

### 7.4 Suggest

| 工具名 | 参数 | 返回 | 对应 CLI |
|--------|------|------|---------|
| `gitgo_suggest_formalize` | `project: str` | `dict` (formalize context) | `suggest formalize --json` |
| `gitgo_suggest_triage` | `project: str` | `dict` (triage context) | `suggest triage --json` |
| `gitgo_suggest_summary` | `project: str` | `dict` (summary context) | `suggest summary --json` |

### 7.5 Governance

| 工具名 | 参数 | 返回 | 对应 CLI |
|--------|------|------|---------|
| `gitgo_governance_quality` | `project: str` | `dict` (quality report) | `governance quality --json` |
| `gitgo_governance_patterns` | `project: str` | `dict` (patterns report) | `governance patterns --json` |
| `gitgo_governance_graph` | `project: str` | `dict` (graph) | `governance graph --json` |
| `gitgo_governance_releases` | `project: str` | `dict` (releases) | `governance releases --json` |
| `gitgo_governance_release_note` | `project: str`, `message: str` | `dict` (result) | `governance release-note --json` |

---

## 附录 A: CLI Mode 完整列表

| mode | --json | --stream | 类型 |
|------|--------|----------|------|
| `gui` | — | — | 人类 GUI |
| `cui` | — | — | 人类 TUI |
| `config` | — | — | 交互式配置 |
| `list` | 是 | — | Read |
| `status` | 是 | — | Read |
| `history` | 是 | — | Read |
| `scan` | 是 | 是 | Write |
| `formalize` | 是 | — | Write |
| `sync` | 是 | 是 | Write |
| `push` | 是 | 是 | Write |
| `daemon` | 是 | 是 | Daemon |
| `trial` | 是 | — | Triage |
| `session` | 是 | — | Session |
| `release` | 是 | — | Release |
| `suggest` | 是 | — | Suggest |
| `governance` | 是 | — | Governance |

---

## 附录 B: State Bundle Schema（预览）

State Bundle 格式将在 P5-D 阶段正式定义。协议此处预留章节。

```bash
gitgo export state-bundle --project <name> --json
```

```json
{
  "gitgo_protocol_version": "1.0",
  "exported_at": "<string, ISO 8601>",
  "project": {
    "name": "<string>",
    "workspace_path": "<string>",
    "backup_path": "<string>",
    "commit_prefix": "<string>"
  },
  "current_state": { "<State Schema §1.1>" },
  "recent_history": [ "<HistoryEntry, most recent 50>" ],
  "recent_suggestions": [ "<suggest_* entries, most recent 20>" ],
  "governance_summary": {
    "quality": { "<Governance Schema §6.1>" },
    "patterns": { "<Governance Schema §6.2>" }
  }
}
```

`--minimal` 模式不含 `recent_history` 和 `recent_suggestions`。
