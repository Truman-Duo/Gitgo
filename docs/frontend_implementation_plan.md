# Gitgo 前端实现计划

## 总览

基于 2026-06-08 的 gap 分析，现有前端覆盖 4 个 Tab：Workshop、Incoming、Remotes、History。后端 43 个 MCP 工具中约 15 个已有前端通路。剩余缺口集中在 P6 治理体系（Contract / Identity / Lesson / Authorship / Memory）和 Action Bar 6 个空壳按钮。

本计划分三轮推进，每轮约 2 周工作量。**先触达最短改动路径，再铺治理能力。**

---

## 第一轮：填补功能断点（P0 — 3 个改动，预计 2 天）

目标：把 Workshop Tab 底部按钮行补全，不新增文件，不改 PanelState 结构。

### 1-1. Authorship Toggle 接入 Push 流程

**现状**：PushWorker 调用 `step_push(skip_scan)`，不传 authorship 参数。MCP 层 `gitgo_push` 在 `step_push` 前调用 `apply_authorship_filter(session)` 完成清洗。

**改动文件**：`frontend/workspace/workshop_tab.py`、`frontend/workspace/syncpush.py`

**改动内容**：

```
workshop_tab.py — _build_workshop_bottom_row():
  在 push_btn 之后、delete_formal_btn 之前，插入：
  ├── QCheckBox("Strip AI authorship")
  │   ├── setChecked(True)  # 默认开启
  │   └── setProperty("variant", "ghost")
  └── state.strip_authorship_checkbox ← 引用

syncpush.py — _start_push():
  def _start_push(self):
      # ★ 新增：push 前执行 authorship filter
      if getattr(self.state, 'strip_authorship_checkbox', None) \
         and self.state.strip_authorship_checkbox.isChecked():
          from backend.core.authorship import apply_authorship_filter
          apply_authorship_filter(self.state.session, aggressive=False)
          self._log(_tr("authorship.stripped", "AI 痕迹已清洗"))
      # ... 后续逻辑不变
```

**不触及**：PanelState（读 checkbox 状态而非存 state 属性）、PushWorker、SyncSession。

---

### 1-2. Undo Merge 按钮接通 step_dissolve

**现状**：`builder.py:108` `_on_action_undo()` 只打印日志 `"Undo: dissolve formal commit — not implemented in UI"`。`CommitMixin._dissolve_formal_commit()` 已完整实现（含确认弹窗）。

**改动文件**：`frontend/workspace/builder.py`

**改动内容**：

```python
# _on_action_undo() 中 Workshop 分支从：
self._log(_tr("action.undo_merge", "...not implemented in UI"))
# 改为：
if self.state.selected_formal is not None:
    self._dissolve_formal_commit(self.state.selected_formal)
else:
    self._log(_tr("action.undo_none", "没有选中可撤销的正式 commit"))
```

**不触及**：PanelState、其他 Mixin。

---

### 1-3. Template 下拉选择器

**现状**：`commit_format.template_name` 存储在 `ProjectConfig` 中，`build_commit_template()` 已读取该值。Workshop Tab 无 UI 控件。

**改动文件**：`frontend/workspace/workshop_tab.py`、`frontend/workspace/commits.py`

**改动内容**：

```
workshop_tab.py — _build_workshop_bottom_row():
  在 Authorship checkbox 旁插入：
  ├── QLabel("Template:")
  └── QComboBox()
      ├── 填充: TemplateManager.list_templates()
      ├── 默认选中: project.commit_format.template_name
      └── currentTextChanged → 更新 project.commit_format.template_name

模板切换后无需刷新 UI — 下次 _merge_selected() 调用 build_commit_template() 时自动生效。
```

**不触及**：PanelState（模板名写回 `ProjectConfig`，不经 PanelState）。

---

## 第二轮：治理可见性（P1 — 1 个新 Mixin + 1 个子 Tab，预计 5 天）

目标：新增 Governance Tab，把 P6 治理体系从 CLI/MCP 搬到 GUI。History Tab 补充治理事件子页。

