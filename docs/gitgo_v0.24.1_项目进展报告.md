# Gitgo 项目最新进展报告

> 版本：v0.25 | 日期：2026-05-29 | 状态：后端 100% 完成，全局 Runtime 化

---

## 一、项目概述

Gitgo 是一个**项目级同步工具**，将 workspace（工程版）中零散的 git commits
自动聚合为符合 Conventional Commits 规范的 formal commits，同步到 release（正式版）仓库并推送至远程。

解决了 vibe coding / 人+AI 编程场景下的核心痛点：
工作区 commit 随意、格式不规范、无法直接 push 到正式仓库。

**项目规模**：50 个后端模块（~8,000 行 Python）、24 个测试文件（335 个测试函数）、42 个 MCP 工具、22 个 CLI mode。

---

## 二、架构设计

### 2.1 顶层分层

```
┌──────────────────────────────────────┐
│  接口层                               │
│  ├── GUI (PySide6 Qt 桌面)            │
│  ├── CUI (Rich 终端)                  │
│  ├── CLI (headless，22 modes)         │
│  └── MCP Server (FastMCP，42 tools)   │
├──────────────────────────────────────┤
│  引擎层 (backend/)                     │
│  ├── core/       业务引擎              │
│  ├── adapters/   文件/Git 适配器       │
│  ├── models/     共享数据模型          │
│  └── remote/     GitHub/GitLab API    │
└──────────────────────────────────────┘
```

### 2.2 引擎层子模块设计

