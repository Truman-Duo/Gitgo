# 迭代计划

> 更新日期：2026-05-13

---

## 当前状态

### 已完成

- **Phase 1 Runtime Foundation** — P1-A~P1-E 全部完成，13/13 认证标准验证通过
  - P1-A: Import 解耦 + 结构化输出（`status --json`、lazy import gui/cui）
  - P1-B: CLI verb 矩阵（trial/formalize/scan/push/session 全 verb）
  - P1-C: Governance 状态机（6 states + 转移守卫 + 非法转移显式拒绝）
  - P1-D: Session 持久化（`.gitgo/session.json` + save/resume/status）
  - P1-E: Headless 集成验证（`scripts/verify_headless.sh` 13 checks）
- **CommitBox v2 重构** — Property-driven QSS 状态管理，CommitCanvas 统一提交区，贝塞尔连接线
- **SyncSession 状态机** — 纯 Python 双层状态机（10 operational + 6 governance states）
- **Operations 层** — scan/sync/git/push/security 纯函数，零 Qt 依赖
- **Adapters 体系** — Local/SSH FileAdapter + GitRunner，工厂模式
- **Trial 三叉工作流** — accept/promote/discard，IncomingChange 模型
- **插件系统** — 7 hook 点，3 级搜索路径
- **i18n / 历史 / 主题** — 双语言、同步日志、动态 QSS
- **代码模块化拆分** — `__main__.py`(643→180) → `cli/commands.py`、`sync.py`(324→195) → `security.py`、`project_list.py`(543→310) → `project_edit_dialog.py`
- **状态驱动闭环** — 7 个新 step 方法收口所有 core 变异，Push 路径统一，`on_stage_changed` 接线，按钮状态集中推导

### 当前进行中

- **Phase 2 Agent-Ready Runtime** — ✅ 全部四阶段完成
  - P2-A ✅ Semantic State Layer + `--stream` 流式输出
  - P2-B ✅ Unified Operation History（9 种操作类型）
  - P2-C ✅ Persistent Daemon Core（watchdog + 纯线程架构 + stdin JSON 协议）
  - P2-D ✅ Agent Interface（MCP Server 9 tools + daemon JSON 事件流）

**完整计划详见：** `iterations/Phase2_AgentReadyRuntime.md`

### 源码审计关键发现

**core/ 已经是纯 Python——没有 Qt 依赖。** 唯一耦合是 `__main__.py` 顶层 2 行 import（无条件加载 PySide6 和 Rich），已在 P1-A 修复为延迟 import。

`save_session()` (sync_session.py:822-838) 遗漏 `is_incoming` / `sources_cleared` 字段持久化——P2-A 附带修复。

### 待启动

- **Phase 5 RemoteConnector** — GitHub/GitLab API 集成

---

## 执行顺序

### ✅ Phase 1（主轨道 — 已完成）

```
P1-A ✅              P1-B ✅              P1-C ✅
Import 解耦     →     CLI Verb 矩阵    →    状态机语义固化
结构化输出            ← Agent 可用里程碑 →   转移守卫

                              ↓
                         P1-D ✅              P1-E ✅
                         Session 持久化  →    Headless 集成验证
```

**全部 13/13 认证标准通过。** `scripts/verify_headless.sh` 一键验证。

### P0（GUI 轨道 — 并行执行，不受 Phase 1 影响）

```
F-8 主题收尾  →  RSB Widget  →  动画系统  →  ...
```

core/ 和 frontend/ 的耦合仅为 callback hooks，两条轨道独立。

### Phase 2（主轨道 — 当前）

```
P2-A 📋              P2-B 📋              P2-C 📋              P2-D 📋
Semantic State  →    Unified History →    Persistent     →    Agent Interface
+ Streaming          9 op types           Daemon Core          + MCP Server
```

**核心转变**：从 "agent 调用 Gitgo"（每次启动新进程）到 "agent 连接到 Gitgo"（持久化 runtime）。