### 2-1. Governance Tab（新文件：`frontend/workspace/governance.py`）

**注册**：`builder.py` `_init_ui()` 新增第 5 个 Tab。

**PanelState 新增字段**（Producer 均为 GovernanceMixin）：

```python
# panel_state.py 新增：
self.contract_data: dict = {}     # P: governance, C: governance
self.integrity_status: dict = {}  # P: governance, C: governance
self.lesson_data: dict = {}       # P: governance, C: governance
```

**三个 Section 卡片**：

| Section | 后端 API | 数据流 |
|---------|----------|--------|
| **Contract** | `ContractManager.load(ws_path).to_dict()` | contract.features 列表 + contract.tech_stack + `detect_drift()` 告警 |
| **Identity** | `_run_integrity_checks(entries, ws_path, project)` | 3 类检查结果（mass_override / identity_deletion / structure_collapse）→ 百分比 + 告警列表 |
| **Lesson** | `LessonManager.load_abstract(ws_path)` + `load_instance(ws_path, name)` + `load_pending(ws_path, name)` | 计数卡片 + 最近 lessons 列表 |

**交互**：
- Contract: 「View Full Contract」弹窗 → `QDialog` 内 `QPlainTextEdit` 显示完整 YAML
- Contract: 「Force Update」按钮 → `ContractManager.update_feature()` 调用
- Identity: 「View Diff」→ 对变更文件调用 `get_file_diff()`
- Identity: 「Restore Snapshot」→ `snapshot_tool_memories().restore()`
- Lesson: 「View All」→ `QDialog` 内列表，支持搜索（`LessonManager.search()`）

**QSS 设计约束**：
- 健康状态用 property selector：`setProperty("severity", "ok"|"warning"|"danger")`
- Integrity < 80% → danger（红边框），< 95% → warning（黄），>= 95% → ok（绿）
- 所有颜色走 `get_theme()` token，不硬编码

**Mixin 类结构**：

```python
class GovernanceMixin:
    """Governance Tab — 合约 / 完整性 / 知识"""
    
    def _build_governance_tab(self) -> QWidget:
        scroll = QScrollArea()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self._build_contract_card())
        layout.addWidget(self._build_identity_card())
        layout.addWidget(self._build_lesson_card())
        layout.addStretch()
        scroll.setWidget(container)
        self._load_governance_data()  # 打开 tab 时自动加载
        return scroll
    
    def _load_governance_data(self):
        """从后端加载所有治理数据，写入 PanelState"""
        ws_path = Path(self.state.project.workspace_path)
        
        # Contract
        contract = ContractManager.load(ws_path)
        self.state.contract_data = contract.to_dict() if contract else {}
        
        # Identity — 运行完整性检查
        warnings = _run_integrity_checks(
            self.state.session.entries, ws_path, self.state.project)
        self.state.integrity_status = {
            "warnings": warnings,
            "score": self._calc_integrity_score(warnings),
        }
        
        # Lessons
        abstract = LessonManager.load_abstract(ws_path)
        instance = LessonManager.load_instance(ws_path, self.state.project.name)
        pending = LessonManager.load_pending(ws_path, self.state.project.name)
        self.state.lesson_data = {
            "abstract": len(abstract), "instance": len(instance),
            "pending": len(pending), "recent": instance[:5],
        }
        
        self._refresh_governance_cards()
```

---

### 2-2. History Tab 治理事件子页

**现状**：`history.py` 只展示 commit 历史（依赖 `HistoryEntry.commit_message` 非空）。`HistoryManager` 存储了操作级别的 `HistoryEntry`（operation="sync"/"push"/"triage_*" 等），但 `_populate_history()` 过滤掉了。

**改动文件**：`frontend/workspace/history.py`、`frontend/workspace/builder.py`

**方案**：在 History panel 顶端加 `QComboBox` 切换：

```
[▼ All Events]  [▼ Filter by type]
   ├─ Commits (default)
   └─ Governance Events
```

当选择 "Governance Events" 时，`_populate_history()` 改为展示所有 `operation` 字段所在的条目，按事件类型着色：

