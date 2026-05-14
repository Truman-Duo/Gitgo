# AI Protocol — Gitgo ↔ Agent JSON Schema 规范

> 版本: 1.0 | 2026-05-13 | P3 基础设施

---

## 设计原则

1. Gitgo **仅输出 context JSON**，不调用 LLM
2. Agent 自行分析 context，返回 **suggest JSON**
3. Gitgo **仅执行**，不自动采纳 AI 建议
4. 所有 JSON 字段向后兼容新增（agent 忽略未知字段）

---

## 1. Context JSON（Gitgo → Agent）

### 1.1 formalize context

```bash
gitgo suggest formalize --project X --json
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

### 1.2 triage context

```bash
gitgo suggest triage --project X --json
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
        "body": "<string, truncated to 500 chars>"
      }
    ],
    "release_context": {
      "recent_formal_commits": ["<string, [PREFIX-N] + first line of message>"]
    }
  }
}
```

### 1.3 summary context

```bash
gitgo suggest summary --project X --json
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

## 2. Suggest Response JSON（Agent → Gitgo）

Agent 分析 context 后，返回以下格式的建议。Gitgo 展示给人确认，**不自动执行**。

### 2.1 formalize response

```json
{
  "suggest": "formalize",
  "groups": [
    {
      "indices": ["<int, commit index from context.commits>"],
      "message": "<string, full Conventional Commits message with [PREFIX-N] tag>",
      "rationale": "<string, one-line explanation for this grouping choice>"
    }
  ]
}
```

约束：
- `indices` 必须完全覆盖 context 中所有 commit（每个 commit 恰好属于一个 group）
- `message` 首行必须含 `[PREFIX-N]` 编号
- `rationale` 不超过 120 字符

### 2.2 triage response

```json
{
  "suggest": "triage",
  "recommendations": [
    {
      "index": "<int, incoming change index from context>",
      "action": "<string: accept|promote|discard>",
      "confidence": "<string: high|medium|low>",
      "reason": "<string, one-line explanation>"
    }
  ]
}
```

约束：
- 每个 incoming change 恰好一条推荐
- `confidence` = `high` 表示 agent 高度确定（如显式标记为安全补丁/实验），`low` 表示需人仔细判断
- `reason` 不超过 120 字符

### 2.3 summary response

Agent 不返回 summary response JSON。Agent 自行生成 narrative 文本后，注入到上层展示中。

---

## 3. Error Response JSON

所有 suggest 命令的错误输出格式：

```json
{
  "error": "<ERROR_CODE>",
  "message": "<human-readable description>",
  "detail": "<optional, additional context>"
}
```

### 错误码

| 错误码 | 含义 |
|--------|------|
| `PROJECT_NOT_FOUND` | 项目名不在配置中 |
| `UNKNOWN_SUGGEST_TYPE` | suggest-type 不是 formalize/triage/summary |
| `NO_COMMITS` | workspace 无 commit（formalize context 时） |
| `NO_TRIAL_CONFIGURED` | 未配置 trial 仓库（triage context 时） |
| `NO_BACKUP_CONFIGURED` | 未配置 release 仓库 |

---

## 4. 执行流程

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
5. 人确认/修改后，Agent 调用现有的 execute 命令（`formalize` / `trial`）
6. Gitgo 内部通过 `add_suggestion()` 记录 ai_proposal vs human_decision

---

## 5. Agent 实现指南

### context token 尺寸估算

| Context 类型 | 典型 commit 数 | 典型文件数 | 估算 token |
|-------------|---------------|-----------|-----------|
| formalize | 5 | 15 | ~800-1500 |
| triage | 3 | 5 | ~400-700 |
| summary | N/A | N/A | ~200-400 |

不含行级 diff 内容（仅统计 + 顶层符号），控制在大多数 LLM 上下文窗口内。

### 建议的 LLM prompt 结构

```
You are analyzing git commits for a workflow tool called Gitgo.

Context: {context JSON}

Task: {grouping | triage | summary}

Respond with valid JSON matching the schema: {schema reference}
```

### 错误处理

- Agent 返回的 JSON 不符合 schema → Gitgo 展示原始错误，不重试
- Agent 调用 LLM 超时 → Agent 自行重试，Gitgo 无超时机制
- context 为空（无 commit / 无 incoming）→ Gitgo 返回空 context，Agent 不应发起 LLM 调用
