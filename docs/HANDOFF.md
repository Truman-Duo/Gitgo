# gitgo v0.22 交接文档

> 日期：2026-05-17

---

## 本次更新（v0.22）

**Phase 6 + Phase 5.2 完结。** 模板系统、CLI 补齐、MCP 补齐全部完成。

- **模板系统**: `commit-config.json` 多套命名模板，`str.format()` 变量填充，CLI + MCP 管理
- **CLI**: 19 modes（+template +formal），6 formal 管理操作
- **MCP**: 33 tools（+16），覆盖全部 SyncSession 方法
- **SMB 适配器**: UNC 路径，工厂接线
- **GitHub/GitLab**: `list_issues` / `create_pr` 完整实现

### 执行优先级
1. **P0（GUI Track）** — 前端架构调整（B-1 + F-1）

---

## 当前进度总览（v0.22）

| 区域 | 完成度 | 状态 |
|------|--------|------|
| 项目列表 | 85% | 基本可用 |
| Commit Workshop | 90% | CommitCanvas + 三层卡片 ✅ |
| 后端 | 100% | 全部 SyncSession step + Adapters + Remote + Template ✅ |
| Runtime | 100% | P1-P6 全部完成 ✅ |
| Phase 5 | 100% | Protocol & Ecosystem ✅ |
| Phase 6 | 100% | **Template + SMB + Issue/PR + CLI/MCP 补齐** ✅ |
| MCP | 100% | 33 tools，零缺失 ✅ |
| CLI | 100% | 19 modes，全部 SyncSession 方法可 CLI 调用 ✅ |

---

## P4-Pre：数据基础增强（v0.16）

为 P4-A/B/C/D 治理分析功能补齐三项数据基础：

### Pre-1: correlation_id

- `backend/core/history.py` — `HistoryEntry` 新增 `correlation_id: str = ""` 字段
- `HistoryManager.add_operation()` / `add_suggestion()` / `add_entry()` 新增 `correlation_id` 参数
- `backend/core/sync_session.py` — `__init__` 生成 `self._correlation_id = str(uuid.uuid4())`
- 全部 9 处 `add_operation`/`add_entry` 调用传入 `correlation_id=self._correlation_id`
- 向后兼容：默认空字符串，旧历史记录加载不受影响

### Pre-2: Batch Push

- `step_push()` 改为收集**所有** `synced=True, pushed=False` 的 formal commit，一次推送，全部标记 pushed
- push history detail 从 `{"commit": "..."}` 改为 `{"commits": ["[PREFIX-1]", "[PREFIX-2]"]}`
- 支持 P4-C 的 same_push 边和 P4-D 的多 commit 发布单元

### Pre-3: files_changed in formalize detail

- `step_create_formal_commit()` 的 history detail 新增 `files_changed` 列表
- 格式：`[{"path": "adapters/ssh.py", "status": "new"}, ...]`
- P4-C graph builder 可直接读取，无需反查 scan 记录

### 波及文件

| 文件 | 改动 |
|------|------|
| `backend/core/history.py` | `HistoryEntry` + `correlation_id`；`add_operation`/`add_suggestion`/`add_entry` 签名 |
| `backend/core/sync_session.py` | `import uuid`；`self._correlation_id`；9 处 history 调用；`step_push` 批量推送；formalize detail 含 `files_changed` |

### P4-C：语义变更图（v0.19）

**核心文件：**
- `backend/core/governance/graph.py` — 图构建器（~100行）
- `tests/test_graph.py` — 13 个测试

**节点与边：**

| 节点类型 | 来源 | 字段 |
|----------|------|------|
| formal | formalize history 条目 | id, files_changed, source_commits |
| incoming | triage_accept history 条目 | id (incoming:<hash>), trial_hash, message |

| 边类型 | 检测方式 | 阈值 |
|--------|---------|------|
| file_overlap | Jaccard(files_changed_a, files_changed_b) ≥ 0.3 | overlap_files + overlap_ratio |
| same_push | batch push 的 commits 列表 | pushed_at |
| trial_source | triage_accept 与 formalize 同 correlation_id | — |

**CLI：**
```bash
gitgo --mode governance --governance-type graph --project X --json
```

**MCP：** `gitgo_governance_graph(project)` — 15 tools 总计

**认证：** pytest 179 passed, 1 skipped (13 new)

---

### P4-D：发布推理（v0.20）

