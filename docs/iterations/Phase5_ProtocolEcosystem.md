# Gitgo Phase 5: Protocol & Ecosystem

> 设计日期：2026-05-16 | 基于 v0.20 源码 | P4 完成后

---

## Phase 5 的定位

P1-P4 让 Gitgo 成为一套自洽的、可度量可自省的 AI-native workflow runtime。
但这套能力目前只对 Gitgo 自己的 CLI 和 MCP server 可见。

P5 要做的不是加新功能。是把**已经运行的三层协议正式化**，让任何语言、任何框架、
任何 agent 都能消费 Gitgo 的语义模型，而不需要跑 Gitgo 的代码。

一句话：**从 "Gitgo 能做什么" 到 "第三方能基于什么契约集成 Gitgo"。**

---

## 当前基线：三层协议的事实状态

Gitgo 目前有三个隐式的协议层，语义重叠但格式不统一：

| 协议层 | 载体 | 示例 | 文档化程度 |
|--------|------|------|-----------|
| CLI JSON | `__main__.py` argparse + `cli/commands.py` | `gitgo status --project X --json` | 每个 verb 自己定义输出格式，无统一 schema |
| Daemon JSON | `daemon/__init__.py` `_emit()` + `_handle_command()` | `{"cmd":"scan"}` → `{"event":"progress",...}` | 无独立文档，格式在代码中隐式定义 |
| MCP Tool | `mcp_server.py` FastMCP decorator | `gitgo_status(project: str) → dict` | FastMCP 自动生成 tool schema，无版本控制 |
| Suggest Protocol | `docs/AI_Protocol.md` | `{"suggest":"formalize","context":{...}}` | 唯一正式文档化的协议，但仅覆盖 suggest |

P5 的目标是把前三层统一为一份版本化的规范文档，将 Suggest Protocol 迁入作为其子章节。

---

## 阶段结构

| Stage | 名称 | 核心产出 | 预估 |
|-------|------|---------|------|
| P5-A | Protocol Specification | `Gitgo_Protocol_v1.0.md` — 六种 schema 的统一规范 | 1-2 周 |
| P5-B | Reference Agent | `examples/agent_loop.py` — suggest → confirm → execute 完整循环 | 1 周 |
| P5-C | Plugin API Formalization | `docs/Plugin_API.md` + 2 个参考插件 | 1 周 |
| P5-D | State Bundle | `gitgo export state-bundle --json` + schema 文档 | 0.5 周 |

---

## P5-A: Protocol Specification

### 现状

目前 agent 开发者要集成 Gitgo 需要看四份源码：
- `__main__.py` 的 argparse choices 看有哪些 mode
- `cli/commands.py` 的每个 `_cmd_*` 函数看 `--json` 输出格式
- `daemon/__init__.py` 的 `_emit()` 和 `_handle_command()` 看事件和命令格式
- `mcp_server.py` 的 `@mcp.tool` decorator 看 tool 签名和 description

### 目标

一份 `docs/Gitgo_Protocol_v1.0.md`，包含六种 schema 的完整定义，作为 Gitgo 与外部世界的**唯一接口契约**。

### 六种 Schema

#### 1. State Schema

`gitgo status --project X --json` 的输出格式。包含三部分：
- 原始计数（workspace / commits / trial）
- `semantic` 子块（workspace_entropy / suggested_next_action / action_queue / blocked_reason）
- 字段类型、必需性、空值含义

#### 2. Operation Schema

每种 CLI verb 的 `--json` 输出格式。按操作类型列出：
- **Read 类**：`list`、`history`、`session status`
- **Write 类**：`scan`、`formalize`、`sync`、`push`
- **Triage 类**：`trial list`、`trial accept/promote/discard`
- **Governance 类**：`governance quality/patterns/graph/releases/release-note`
- 每种操作的输入参数、输出 JSON 结构、错误码

#### 3. Stream Schema