```python
_GOV_EVENT_COLORS = {
    "sync":              lambda t: t.success_txt,      # 绿色
    "push":              lambda t: t.blue,             # 蓝色
    "governance_pushed": lambda t: t.teal,             # 青色
    "governance_synced": lambda t: t.success,          # 绿色
    "governance_drift":  lambda t: t.danger_txt,       # 红色
    "governance_contract_updated": lambda t: t.amber,  # 黄色
    "governance_lesson": lambda t: t.purple or "#8b5cf6",  # 紫色
    "governance_memory_snapshot": lambda t: t.txt3,    # 灰色
}
```

每条事件显示：时间 | 类型 pill | 项目名 | 详情摘要。

**不新增文件，不改 PanelState。**

---

## 第三轮：知识管理 + 高级功能（P2-P3，预计 5 天）

### 3-1. Lesson 弹出式管理对话框

**目标**：在 Governance Tab 的 Lesson 卡片「View All」按钮上挂 `QLessonDialog`。

**新文件**：`frontend/lesson_dialog.py`

**功能**：
- `QTabWidget` 三个子页：Abstract / Instance / Pending
- Instance 页支持搜索（`QLabel` + `QLineEdit` → `LessonManager.search_instance()`）
- 每条 Lesson 显示：类别 pill / 严重度 pill / trigger / rule
- 右键菜单：Verify / Promote / Delete
- Promote 确认弹窗（选择目标 tech_stack）

### 3-2. Export 功能

**目标**：填实 Workshop Tab Action Bar 的 Export Tasks 按钮。

**方案**：点击 → `QDialog` 选择导出范围（全量 / 仅 formal / 仅 workspace），调用 `StateReader` 或直接序列化 `self.state.session` 数据，写入用户选择的路径（`QFileDialog`）。

### 3-3. Quality 分数卡片

**目标**：在 Governance Tab 顶部加一行迷你指标。

**后端**：`compute_quality_metrics()` in `backend/core/governance/quality.py`。

**UI**：一行 4 个 metric pill：变更覆盖率 / 建议采纳率 / 漂移告警数 / 最近发布。

---

## 实现顺序总览

```
Day 1-2   ██████████  Round 1  (3 改动, 0 新文件)
          ① Authorship checkbox 接入 Push
          ② Undo Merge 接通 dissolve
          ③ Template selector

Day 3-7   ████████████████████████████  Round 2  (1 新 Mixin, 1 Sub-tab)
          ④ Governance Tab: Contract + Identity + Lesson 卡片
          ⑤ History Tab 治理事件子页
          ⑥ PanelState 新增 3 字段 + builder.py 注册第 5 Tab

Day 8-12  ████████████████████████████  Round 3  (弹窗 + 度量)
          ⑦ Lesson Dialog（View All → QDialog）
          ⑧ Export 对话框（填实 Action Bar）
          ⑨ Quality 迷你指标
```

## 约束重申

- 所有新颜色走 `get_theme()` token，禁止 `#xxx` 字面量
- 跨 Mixin 属性 ➜ `PanelState.__init__` 声明并标注 P/C
- 状态切换用 `setProperty` + `unpolish/polish`，不用 `setStyleSheet`
- i18n：所有用户可见字符串走 `_tr("key", "fallback")`
- 布局不走 `move()` / `setFixedHeight`，只用 `setMinimumHeight`
- 组件只暴露 Signal，不暴露子控件

## 风险

- `_run_integrity_checks` 是 `guard.py` 的私有函数，未在 `__init__.py` 导出。需要在 `backend/core/identity/__init__.py` 加一行 `from .guard import _run_integrity_checks as run_integrity_checks` 或在前端直接 `from backend.core.identity.guard import _run_integrity_checks`。
- `detect_drift()` 需要 `changed_files` 参数，应从当前 scan entries 中提取。Governance Tab 需复用 scan 结果。
- `snapshot_tool_memories()` 的参数包含 `project`（ProjectConfig 对象），可在后端调用时传入。