**核心文件：**
- `backend/core/governance/releases.py` — 发布推理引擎（~50行）
- `tests/test_releases.py` — 10 个测试

**两个函数：**

| 函数 | 数据源 | 输出 |
|------|--------|------|
| `list_releases` | push history 条目 | releases 列表（pushed_at / commits / reason） |
| `add_release_note` | 最新 push 条目 | 写入 detail.release_note，返回 bool |

**CLI：**
```bash
gitgo --mode governance --governance-type releases --project X --json
gitgo --mode governance --governance-type release-note --project X --message "..."
```

**MCP：** `gitgo_governance_releases(project)` + `gitgo_governance_release_note(project, message)` — 17 tools 总计

**认证：** pytest 189 passed, 1 skipped (10 new)

### P4 完成总结

| 阶段 | 内容 | 测试 |
|------|------|------|
| P4-Pre | correlation_id + batch push + files_changed | 0 new (foundation) |
| P4-A | Suggestion Quality Metrics | 20 new |
| P4-B | Change Pattern Detection | 16 new |
| P4-C | Semantic Change Graph | 13 new |
| P4-D | Release Reasoning | 10 new |
| **总计** | **4 治理模块 + 1 数据基础** | **59 new tests** |

**MCP tools 总数：** 17（+4 governance tools over baseline）
**后端:** backend/core/governance/ — 5 files, ~500 lines

---

### P4-B：变更模式检测（v0.18）

**核心文件：**
- `backend/core/governance/patterns.py` — 模式检测引擎（~140行）
- `tests/test_patterns.py` — 16 个测试

**三个检测器：**

| 检测器 | 数据源 | 输出 |
|--------|--------|------|
| `detect_co_changing` | formalize detail 的 `files_changed` | 跨目录共变配对 (adapters/ ⇄ tests/) |
| `detect_type_clusters` | formalize detail 的 `commit` + `source_indices` | 类型分布 + 多源合并率 |
| `detect_trial_impact` | triage_accept + 同 correlation_id 后续 scan | accept 后触发 workspace 变更的概率 |

**CLI：**
```bash
gitgo --mode governance --governance-type patterns --project X --json
```

**MCP：** `gitgo_governance_patterns(project)` — 14 tools 总计

**认证：** pytest 166 passed, 1 skipped (16 new)

---

### P4-A：建议质量度量（v0.17）

**核心文件：**
- `backend/core/governance/__init__.py` — 门面 re-export
- `backend/core/governance/quality.py` — 度量引擎（~160行）
- `tests/test_quality.py` — 20 个测试

**CLI：**
```bash
gitgo --mode governance --governance-type quality --project X --json
```

**MCP：** `gitgo_governance_quality(project)` — 13 tools 总计

**关键设计决策：**
- 仅用 indices Jaccard 重叠度，不做 message 文本比较
- 通过 `correlation_id` 匹配 suggest 记录与执行记录
- 支持 `add_suggestion()` 直存模式（ai_proposal + human_decision 在同一记录）
- 空历史返回空报告而非报错

**认证：** pytest 150 passed, 1 skipped (20 new)

---

## 关键修复记录

### 1. 返回崩溃（6 轮迭代最终修复）

**现象**：点击面包屑返回项目列表或按 Esc → app 崩溃，exit code `-1073740791` (STATUS_STACK_BUFFER_OVERRUN)

**根因**：Qt 事件链重入。`shiboken6.delete()` 同步销毁 widget → `processEvents()` 继续处理当前事件链 → 目标 widget 已删除 → 栈缓冲区溢出

**失败方案**：
- `deleteLater()` + guard → 仍崩（延迟后的 Qt 清理事件处理时 widget 已部分析构）
- `shiboken6.delete()` 同步删除 → 仍崩（在当前事件链中销毁）
- `QTimer.singleShot(0, shiboken6.delete)` 延迟删除 → 仍崩（下一轮事件循环中销毁，但仍有残留事件）

**最终成功方案**（`frontend/main_window.py` `_back_to_list()`）：

```python
# 1. 先停动画
if self._page_anim:
    self._page_anim.stop()

if self.workspace:
    ws = self.workspace
    self.workspace = None

    # 2. 断开 scroll 信号
    ws.commit_scroll.verticalScrollBar().valueChanged.disconnect()

    # 3. 先切换页面，再移除 workspace
    self.stack.setCurrentIndex(0)   # ← 先切页！
    self.stack.removeWidget(ws)

    # 4. 隐藏 + 脱离 widget 树。不调用 deleteLater/delete
    ws.hide()
    ws.setParent(None)  # GC 自然回收

# 5. 更新项目列表页面
self.project_list._refresh_table()
```