`--stream` flag 的 line-delimited JSON 事件格式。规范每种操作的：
- `operation_started` 事件格式（op 字段 + 可选的 context 字段）
- `progress` 事件格式（op + current + total + message）
- `operation_complete` 事件格式（op + status + result/error）
- 支持 `--stream` 的操作列表：`scan`、`sync`、`push`、`daemon`

#### 4. Daemon Schema

- **Command 格式**：`{"cmd": "scan" | "status" | "formalize" | "sync" | "push" | "trial" | "session" | "shutdown", ...params}`。每个 command 的额外参数
- **Event 格式**：`{"event": "daemon_started" | "workspace_dirty" | "trial_new_commits" | "state_changed" | "progress" | "operation_started" | "operation_complete" | "command_result" | "daemon_stopped", ...}`
- **生命周期**：启动 → 事件流 → 命令交互 → shutdown

#### 5. Suggestion Schema

从 `docs/AI_Protocol.md` 迁移，作为 Protocol 规范的子章节。合并时检查与当前实现的一致性：
- formalize context / triage context / summary context 三种 context 格式
- formalize response / triage response 两种 suggest response 格式
- error response 格式 + 错误码表
- token 估算指南

#### 6. Governance Schema

`gitgo governance <type> --project X --json` 的输出格式。P4 新增：
- `quality` 输出：by_type / by_commit_type / by_module
- `patterns` 输出：co_changing_modules / commit_type_clusters / trial_impact
- `graph` 输出：nodes[] + edges[]
- `releases` 输出：releases[]
- `release-note` 输入：message

### 版本化策略

- 当前版本：`v1.0`
- 新增字段（如 status_dict 里加一个新 key）：不 bump 版本，agent 忽略未知字段
- 删除/重命名字段：bump 到 `v2.0`，同时在旧字段上标注 `@deprecated` 保留一个版本
- 每个 schema 有独立的版本标签，但共享主版本号

### 产出

- `docs/Gitgo_Protocol_v1.0.md` — 六种 schema 的完整规范文档
- `docs/AI_Protocol.md` — 删除（内容已迁入 Protocol 规范），替换为指向 Protocol 文档的链接

### P5-A 认证标准

- [ ] Protocol 文档覆盖全部 17 个 MCP tool 的输入输出
- [ ] Protocol 文档覆盖全部 CLI verb 的 `--json` 输出
- [ ] Protocol 文档覆盖 daemon 的 8 种 command + 10 种 event
- [ ] Protocol 文档覆盖 `--stream` 的 3 种事件格式
- [ ] 每个 schema 有字段级别的类型 + 必需性 + 空值含义说明
- [ ] 版本化策略写在文档头部

---

## P5-B: Reference Agent

### 目标

一个可运行的参考 agent 实现，不是 Agent SDK，而是**协议完整性的验证器**。
约 200 行 Python 脚本，展示完整的 `suggest → human confirm → execute` 循环。
任何 agent 开发者读这个脚本就能理解如何集成 Gitgo。

### 行为流程

```python
# examples/agent_loop.py

def run(project: str):
    """Gitgo reference agent — suggest → confirm → execute 完整循环."""
    
    # Step 1: 获取当前状态
    status = gitgo("status", project)
    if status["semantic"]["suggested_next_action"] == "idle":
        print("Nothing to do.")
        return
    
    # Step 2: 获取 AI 建议
    suggest_type = status["semantic"]["suggested_next_action"]
    if suggest_type == "triage":
        context = gitgo("suggest", project, "--suggest-type", "triage")
        # 展示 context 给人，等待决策
        decision = human_triage_decision(context["context"]["incoming_changes"])
        for idx, action in decision.items():
            gitgo("trial", project, "--trial-action", action, "--index", str(idx))
    
    elif suggest_type == "formalize":
        context = gitgo("suggest", project, "--suggest-type", "formalize")
        # 展示 context 给人（commits + diff 摘要），等待确认
        groups = human_grouping_decision(context["context"]["commits"])
        for group in groups:
            gitgo("formalize", project,
                  "--indices", ",".join(str(i) for i in group["indices"]),
                  "--message", group["message"])
    
    # Step 3: 执行
    gitgo("sync", project)
    gitgo("push", project)
```