P2-A 和 P2-B 是纯 core 增强（SyncSession + HistoryManager），P2-C 是新模块（`core/daemon/`），P2-D 是薄包装。
P0（GUI 轨道）不受影响，继续并行推进。

---

## Phase 1 阶段速览

| Stage | 核心产出 | 里程碑 |
|-------|---------|--------|
| P1-A | headless JSON CLI 可运行 | 首次 headless 运行 |
| P1-B | 完整 CLI verb 矩阵 | **Agent 可用** |
| P1-C | GOVERNANCE_STATE.md + 转移守卫 | 非法转移显式拒绝 |
| P1-D | .gitgo/session.json + 中断恢复 | 中断后可恢复 |
| P1-E | 全流程 headless 测试脚本 | CI 可自动验证 |

**完整计划详见：** `docs/Phase1_RuntimeFoundation.md`

---

## Phase 2 阶段速览

| Stage | 核心产出 | 里程碑 |
|-------|---------|--------|
| P2-A | `status_dict()` + `semantic` 块 + `--stream` | Agent 直接消费状态 |
| P2-B | HistoryManager 覆盖 9 种操作类型 | 完整审计追踪 |
| P2-C | watchdog + trial 轮询 + stdin 命令 | **Persistent Runtime** |
| P2-D | line-delimited JSON 事件流 + MCP 可选 | Agent 持续连接 |

**完整计划详见：** `iterations/Phase2_AgentReadyRuntime.md`

---

## v0.11 代码模块化拆分 (2026-05-13)

按耦合度分析执行 3 个安全拆分（全部不影响可读性/可理解性）：

| 源文件 | 拆分前 | 拆分后 | 新建文件 |
|--------|--------|--------|---------|
| `__main__.py` | 643行 | ~180行 | `cli/commands.py` + `cli/__init__.py` |
| `core/operations/sync.py` | 324行 | ~195行 | `core/operations/security.py` |
| `frontend/project_list.py` | 543行 | ~310行 | `frontend/project_edit_dialog.py` |

---

## 已完成迭代

### CommitBox v2 重构

**2026-05-11.** Property-driven QSS 状态管理替代三层样式冲突（Global QSS ↔ setStyleSheet ↔ QPainter）。CommitCanvas 统一提交区（QHBoxLayout spacing=52 贝塞尔通道）。4 轮 header-box 对齐调试，根因：setMinimumWidth vs setFixedWidth。6 bug 全部修复。

**详见：** `iterations/CommitBox_v2_重构设计.md`

### v0.5 ~ v0.1

以下迭代已完成，详情已归档：

- v0.5: Action Bar / 快捷键 / 动态 QSS / Mixin 架构
- v0.3: GUI 优化 / Push 安全检查 / 差异预览 / i18n / CLI 增强 / 同步历史 / 插件系统 / RepoNode 数据模型
- v0.2: CUI 同步
- v0.1: Commit 工作流重构 / 多项目管理

**详见：** `iterations/archive/`

---

## 架构总览

```
git_url + file_access → RepoNode
                         ├── workspace (高熵开发区)
                         ├── release  (结构化历史)
                         └── trial    (待治理输入) ──→ IncomingChange
                                                        ├── accept  → release (cherry-pick)
                                                        ├── promote → workspace (incoming/*)
                                                        └── discard → 忽略

适配器模式:
  FileAdapter (ABC)          GitRunner (ABC)
   ├── LocalFileAdapter       ├── LocalGitRunner
   ├── SSHFileAdapter         └── SSHGitRunner
   └── SMBFileAdapter

共享状态机 (core/sync_session.py):
  Operational: IDLE → TRIAL_CHECKING → SCANNING → SELECTING → ...
  Governance:  workspace → trial → curated → formalized → release_ready → published

前端: GUI / CUI / Daemon 三种模式
```