**关键点**：
- `setCurrentIndex(0)` 必须在 `removeWidget` 之前
- 不使用任何主动 C++ 删除（`deleteLater` / `shiboken6.delete`）
- `setParent(None)` 彻底脱离 widget 树，Python GC 自然回收

### 2. 提交区重构 — CommitCanvas

**旧架构**：两个独立 QScrollArea（ws/fm）中间夹 CommitConnector

**新架构**（`frontend/widgets.py`）：

```python
class CommitCanvas(QWidget):
    # 内部 QHBoxLayout(spacing=52)
    #   ws_column (左, minWidth=148) — WorkspaceCommitBox
    #   fm_column (右, stretch=1)    — FormalCommitBox
    # paintEvent: 从 ws_column.right() → fm_column.left() 画贝塞尔
```

**关键设计**：
- `lo.setSpacing(52)` — 52px 贝塞尔线通道（之前 `setSpacing(0)` 导致 gap=1px 线不可见）
- `p.setBrush(Qt.NoBrush)` — 只描边不填充（否则有弧形阴影）
- `try/finally: p.end()` — QPainter 异常安全
- `_refresh_commit_lines()` — 通过 `mapTo(canvas, rect().center()).y()` 计算坐标
- scroll bar `valueChanged` 连接刷新

### 3. 合并流程修正

**旧流程**：选中 ws commits → 弹出 QDialog 编辑 → 确认提交

**新流程**（`frontend/workspace/commits.py`）：
```python
# _merge_selected: 模板填入 msg_box
self.msg_box.setPlainText(template)
self._merging = True
self._merge_indices = set(...)

# _submit_commit_message: 检查 _merging 标志
if merging:
    fc = step_create_formal_commit(selected_indices=..., message=msg)
    # 合并模式逻辑
else:
    fc = submit_commit_message(...)  # 直接提交模式
```

### 4. 其他修复

| 问题 | 文件 | 方案 |
|------|------|------|
| QPointF NameError | widgets.py | 从 QtCore 导入 |
| QHBoxLayout NameError | widgets.py | 补充导入 |
| paintEvent QPainter 残留 | widgets.py | try/finally p.end() |
| 主题切换 CommitBox 颜色残留 | theme.py | 遍历 Box 调 _apply_style() |
| 树引导线 State_Open 不可靠 | builder.py | itemAt().isExpanded() |
| 树引导线颜色 | builder.py | 改用 get_theme()["bdr2"] |
| subprocess 闪窗 | local_git_runner.py | CREATE_NO_WINDOW |
| overflow:hidden 无效 QSS | themes/__init__.py | 删除 |
| showEvent QTimer 延迟回调 | panel.py | 直接调用 _update_action_bar() |
| Esc 快捷键 | main_window.py | 从 WorkspacePanel 移到 MainWindow 注册 |

---

## Debug 基础设施

- `debug_entry.py` — Python try/except 包裹 gui_main，异常时 `input("按 Enter")`
- `run_debug.bat` — bat 包裹 exe，C++ segfault 时控制台也存活
- 构建：`python build.py --debug` → `dist/gitgo_debug.exe` + `dist/run_debug.bat`

---

## v0.8 模块化重构（2026-05-11）

| 模块 | 拆分前 | 拆分后 |
|------|--------|--------|
| Builder | `builder.py`(653) | `builder.py`(162) + `explorer.py`(191) + `workshop_tab.py`(203) + `incoming_tab.py`(100) |
| Widgets | `widgets.py`(265) | `widgets.py`(3门面) + `commit_box.py`(144) + `commit_canvas.py`(74) |
| Themes | `__init__.py`(279) | `__init__.py`(93) + `qss.py`(186) |
| CUI | `cui_main.py`(636) | `cui_main.py`(15门面) + `cui/`(583, 4文件) |

---

## v0.12 状态驱动闭环（2026-05-13）

P1 完成后分析发现前端直接变异 core 数据（`formal_commits.pop/append`、`selected_workspace.add/discard`、`fc.message =` 等），绕过 SyncSession step 方法。闭合四个缺口：