### 实现约定

- 所有 Gitgo 调用通过 `subprocess.run(["python", "-m", "gitgo", "--mode", ..., "--json"])` ——证明协议不依赖 Python import
- `human_*_decision()` 函数是交互式终端输入（`input()`）——证明**人在循环中**的 governance 约束
- 不依赖任何 LLM 库——这个 agent 不调用 AI，它把 context 展示给人，人做决策
- ~200 行，单个文件，零依赖

### 产出

- `examples/agent_loop.py` — 参考 agent 实现

### P5-B 认证标准

- [ ] `python examples/agent_loop.py MyProject` 可从头到尾走通 scan→formalize→sync→push
- [ ] 所有 Gitgo 调用通过 subprocess CLI（不 import gitgo 模块）
- [ ] 人的确认步骤不可跳过——每个决策点有 `input()`
- [ ] 错误路径有提示（如 project 不存在、push 被安全检查阻止）

---

## P5-C: Plugin API Formalization

### 现状

Plugin API 的文档是 `backend/core/plugin.py` 的 docstring——嵌在源代码里。
第三方开发者需要读 Python 源码才能理解怎么写插件。

`plugins/auto_merge.py` 是唯一的参考插件（30 行，纯规则引擎）。

Plugin 搜索路径有两层（`{exe}/plugins/`、`~/.vernier/plugins/`），在 `plugin_loader.py` 中隐式定义。

### 目标

把 Plugin API 从源码提取为正式文档，并增加 2 个参考插件覆盖更多 hook 类型。

### 产出

**`docs/Plugin_API.md`** — 包含：

- 8 个 hook 的完整 API 参考（签名、参数、返回值、调用时机、默认行为）
- 插件发现和加载机制（2 级搜索路径、`plugin_class` 约定、`ProjectConfig.commit_format.plugins` 启用列表）
- 插件开发约定（纯 Python + JSON 兼容数据类型 + 不阻塞主流程）
- 参考插件说明

**`plugins/slack_notify.py`**（~50 行）— 演示 sync/push 钩子的用法：

```python
class SlackNotifyPlugin(SyncPlugin):
    name = "slack-notify"
    version = "0.1.0"
    
    def on_sync_complete(self, result: dict) -> None:
        if result.get("success"):
            _send_slack(f"Sync completed: {result.get('commit_hash', '')[:12]}")
    
    def on_push_complete(self, result: dict) -> None:
        if result.get("success"):
            _send_slack(f"Push completed: {result.get('remote', '')}")
```

**`plugins/jira_link.py`**（~40 行）— 演示 commit selection 钩子的用法：

```python
class JiraLinkPlugin(SyncPlugin):
    name = "jira-link"
    version = "0.1.0"
    
    def on_commit_select(self, commits: list[dict]) -> list[int] | None:
        """推荐选中引用同一 Jira issue 的 commit。"""
        # 从 subject/body 中正则提取 Jira key，同 key 的 commit 推荐合并
        ...
```

### P5-C 认证标准

- [ ] `docs/Plugin_API.md` 覆盖全部 8 个 hook 的完整 API 规范
- [ ] `slack_notify` 插件可通过 `--plugin slack-notify` 启用
- [ ] `jira_link` 插件可通过 `--plugin jira-link` 启用
- [ ] 参考插件抛异常时不影响主流程（已有 PluginOrchestrator 保护）
- [ ] `auto_merge` 插件行为不变

---

## P5-D: State Bundle

### 目标

定义一种自包含的治理状态导出格式，使 Gitgo 状态可以被：
- **存档**：保存项目在某个时间点的完整治理快照
- **传输**：两个 Gitgo 实例间交换状态（不需要共享配置或 git 仓库访问）
- **比较**：diff 两个 Bundle 看治理质量的变化
- **外部消费**：第三方工具读取 Bundle 而不需要安装 Gitgo

### State Bundle 格式

