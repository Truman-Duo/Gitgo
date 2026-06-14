# Claude Code 执行规格 — Gitgo 前端补全

> 本文档供 Claude Code 消费。每个 Task 自包含，描述从现状到目标的完整改动。

---

# Task A：Authorship Toggle 接入 Push 流程

## 你要做的事

在 Workshop Tab 底部按钮行，Push 按钮旁边加一个 checkbox，勾选后 Push 时自动清洗 AI 痕迹。

## 读上下文

```
frontend/workspace/workshop_tab.py       — _build_workshop_bottom_row()
frontend/workspace/syncpush.py           — _start_push()
backend/core/authorship.py               — apply_authorship_filter()
frontend/workspace/panel_state.py        — 了解 PanelState 结构
```

## 改动 1 / 2：`workshop_tab.py` — 加 checkbox

位置：`_build_workshop_bottom_row` 方法，push_btn 添加之后、delete_formal_btn 之前。

在当前代码：
```python
        self.state.push_btn.clicked.connect(self._start_push)
        lo.addWidget(self.state.push_btn)

        self.state.delete_formal_btn = QPushButton("✕")
```

改为：
```python
        self.state.push_btn.clicked.connect(self._start_push)
        lo.addWidget(self.state.push_btn)

        # ── Authorship toggle ──
        from PySide6.QtWidgets import QCheckBox
        self.state.authorship_cb = QCheckBox(
            _tr("action.strip_authorship", "清洗 AI 痕迹"))
        self.state.authorship_cb.setChecked(True)
        self.state.authorship_cb.setToolTip(
            _tr("action.strip_authorship_tt",
                "Push 前清除 commit message 和代码注释中的 AI 协作痕迹"))
        lo.addWidget(self.state.authorship_cb)

        self.state.delete_formal_btn = QPushButton("✕")
```

## 改动 2 / 2：`syncpush.py` — Push 前执行清洗

位置：`_start_push` 方法，`self.state.push_worker = PushWorker(...)` 之前。

在：
```python
        self.state.progress_bar.setValue(0)
        self.state.progress_label.setText(_tr("exec.pushing", "正在 push 到远程..."))
        self._log(_tr("exec.pushing_log", "开始 push..."))
```

和：
```python
        self.state.push_worker = PushWorker(self.state.session)
```

之间插入：
```python
        # Authorship 清洗
        if getattr(self.state, 'authorship_cb', None) \
           and self.state.authorship_cb.isChecked():
            from backend.core.authorship import apply_authorship_filter
            stats = apply_authorship_filter(self.state.session, aggressive=False)
            self._log(_tr("authorship.stripped",
                         "AI 痕迹已清洗 ({n} 处)").format(n=stats.get("total", 0)))
```

⚠️ 注意：`_force_push` 方法也需要加同样的代码块。

## i18n

```
locales/zh.json 新增：
  "action.strip_authorship": "清洗 AI 痕迹",
  "action.strip_authorship_tt": "Push 前清除 commit message 和代码注释中的 AI 协作痕迹",
  "authorship.stripped": "AI 痕迹已清洗 ({n} 处)"

locales/en.json 新增：
  "action.strip_authorship": "Strip AI authorship",
  "action.strip_authorship_tt": "Remove AI collaboration traces from commit messages and code comments before push",
  "authorship.stripped": "AI authorship stripped ({n} items)"
```

## 验证

1. 打开项目 → Workshop Tab → 底部应出现 checkbox
2. 默认勾选
3. sync 后 push → 日志应显示 "AI 痕迹已清洗"（或至少不报错）
4. 取消勾选 → push 应跳过清洗步骤

---

# Task B：Undo Merge 按钮接通 dissolve

## 你要做的事

Workshop Tab 的 Action Bar 上「Undo Merge」按钮目前只打日志，不改任何东西。让它真正执行 dissolve 操作。

## 读上下文

```
frontend/workspace/builder.py           — _on_action_undo()
frontend/workspace/commits.py           — _dissolve_formal_commit() 已有完整实现
```

## 改动：`builder.py`

位置：`_on_action_undo` 方法，Workshop tab (idx==0) 分支。

当前代码：
```python
    def _on_action_undo(self):
        idx = self.state.tab_bar.currentIndex()
        if idx == 0:
            self._log(_tr("action.undo_merge", "Undo: dissolve formal commit — not implemented in UI"))
```

改为：
```python
    def _on_action_undo(self):
        idx = self.state.tab_bar.currentIndex()
        if idx == 0:
            if self.state.selected_formal is not None:
                self._dissolve_formal_commit(self.state.selected_formal)
            else:
                self._log(_tr("action.undo_none", "没有可撤销的正式 commit"))
```

⚠️ 注意：`_dissolve_formal_commit` 来自 `CommitMixin`，`BuilderMixin` 通过 `WorkspacePanel` 多重继承链能访问到。

## 不触及其他代码

`_dissolve_formal_commit` 内部已有确认弹窗、source_indices 检查，不需要额外处理。Undo Merge 只在 idx==0 (Workshop) 时可用，其他 Tab 的 Undo 行为保持不变。

## i18n

```
locales/zh.json：
  "action.undo_none": "没有可撤销的正式 commit"

locales/en.json：
  "action.undo_none": "No formal commit to undo"
```

---

# Task C：Template 下拉选择器

## 你要做的事

在 Workshop Tab 底部，Authorship checkbox 旁边加一个 template 下拉选择器，让用户选择 commit message 模板。

## 读上下文

```
frontend/workspace/workshop_tab.py       — _build_workshop_bottom_row()
backend/core/template_manager.py         — TemplateManager.load()
frontend/workspace/commits.py            — _merge_selected() 调用 build_commit_template()
```

## 改动：`workshop_tab.py`

在 `_build_workshop_bottom_row` 方法，authorship_cb 之后插入：

```python
        # ── Template selector ──
        from PySide6.QtWidgets import QComboBox
        from backend.core.template_manager import TemplateManager
        tmpl_label = QLabel(_tr("action.template", "Template:"))
        tmpl_label.setStyleSheet(f"font-size:11px;color:{get_theme().txt3};")
        lo.addWidget(tmpl_label)
        self.state.template_combo = QComboBox()
        templates = TemplateManager.load()
        self.state.template_combo.addItems([t.name for t in templates])
        current = self.state.project.commit_format.get("template_name", "default")
        if current in templates:
            self.state.template_combo.setCurrentText(current)
        self.state.template_combo.setToolTip(
            _tr("action.template_tt", "选择 commit message 模板"))
        self.state.template_combo.currentTextChanged.connect(
            lambda name: self.state.project.commit_format.update(
                {"template_name": name}))
        lo.addWidget(self.state.template_combo)
```

## 不要做的事

不要修改 `_merge_selected` 或 `build_commit_template`——它们已经读取 `project.commit_format.template_name`。

## 同步逻辑

`SyncPushMixin._start_sync` 用 `project.commit_format.template_name` 参数传模板名到 SyncWorker。SyncWorker 的 `step_sync` 读取该参数。确认当前 `SyncWorker.__init__` 没有显式传模板——如果有缺失需补一行：
```python
self.template_name = session.project.commit_format.get("template_name", "default")
```

## i18n

```
zh: "action.template": "模板:", "action.template_tt": "选择 commit message 模板"
en: "action.template": "Template:", "action.template_tt": "Select commit message template"
```