| Gap | 修复 |
|-----|------|
| formal_commits 直接 mutation | 新增 `step_delete_formal` / `step_edit_formal_message` / `step_edit_formal_number` / `step_dissolve_formal` / `step_clear_formal_sources` / `step_add_incoming_formal` |
| selected_workspace 直接操作 | 新增 `step_toggle_workspace_selection(mode)` — 统一 toggle/range/single 三种选择模式 |
| Push 路径分裂 | `PushWorker` 统一走 `step_push()`，`on_security_warning` 接线替代直接调 `push_to_backup()` |
| `on_stage_changed` 未使用 | 接线到 `_apply_stage()`（线程安全）+ `_refresh_button_states()` 集中推导 5 按钮状态，消除 ~20 处散落 `setEnabled` |
| `submit_commit_message` 绕过 | 改为委托 `step_create_formal_commit(message=, selected_indices=set())` |

修改文件：`sync_session.py` (+180行) / `workers.py` (-20行) / `commits.py` / `trial.py` / `syncpush.py` / `panel.py` / `submitter.py`

## v0.11 代码拆分（2026-05-13）

Phase 1 完成后，按耦合度分析将 3 个 >300 行文件拆分为模块：

| 源文件 | 拆分前 | 拆分后 | 新建文件 |
|--------|--------|--------|---------|
| `__main__.py` | 643行 | ~180行 | `cli/commands.py` (400行) + `cli/__init__.py` |
| `core/operations/sync.py` | 324行 | ~195行 | `core/operations/security.py` (120行) |
| `frontend/project_list.py` | 543行 | ~310行 | `frontend/project_edit_dialog.py` (210行) |

---
## v0.14 UI 交接：PanelState 显式化 + 文件重组（2026-05-13）

### PanelState — Mixin 共享状态的显式容器

**动机**：WorkspacePanel 通过 10 个 Mixin 类（Builder / Explorer / WorkshopTab / IncomingTab / Commit / Theme / SyncPush / Trial / Remotes / History）组合而成，Mixin 间共享 39+ 个 `self.xxx` 属性。这种隐式契约导致新开发者无法判断"这个属性谁写的、谁在消费"。

**方案**：将全部跨 Mixin 属性提取到显式的 `PanelState` 对象中，每个属性标注 Producer (P) 和 Consumer (C)。

```python
# frontend/workspace/panel_state.py
class PanelState:
    """WorkspacePanel 的所有跨 Mixin 共享状态。
    rule: self.state.xxx — 任何被 2+ 个 Mixin 访问的属性必须在此定义。
    """
    def __init__(self):
        # ── 业务状态 (P: panel.py) ──────────────────────────
        self.config: Config | None = None           # P: panel, C: all
        self.project: ProjectConfig | None = None   # P: panel, C: all
        self.session: SyncSession | None = None     # P: panel, C: ALL mixins

        # ── 选中状态 ───────────────────────────────────────
        self.selected_formal: int | None = None     # P: panel.init, C: commits/syncpush/trial
        self.selected_incoming: int | None = None   # P: panel.init, C: trial/incoming

        # ── 流程状态 ───────────────────────────────────────
        self._merging: bool = False                 # P: commits, C: commits
        self._merge_indices: set[int] = set()       # P: commits, C: commits
        # ... 39+ attributes total
```

**访问规范**：
- 所有 Mixin 内通过 `self.state.xxx` 访问共享属性
- WorkspacePanel `__init__` 中 `self.state = PanelState()` 创建容器
- 新的跨 Mixin 属性必须先在 `PanelState.__init__` 中定义并标注 P/C
- 局部属性（只在一个 Mixin 内使用）仍用 `self._xxx`，不放入 PanelState

**修改文件**（11 文件）：
- `frontend/workspace/panel_state.py` — **新建**（PanelState 类定义，39+ 属性）
- `frontend/workspace/panel.py` — `self.state = PanelState()` 创建，全部共享属性改 `self.state.xxx`
- `frontend/workspace/builder.py` — 全部属性改 `self.state.xxx`
- `frontend/workspace/commits.py` — 同上 + 修复 `from backend.core.submitter import submit_commit_message`
- `frontend/workspace/syncpush.py` — 同上
- `frontend/workspace/trial.py` — 同上
- `frontend/workspace/remotes.py` — 同上
- `frontend/workspace/history.py` — 同上
- `frontend/workspace/explorer.py` — 同上
- `frontend/workspace/workshop_tab.py` — 同上
- `frontend/workspace/incoming_tab.py` — 同上
- `frontend/workspace/theme.py` — 同上