```json
{
  "gitgo_protocol_version": "1.0",
  "exported_at": "2026-05-16T10:00:00",
  "project": {
    "name": "MyProject",
    "workspace_path": "D:/Workspace/MyProject",
    "release_path": "D:/Backup/MyProject",
    "commit_prefix": "MYAPP"
  },
  "current_state": { /* status_dict(semantic=True) 输出 */ },
  "recent_history": [ /* HistoryManager.load() 最近 50 条，按 project 过滤 */ ],
  "recent_suggestions": [ /* suggest_* 记录最近 20 条 */ ],
  "governance_summary": {
    "quality": { /* governance quality 输出 */ },
    "patterns": { /* governance patterns 输出 */ }
  }
}
```

### CLI

```bash
gitgo export state-bundle --project X --json > myproject_gitgo_state.json
gitgo export state-bundle --project X --json --minimal  # 不含 history，仅状态快照
```

### 产出

- `cli/commands.py` — `_cmd_export` verb + `state-bundle` 子动作
- `__main__.py` — `--mode export --export-type state-bundle --project X --json`
- `docs/Gitgo_Protocol_v1.0.md` — State Bundle schema 作为附录章节
- `docs/State_Bundle_v1.0.md` — State Bundle 独立规范（简短，指向 Protocol 文档的附录）

### P5-D 认证标准

- [ ] `gitgo export state-bundle --project X --json` 输出合法 JSON
- [ ] 输出含 `gitgo_protocol_version` 版本标识
- [ ] `--minimal` 模式不含 history/suggestions
- [ ] 输出的 `current_state` 与 `gitgo status --json` 的格式一致
- [ ] State Bundle 可被 `python -m json.tool` 验证

---

## Phase 5 完成标准

| 条件 | 必须 |
|------|------|
| `docs/Gitgo_Protocol_v1.0.md` 覆盖全部 6 种 schema + 版本化策略 | 是 |
| `docs/AI_Protocol.md` 删除，内容迁入 Protocol 规范 | 是 |
| `examples/agent_loop.py` 可运行完整的 suggest→confirm→execute 循环 | 是 |
| `docs/Plugin_API.md` 覆盖全部 8 个 hook 的 API 规范 | 是 |
| `plugins/slack_notify.py` + `plugins/jira_link.py` 两个参考插件可启用 | 是 |
| `gitgo export state-bundle --project X --json` 输出符合 schema | 是 |
| 所有新增产出为零新依赖 | 是 |

---

## 新增/修改文件清单

| 文件 | 内容 | 类型 |
|------|------|------|
| `docs/Gitgo_Protocol_v1.0.md` | 六种 schema 的统一协议规范 | 新建 |
| `docs/AI_Protocol.md` | 删除，替换为指向 Protocol 文档的链接 | 修改 |
| `docs/Plugin_API.md` | Plugin API 正式文档 | 新建 |
| `docs/State_Bundle_v1.0.md` | State Bundle 格式规范 | 新建 |
| `examples/agent_loop.py` | 参考 agent 实现 (~200 行) | 新建 |
| `plugins/slack_notify.py` | Slack 通知参考插件 (~50 行) | 新建 |
| `plugins/jira_link.py` | Jira 关联参考插件 (~40 行) | 新建 |
| `cli/commands.py` | `_cmd_export` + `state-bundle` 子动作 (~40 行) | 修改 |
| `cli/__init__.py` | export `_cmd_export` | 修改 |
| `__main__.py` | `--mode export --export-type state-bundle` | 修改 |

---

## Phase 5 完成后的里程碑

Gitgo 从一个"可被 agent 调用的工具"变成一个"有正式接口契约的运行时标准"：

- Protocol v1.0 文档是第三方集成的唯一入口——不需要读代码
- `agent_loop.py` 证明了协议是完整的——任何语言、任何框架都能实现同样的循环
- Plugin API 文档 + 3 个参考插件让第三方可以扩展 Gitgo 而不必理解内部架构
- State Bundle 让治理状态可以脱离 Gitgo 实例而存在——可存档、可传输、可比较

这些产出不引入新功能、新依赖、新架构。它们是对 P1-P4 全部功能的"命名、版本化、文档化"。