| 模块 | 文件 | 设计 |
|------|------|------|
| **sync_session** | `sync_session.py` (1100行) | 工作流状态机：18 个 step_* 方法，覆盖 scan→formalize→sync→push→trial 全流程。状态驱动闭环——所有前端必须通过 step 方法操作，不得直接 mutation |
| **operations/** | 6 文件 (~750行) | 底层操作：scan（文件 SHA256 对比+换行符归一化）、git（commit 模板+编号推断+验证）、sync（文件复制+git commit）、security（推送前敏感信息扫描）、diff（文件差异统计） |
| **governance/** | 5 文件 (~650行) | 治理层：quality（AI 建议采纳率度量，indices Jaccard 重叠）、patterns（共变模块检测+commit 类型聚类+trial 影响分析）、graph（语义变更图：file_overlap/same_push/trial_source 三种边）、releases（发布推理+release note）、state_bundle（治理状态快照导出） |
| **identity/** | 2 文件 (~340行) | Identity Guard：guard.py 三条检测规则（全量覆盖/mass_override、身份文件删除/identity_file_deleted、目录骨架崩塌/structure_collapse）；snapshot.py 工具记忆增量快照（.claude/.codex/.codebuddy → backup，保留5次） |
| **knowledge/** | 1 文件 (~340行) | Lesson 系统：抽象层（跨项目通用技术栈知识）+ 实例层（单项目具体文件级知识），JSONL 格式，sync 后自动收割（同一文件反复修改3+次→pending lesson），verify/search/promote_to_abstract 操作 |
| **template_manager** | `template_manager.py` (127行) | Commit 模板系统：CommitTemplate 数据类（name/header_format/body_format/prefix_override），TemplateManager 持久化到 `commit-config.json`，`str.format()` 8 变量填充 |
| **state_reader** | `state_reader.py` (115行) | **v0.25 新增** — 统一治理状态查询接口：6 个 get_* 方法，封装全部持久化位置的路径逻辑 |
| **contract** | `contract.py` (326行) | Project Contract：合约 YAML（tech_stack/decided_features/architecture_constraints），sync 后自动更新，push 前漂移检测（功能删除/技术栈漂移/架构违反） |
| **authorship** | `authorship.py` (145行) | Authorship 过滤：push 前 AI 痕迹清洗——commit message 正则去除 Co-authored-by/Generated with 等模式；激进模式额外去除代码中的 AI 注释；AI 配置文件排除（CLAUDE.md/.claude/.codex/.cursor/） |
| **config** | `config.py` (269行) | 配置管理：ProjectConfig 数据类（含 workspace/release/trial 三维 RepoNode），ConfigManager 读写 JSON（多项目格式），旧格式自动迁移，DEFAULT 配置块（commit_format/security_scan/integrity/authorship） |
| **daemon/** | 4 文件 (~430行) | 持久守护进程：watchdog 文件监控 + trial 定时轮询 + stdin JSON 命令（start/stop/status/run） |
| **history** | `history.py` (130行) | 操作历史：HistoryEntry 数据类（9 种 op type），HistoryManager 静态类（add_operation/add_entry/load），correlation_id 跨记录关联 |
| **plugin** | 2 文件 (~440行) | 插件系统：SyncPlugin 基类（8 个 hook 接口），PluginOrchestrator 发现/加载（2 层搜索路径），3 个参考插件（auto_merge/slack_notify/jira_link） |

### 2.3 适配器层设计

```
FileAdapter (ABC)              GitRunner (ABC)
├── LocalFileAdapter           ├── LocalGitRunner
├── SSHFileAdapter             └── SSHGitRunner
└── SMBFileAdapter (UNC路径)

工厂 create_adapters_for_node() 根据 FileAccessKind
自动创建适配器对：LOCAL → LocalFileAdapter + LocalGitRunner
                  SSH   → SSHFileAdapter   + SSHGitRunner
                  SMB   → SMBFileAdapter   + LocalGitRunner (UNC)
```

### 2.4 远程连接器设计

```
RemoteConnector (ABC，5 个抽象方法)
├── GitHubConnector (httpx REST API)
│   ├── get_repo_info / create_release
│   ├── list_issues / create_pr
│   └── _parse_owner_repo (HTTPS/SSH URL 解析)
└── GitLabConnector (httpx REST API v4)
    ├── get_repo_info / create_release
    ├── list_issues / create_pr (MR)
    └── _parse_project_path (URL-encoded namespace)
```

### 2.5 数据模型设计

```
RepoNode                    ← git_url + file_access + remote
├── FileAccess              ← kind (LOCAL/SSH/SMB) + path + host + port
├── RemoteTarget            ← kind (github/gitlab/bare) + url
└── last_known_head

FileEntry                   ← rel_path / status (new|modified|same|renamed|deleted) / hash / selected
CommitInfo                  ← hash / subject / type (feat|fix|...) / scope / body
FormalCommit                ← message / number / prefix / source_indices / synced / pushed
IncomingChange              ← hash / message / author / timestamp / triage (pending|accepted|promoted|discarded)
HistoryEntry                ← timestamp / project_name / operation / status / detail / correlation_id
SessionStage (Enum)         ← IDLE → SCANNING → SELECTING → COMMITTING → SYNCING → PUSHING → TRIAL_*
```

---

## 三、功能清单

### 3.1 核心工作流（5 项）

| 功能 | CLI | MCP | 说明 |
|------|-----|-----|------|
| `scan` | `--mode scan` | `gitgo_scan` | 工作区文件扫描，SHA256 哈希对比（8MB 分块），支持换行符归一化（normalize_eol） |
| `formalize` | `--mode formalize` | `gitgo_formalize` | 从 workspace commits 创建 formal commit，多选合并（indices），模板填充（--template） |
| `sync` | `--mode sync` | `gitgo_sync` | 变更文件同步到 backup 仓库，自动创建 git commit，同步基点记录 |
| `push` | `--mode push` | `gitgo_push` | 推送至远程（批量推送所有 synced+unpushed），安全检查，Authorship 过滤 |
| `run_workflow` | `--mode daemon run` | `gitgo_run_workflow` | 一键全流程：scan → formalize → sync → push |

### 3.2 扩展工作流（9 项）

| 功能 | CLI | MCP | 说明 |
|------|-----|-----|------|
| `trial` | `--mode trial --trial-action` | `gitgo_trial_list/triage` | 三叉决策：list 查看外部提交，accept（cherry-pick 到 release，-X theirs 自动冲突解决），promote（fetch 到 workspace incoming/* 分支），discard（标记已处理） |
| `daemon` | `--mode daemon --daemon-action` | — | 持久守护进程：start（watchdog+轮询）、stop、status、run（一次性全流程） |
| `session` | `--mode session --session-action` | `gitgo_session` | 会话管理：save（保存到 .gitgo/session.json）、status（查看）、resume（恢复跨进程状态） |
| `release` | `--mode release --release-action` | `gitgo_release_create/info` | 远程发布：get-info（仓库信息）、create-release（GitHub/GitLab API） |
| `history` | `--mode history --op --limit` | `gitgo_history` | 操作历史查询：按项目/操作类型过滤，JSON 输出 |
| `suggest` | `--mode suggest --suggest-type` | `gitgo_suggest_formalize/triage/summary` | AI 建议上下文：formalize（commit 列表+diff 统计）、triage（incoming changes+release 上下文）、summary（三段统计） |
| `governance` | `--mode governance --governance-type` | `gitgo_governance_*` (5 tools) | 治理度量：quality（采纳率/修改率/拒绝率，按类型+commit type+模块切片）、patterns（共变模块/类型聚类/trial 影响）、graph（语义变更图）、releases（发布历史+release note） |
| `export` | `--mode export --export-type` | `gitgo_export` | 状态包导出：state-bundle（完整治理快照，含 identity 块） |
| `list` | `--mode list` | `gitgo_list_projects` | 项目列表 |

### 3.3 增强系统（6 项）

| 功能 | CLI | MCP | 设计 |
|------|-----|-----|------|
| `template` | `--mode template --template-action` | `gitgo_template_*` (4 tools) | 多套 commit message 模板持久化到 `commit-config.json`，`str.format()` 8 变量填充（prefix/number/type_str/scope_str/subject/project_name/commit_count/commit_list），prefix_override 支持 |
| `formal` | `--mode formal --formal-action` | `gitgo_formal_*` (6 tools) | Formal commit 生命周期管理：list/delete/edit-message/edit-number/dissolve（恢复 workspace commit）/clear-sources（解除引用） |
| `memory` | `--mode memory --memory-action` | `gitgo_memory_*` (3 tools) | 工具记忆增量快照（.claude/.codex/.codebuddy → backup .gitgo/memories/，保留最近 5 次），restore 恢复，list 查看 |
| `contract` | `--mode contract` | `gitgo_contract_show/update` | 项目合约（.gitgo/contract.yaml）：sync 后自动更新 decided_features，push 前漂移检测（功能删除/签名丢失/技术栈漂移/架构违反） |
| `lesson` | `--mode lesson` | `gitgo_lesson_*` (4 tools) | 知识传承：抽象层（跨项目技术栈知识）+ 实例层（单项目具体知识），JSONL 格式，sync 后自动收割（同一文件 3+ 次修改 → pending lesson），verify/search/promote_to_abstract |
| `bootstrap` | `--mode bootstrap` | — | 一键自举：自动检测 workspace/release 路径 + 推断下一个编号 + 写入 gitgo_config.json |

### 3.4 防御系统（4 项）

| 系统 | 触发时机 | 检测/操作 |
|------|---------|----------|
| **Identity Guard** | step_scan | 全量覆盖检测（阈值 80%）、身份文件删除告警（CLAUDE.md/.claude/ 等）、目录骨架崩塌检测（Jaccard < 0.3）→ 警告写入 HistoryManager |
| **Memory Snapshot** | step_sync | 自动快照工具记忆到 backup（增量拷贝，filecmp 比对），保留最近 5 次 |
| **Drift Detection** | step_push 前 | 功能删除（decided_feature 文件消失/签名丢失）、技术栈漂移（新增未声明 import）、架构违反（constraints 正则匹配） |
| **Authorship** | step_push 前 | commit message 清洗（正则去除 5 种 AI 声明模式）、代码注释清洗（激进模式）、AI 配置文件排除 |

---

## 四、测试结果

### 4.1 总体指标

| 指标 | 数值 |
|------|------|
| 测试文件 | 24 个 |
| 测试函数 | 335 个 |
| 通过 | **334** |
| 跳过 | 1（需网络：SSH 连接测试） |
| 失败 | **0** |
| 执行时间 | ~56 秒 |

### 4.2 按模块测试覆盖

| 测试文件 | 测试数 | 覆盖模块 |
|---------|--------|---------|
| test_remote.py | 43 | GitHub/GitLab API + issue/PR + factory |
| test_authorship.py | 21 | commit message 清洗 + 代码注释清洗 + AI 配置排除 |
| test_quality.py | 20 | governance/quality 度量引擎 |
| test_identity_guard.py | 18 | guard.py 三条规则 + snapshot 快照/恢复 |
| test_ssh_adapters.py | 18 | SSHFileAdapter + SSHGitRunner |
| test_local_file_adapter.py | 16 | LocalFileAdapter 全部方法 |
| test_local_git_runner.py | 16 | LocalGitRunner 全部方法 |
| test_models.py | 16 | RepoNode/FileAccess/IncomingChange 等模型 |
| test_patterns.py | 16 | governance/patterns 三种检测器 |
| test_contract.py | 15 | ProjectContract + ContractManager + detect_drift |
| test_config.py | 14 | Config/ConfigManager/ProjectConfig |
| test_graph.py | 13 | governance/graph builder |
| test_smb_adapter.py | 13 | SMBFileAdapter UNC 路径 + 工厂 |
| test_template_manager.py | 13 | CommitTemplate + TemplateManager |
| test_lesson.py | 13 | Lesson 数据模型 + LessonManager + harvest |
| test_regression.py | 12 | B1-B6 集成回归 + 字段名审计 |
| test_protocol_schema.py | 11 | Protocol v1.0 schema 校验 |
| test_releases.py | 10 | governance/releases 发布推理 |
| test_sync_session.py | 10 | trial 相关 step 方法 |
| test_self_referential.py | 8 | 自指流程 EOL 归一化 + hash + compare_files |
| test_state_bundle.py | 6 | State Bundle 导出 |
| test_operations.py | 5 | scan/sync 操作 |
| test_diff.py | 5 | diff 操作 |
| test_factory.py | 3 | 适配器工厂 |

### 4.3 集成测试结果

| 测试场景 | 环境 | 结果 |
|---------|------|------|
| 自指 bootstrap + sync + push | Clone → bootstrap → 三仓 workflow | ✅ |
| GitHub 真实 push | 私有仓库 `Truman-Duo/gitgo-integration-test` | ✅ |
| 换行符归一化 | CRLF workspace vs LF release，9 文件测试 | ✅ |
| Identity Guard 告警 | 缺少 CLAUDE.md/.claude/ → Alert 正确触发 | ✅ |
| Trial promote | 外部仓库 fetch 到 workspace | ✅ |
| AI 建议上下文 | formalize/triage/summary 三种 context JSON | ✅ |
| 跨进程 session 恢复 | 独立 push 命令 → 加载已保存 session | ✅ |
| 编号递增 | MYAPP-1 → MYAPP-2 正确递增 | ✅ |
| Cherry-pick 冲突 | trial accept 多版本冲突 → -X theirs 重试 | ✅ |

---

## 五、当前状态

### 完成度

| 区域 | 进度 | 备注 |
|------|------|------|
| 后端引擎 | **100%** | 50 模块，零 NotImplementedError 存根 |
| CLI | **100%** | 22 modes，全 SyncSession 方法可调用 |
| MCP Server | **100%** | 42 tools，零缺失 |
| 测试 | **100%** | 334 passed / 0 failed |
| 集成验证 | **100%** | 三仓 GitHub 真实环境 14/14 通过 |
| GUI | **90%** | Commit Workshop + Incoming + Remotes + History 四 Tab |

### 唯一待办

| 优先级 | 内容 |
|--------|------|
| **P0** | GUI Track — B-1 + F-1 前端架构调整 |

### 版本历史

| 版本 | 日期 | 内容 |
|------|------|------|
| v0.10 | 2026-05 | P1: Runtime Foundation |
| v0.12 | 2026-05 | P2: Semantic + Persistence |
| v0.15 | 2026-05 | P3: AI-Augmented |
| v0.20 | 2026-05 | P4: Governance Layer |
| v0.21 | 2026-05-16 | P5: Protocol & Ecosystem |
| v0.22 | 2026-05-17 | P6: Template + SMB + Issue/PR + CLI/MCP |
| v0.23 | 2026-05-19 | Identity Guard 三层防线 |
| v0.24 | 2026-05-19 | Authorship + Contract + Lesson System |
| v0.24.1 | 2026-05-26 | 全量 Bug 修复 + Bootstrap + EOL 归一化 + 三仓集成验证 |
| **v0.25** | **2026-05-29** | **State Convergence — C1-C3 全局 Runtime 化** |

### 新增：State Convergence（v0.25）

本阶段不扩功能，只收拢——让 9 个子系统的状态语义统一，变更审计统一，持久化查询接口统一。

**C1: Governance Event Completeness** — 9 个 governance event 写入 HistoryManager：
`governance_synced` / `governance_pushed` / `governance_dissolved` / `governance_edited` /
`governance_renumbered` / `governance_drift` / `governance_contract_updated` /
`governance_lesson` / `governance_memory_snapshot`

**C2: Three-Layer State Distinction** — `--layered` 三层显式输出：
- `operational`：stage / entries（瞬态，session 内有效）
- `governance`：formal counts / trial / contract（持久态，跨 session）
- `semantic`：entropy / next_action / action_queue（派生态，agent 可直接消费）

**C3: Unified State Query** — `backend/core/state_reader.py`，6 个 `get_*` 方法：
- `get_formal_commits` / `get_contract` / `get_lessons`
- `get_integrity_warnings` / `get_memory_snapshots` / `get_governance_events`

**架构图**：
```
操作发生 (step_*)
  ├→ 内存状态更新
  ├→ session.json 更新 (save_session)
  └→ governance event 写入 HistoryManager ★
       ├→ StateReader 可查询
       ├→ governance/quality 可从 event 推导
       └→ governance/patterns 可从 event 推导
```

---

## 六、联系与资源

- **GitHub**: https://github.com/Truman-Duo/Gitgo
- **工作区**: `C:\Users\Duo\Desktop\Truman\ClaudeCode_WorkSpace\gitgo`
- **正式仓库**: `C:\Users\Duo\Desktop\Truman\documents\Git\gitgo`
- **关键文档**: `docs/CLAUDE.md` / `docs/HANDOFF.md` / `docs/VERSION.md`
- **设计文档**: `docs/iterations/v0.24_Knowledge_Authorship_Drift.md`
- **测试日志**: `docs/bootstrap_test.log`