### 项目文件重组（两层架构）

**旧结构**：根目录 18 个 `.py` 文件 + 10 个子目录，后端、接口、工具混放。

**新结构**：引擎层 / 接口层两层分离：

| 层 | 目录 | 说明 |
|----|------|------|
| 引擎层 | `backend/core/` | 业务引擎 (sync_session / config / i18n / history / operations / daemon) |
| 引擎层 | `backend/adapters/` | 文件/Git 适配器 (Local/SSH) |
| 引擎层 | `backend/models/` | 共享数据模型 |
| 引擎层 | `backend/remote/` | 外部 API (GitHub/GitLab) |
| 接口层 | `frontend/` | 人类 GUI — Qt 桌面 |
| 接口层 | `cui/` | 人类 TUI — Rich 终端 |
| 接口层 | `cli/` | Agent 接口 — headless CLI |

**文件移动清单**（~20 文件）：
- `config.py` → `backend/core/config.py`
- `i18n.py` → `backend/core/i18n.py`
- `history.py` → `backend/core/history.py`
- `plugin.py` → `backend/core/plugin.py`
- `plugin_loader.py` → `backend/core/plugin_loader.py`
- `migrate.py` → `backend/core/migrate.py`
- `core/` → `backend/core/`（sync_session / daemon / operations）
- `adapters/` → `backend/adapters/`
- `models/` → `backend/models/`
- `remote/` → `backend/remote/`
- `gui_main.py` → `frontend/gui_main.py`
- `debug_entry.py` → `frontend/debug_entry.py`
- `cui_main.py` → `cui/main.py`
- `debug_launcher.py` → `scripts/debug_launcher.py`

**根目录保留**：`__init__.py` / `__main__.py` / `build.py` / `mcp_server.py` / `requirements.txt`

**导入路径变更**（全员统一 `from backend.xxx import`）：
```python
from config import ...           →  from backend.core.config import ...
from i18n import ...             →  from backend.core.i18n import ...
from history import ...          →  from backend.core.history import ...
from core import ...             →  from backend.core import ...
from models import ...           →  from backend.models import ...
from adapters import ...         →  from backend.adapters import ...
from gui_main import ...         →  from frontend.gui_main import ...
from cui_main import ...         →  from cui.main import ...
```

---
## 文件地图（v0.14）

```
gitgo/
├── __init__.py                # 包标记
├── __main__.py                # CLI 入口 (~180行，延迟 import gui/cui)
├── build.py                   # PyInstaller 两阶段打包
├── mcp_server.py              # P2-D MCP Server (FastMCP, 9 tools)
├── requirements.txt
│
├── backend/                   # 引擎层 — 全项目共用的纯逻辑
│   ├── __init__.py            # 门面 re-export
│   ├── core/                  #   业务引擎
│   │   ├── __init__.py
│   │   ├── sync_session.py    #   纯 Python 状态机（全 step 方法覆盖）
│   │   ├── config.py          #   Config / ConfigManager / ProjectConfig
│   │   ├── history.py         #   HistoryManager + HistoryEntry (9 op types)
│   │   ├── i18n.py            #   _tr() / load_language / available_languages
│   │   ├── plugin.py          #   SyncPlugin 基类 + 7 钩子
│   │   ├── plugin_loader.py   #   PluginOrchestrator 发现/加载
│   │   ├── migrate.py         #   旧配置自动迁移
│   │   ├── scanner.py         #   FileScanner — walk + 树构建
│   │   ├── submitter.py       #   submit_commit_message — 验证 + 创建 formal
│   │   ├── daemon/            #   P2-C 持久守护进程
│   │   │   ├── __init__.py    #   run_daemon() 主循环 + 事件调度
│   │   │   ├── watcher.py     #   WorkspaceWatcher (watchdog)
│   │   │   ├── poller.py      #   TrialPoller (定时轮询)
│   │   │   └── commands.py    #   CommandReader (stdin JSON)
│   │   └── operations/        #   底层 git 操作
│   │       ├── __init__.py    #   门面 re-export
│   │       ├── models.py      #   FileEntry / CommitInfo
│   │       ├── scan.py        #   文件扫描 + SHA256 对比
│   │       ├── git.py         #   git log / commit template / validation
│   │       ├── sync.py        #   sync_to_backup / push_to_backup
│   │       ├── security.py    #   安全检查（独立模块）
│   │       └── utils.py       #   _entry_to_dict 辅助
│   ├── adapters/              #   文件/Git 适配器 (Local/SSH)
│   ├── models/                #   共享数据模型 (RepoNode/FileAccess/IncomingChange)
│   └── remote/                #   外部 API (GitHub/GitLab, Phase 5)
│
├── cli/                       # Agent 接口 — headless CLI verbs
│   ├── __init__.py            # 门面 re-export
│   └── commands.py            # _cmd_list/status/sync/daemon/trial/formalize/scan/push/session + _init_session
│
├── frontend/                  # 人类 GUI — Qt 桌面
│   ├── gui_main.py            # GUI 薄入口
│   ├── debug_entry.py         # Debug 版保活入口
│   ├── main_window.py         # MainWindow + Esc + _back_to_list
│   ├── project_list.py        # 项目列表面板（表格+定时刷新+右键菜单）
│   ├── project_edit_dialog.py # _ProjectEditDialog
│   ├── settings.py            # SettingsDialog
│   ├── workers.py             # 5 种后台 Worker (Scan/Sync/Push/TrialCheck/Triage)
│   ├── commit_box.py          # CommitBox / WorkspaceCommitBox / FormalCommitBox
│   ├── commit_canvas.py       # CommitCanvas（统一提交区）
│   ├── incoming_card.py       # IncomingChangeCard
│   ├── widgets.py             # 门面 re-export
│   └── workspace/             # 12 文件 Mixin 组合
│       ├── __init__.py
│       ├── panel.py           # WorkspacePanel 聚合（10 Mixin）
│       ├── panel_state.py     # ★ PanelState — 跨 Mixin 共享状态显式容器
│       ├── builder.py         # BuilderMixin(Explorer+Workshop+Incoming) 核心
│       ├── explorer.py        # ExplorerMixin + _BranchLineStyle
│       ├── workshop_tab.py    # WorkshopTabMixin
│       ├── incoming_tab.py    # IncomingTabMixin
│       ├── theme.py           # ThemeMixin
│       ├── commits.py         # CommitMixin（合并+连接线）
│       ├── syncpush.py        # SyncPushMixin
│       ├── trial.py           # TrialMixin
│       ├── remotes.py         # RemotesMixin
│       └── history.py         # HistoryMixin
│
├── cui/                       # 人类 TUI — Rich 终端
│   ├── __init__.py
│   ├── main.py                # CUI 门面 (15行，原 cui_main.py)
│   ├── main_flow.py           # 主流程 + entry()
│   ├── projects.py            # 项目 CRUD
│   ├── display.py             # Rich 表格渲染
│   └── workflow.py            # 工作流步骤
│
├── themes/                    # 主题系统
│   ├── __init__.py            # ThemeColors 令牌 + 门面
│   ├── qss.py                 # build_qss() 动态 QSS
│   ├── dark.py / light.py     # 颜色字典
├── locales/                   # 翻译文件 (zh.json / en.json)
├── plugins/                   # 用户插件
├── scripts/                   # 工具脚本
│   ├── verify_headless.sh     # Phase 1 集成验证 (13 checks)
│   └── debug_launcher.py      # 外部调试启动器
├── tests/                     # 测试套件 (97/97)
├── docs/                      # 文档
│   ├── HANDOFF.md             # 本文件
│   ├── CLAUDE.md              # Claude Code 指南
│   ├── VERSION.md             # 版本记录
│   ├── Phase1_RuntimeFoundation.md  # Phase 1 完整计划
│   ├── GOVERNANCE_STATE.md    # Governance 状态机定义
│   ├── 前端设计报告.md
│   ├── 后端设计报告.md
│   └── iterations/            # 迭代计划 + 归档
└── dist/                      # 构建产物 (gitgo.exe, 54.3 MB)
```

---

## 已知残留问题

- **进程退出时偶发 segfault**：无 Python frame，纯 C++ 清理阶段，不影响功能使用
- **Action Bar 首次渲染**：偶有不到位，`showEvent` 中直接调 `_update_action_bar()` 已基本解决
- **贝塞尔线样式**：功能正常但弧线不够美观，待后续优化
