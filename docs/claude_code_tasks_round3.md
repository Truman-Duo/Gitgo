# Claude Code 执行规格 — Round 3

> 本轮：Bug 修复 + Memory Snapshot 管理 + Lesson CRUD + Contract 更新 + Template CRUD
>
> 0 个新 Tab，改动集中在 Governance Tab 的卡片增强弹窗 + 1 个 Settings 子页。

---

## Task H：修复 Template 下拉 Bug

### 问题

`workshop_tab.py` L149 调用 `TemplateManager.list_templates()` — 该方法不存在。
正确 API 是 `TemplateManager.load()` 返回 `list[CommitTemplate]`。

### 改动：`workshop_tab.py`

位置：`_build_workshop_bottom_row`，Template selector 代码块。

```python
        # ── Template selector ──
        from backend.core.template_manager import TemplateManager
        tmpl_label = QLabel(_tr("action.template", "Template:"))
        tmpl_label.setStyleSheet(f"font-size:11px;color:{get_theme().txt3};")
        lo.addWidget(tmpl_label)
        self.state.template_combo = QComboBox()
        templates = TemplateManager.list_templates()           # ← BUG: 方法不存在
        self.state.template_combo.addItems(templates)
```

改为：

```python
        # ── Template selector ──
        from backend.core.template_manager import TemplateManager
        tmpl_label = QLabel(_tr("action.template", "Template:"))
        tmpl_label.setStyleSheet(f"font-size:11px;color:{get_theme().txt3};")
        lo.addWidget(tmpl_label)
        self.state.template_combo = QComboBox()
        templates = TemplateManager.load()
        template_names = [t.name for t in templates]
        self.state.template_combo.addItems(template_names)
        current = self.state.project.commit_format.get("template_name", "default")
        if current in template_names:
            self.state.template_combo.setCurrentText(current)
```

只改 2 行：`.list_templates()` → `.load()`，然后提取 `.name`。

---

## Task I：Memory Snapshot 管理

### 目标

在 Governance Tab 的 Identity Guard 卡片底部加两个按钮和一个列表弹窗。

### 后端 API

```python
from backend.core.identity.snapshot import (
    snapshot_tool_memories,   # (workspace_path, backup_path, project) → dict
    restore_tool_memories,    # (backup_path, workspace_path, timestamp) → dict
    list_memory_snapshots,    # (backup_path) → list[dict]
)
```

### 改动 1/2：`governance.py` — Identity 卡片加按钮

在 `_build_identity_card` 方法末尾（return card 之前），插入按钮行：

```python
        # ── Memory 按钮行 ──
        btn_row2 = QHBoxLayout()
        snap_btn = QPushButton(_tr("gov.snap_now", "Snapshot Now"))
        snap_btn.setProperty("variant", "ghost")
        snap_btn.clicked.connect(self._on_snapshot_now)
        btn_row2.addWidget(snap_btn)

        list_btn = QPushButton(_tr("gov.snap_list", "Snapshots"))
        list_btn.setProperty("variant", "ghost")
        list_btn.clicked.connect(self._on_list_snapshots)
        btn_row2.addWidget(list_btn)

        restore_btn = QPushButton(_tr("gov.snap_restore", "Restore Latest"))
        restore_btn.setProperty("variant", "ghost")
        restore_btn.clicked.connect(self._on_restore_latest)
        btn_row2.addWidget(restore_btn)
        btn_row2.addStretch()
        card.layout().addLayout(btn_row2)
```

### 改动 2/2：`governance.py` — 三个回调方法

在 `GovernanceMixin` 类末尾添加：

```python
    def _on_snapshot_now(self):
        """手动触发一次记忆快照"""
        from backend.core.identity.snapshot import snapshot_tool_memories
        ws = Path(self.state.project.workspace_path)
        bk = Path(self.state.project.backup_path)
        result = snapshot_tool_memories(ws, bk, self.state.project)
        snapped = result.get("snapped", [])
        if snapped:
            msg = _tr("gov.snap_ok", "已快照: {files}").format(
                files=", ".join(snapped))
        else:
            msg = _tr("gov.snap_empty", "无记忆文件可快照")
        self._log(msg)

    def _on_list_snapshots(self):
        """列出所有快照"""
        from backend.core.identity.snapshot import list_memory_snapshots
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, \
            QPushButton, QMessageBox

        bk = Path(self.state.project.backup_path)
        snaps = list_memory_snapshots(bk)

        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("gov.snap_dialog", "Memory Snapshots"))
        dlg.setMinimumSize(500, 350)
        lo = QVBoxLayout(dlg)

        if not snaps:
            lo.addWidget(QLabel(_tr("gov.snap_none", "没有快照记录")))
        else:
            t = get_theme()
            for s in snaps[:20]:
                card = QFrame()
                card.setStyleSheet(
                    f"background:{t.bg};border:.5px solid {t.bdr};"
                    f"border-radius:4px;padding:8px;margin:2px 0;")
                cl = QVBoxLayout(card)
                cl.setSpacing(2)

                hdr = QHBoxLayout()
                hdr.addWidget(QLabel(
                    f'<b style="color:{t.txt};">{s["source"]}</b>'))
                hdr.addStretch()
                ts = s["timestamp"]
                hdr.addWidget(QLabel(
                    f'<span style="color:{t.txt3};font-size:10px;">{ts}</span>'))
                cl.addLayout(hdr)

                # 恢复按钮
                restore_btn = QPushButton(
                    _tr("gov.restore_this", "Restore"))
                restore_btn.setProperty("variant", "ghost")
                restore_btn.clicked.connect(
                    lambda checked, ts_val=ts:
                    self._do_restore(ts_val) and dlg.accept())
                cl.addWidget(restore_btn)

                lo.addWidget(card)

        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(dlg.accept)
        lo.addWidget(close_btn)
        dlg.exec()

    def _on_restore_latest(self):
        """恢复最新快照"""
        self._do_restore(None)

    def _do_restore(self, ts: str | None):
        from backend.core.identity.snapshot import restore_tool_memories
        from PySide6.QtWidgets import QMessageBox

        reply = QMessageBox.question(
            self,
            _tr("gov.restore_confirm_title", "确认恢复"),
            _tr("gov.restore_confirm",
                "将用快照覆盖当前 workspace 中的 .claude/ .codex/ .codebuddy/ 文件？"),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        ws = Path(self.state.project.workspace_path)
        bk = Path(self.state.project.backup_path)
        result = restore_tool_memories(bk, ws, ts)
        restored = result.get("restored", [])
        if "error" in result:
            self._log(_tr("gov.restore_fail", "恢复失败: {e}").format(
                e=result["error"]))
        else:
            self._log(_tr("gov.restore_ok", "已恢复: {files}").format(
                files=", ".join(restored)))

        return True  # 供弹窗关闭用
```

---

## Task J：Lesson 管理弹窗增强

### 目标

把 Governance Tab 的 Lesson 卡片「View All」按钮关联的弹窗从只读列表升级为带搜索/验证/提升功能的完整对话框。

### 新文件：`frontend/lesson_dialog.py`

```python
"""LessonDialog — Lesson 列表 + 搜索 + 验证 + 提升"""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
    QMessageBox, QInputDialog, QScrollArea,
)
from backend.core.i18n import _tr
from backend.core.knowledge.lesson import LessonManager
from backend.core.knowledge.models import Lesson
from themes import get_theme


class LessonDialog(QDialog):
    """Lesson 管理弹窗 — 三个子页 (Instance / Abstract / Pending) + 搜索"""

    def __init__(self, workspace_path: str, project_name: str, parent=None):
        super().__init__(parent)
        self.ws_path = Path(workspace_path)
        self.project_name = project_name
        self.setWindowTitle(_tr("lesson.dialog_title", "Lesson 管理"))
        self.setMinimumSize(650, 500)
        self._build_ui()
        self._load_data()

    def _build_ui(self):
        t = get_theme()
        lo = QVBoxLayout(self)

        # 搜索栏
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            _tr("lesson.search_placeholder", "搜索 lesson..."))
        self.search_input.textChanged.connect(self._on_search)
        search_row.addWidget(self.search_input, 1)
        search_btn = QPushButton(_tr("lesson.search", "搜索"))
        search_btn.setProperty("variant", "secondary")
        search_btn.clicked.connect(self._on_search)
        search_row.addWidget(search_btn)
        lo.addLayout(search_row)

        # Tab 子页
        self.tabs = QTabWidget()
        self.instance_page = self._make_list_page("instance")
        self.abstract_page = self._make_list_page("abstract")
        self.pending_page = self._make_list_page("pending")
        self.tabs.addTab(self.instance_page, _tr("lesson.tab_instance", "Instance"))
        self.tabs.addTab(self.abstract_page, _tr("lesson.tab_abstract", "Abstract"))
        self.tabs.addTab(self.pending_page, _tr("lesson.tab_pending", "Pending"))
        lo.addWidget(self.tabs, 1)

        # 关闭
        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(self.accept)
        lo.addWidget(close_btn)

    def _make_list_page(self, kind: str) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 8, 0, 0)
        lo.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        setattr(self, f"_{kind}_container", container)
        setattr(self, f"_{kind}_layout", QVBoxLayout(container))
        getattr(self, f"_{kind}_layout").setSpacing(4)
        getattr(self, f"_{kind}_layout").addStretch()
        scroll.setWidget(container)
        lo.addWidget(scroll)
        return w

    def _load_data(self, search_query: str = ""):
        if search_query:
            lessons = LessonManager.search(
                self.ws_path, search_query, self.project_name)
            # 搜索结果全部显示在 instance 页
            self._populate_page("instance", lessons)
            self._populate_page("abstract", [])
            self._populate_page("pending", [])
        else:
            self._populate_page("instance",
                LessonManager.load_instance(self.ws_path, self.project_name))
            self._populate_page("abstract",
                LessonManager.load_abstract(self.ws_path))
            self._populate_page("pending",
                LessonManager.load_pending(self.ws_path, self.project_name))

    def _populate_page(self, kind: str, lessons: list[Lesson]):
        lay = getattr(self, f"_{kind}_layout")
        # 清空（保留 stretch）
        while lay.count() > 1:
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        t = get_theme()
        for les in lessons:
            card = QFrame()
            card.setStyleSheet(
                f"background:{t.bg};border:.5px solid {t.bdr};"
                f"border-radius:6px;padding:10px;margin:2px 0;")
            cl = QVBoxLayout(card)
            cl.setSpacing(4)

            # 类别 + 严重度
            hdr = QHBoxLayout()
            cat = QLabel(les.category or "general")
            cat.setStyleSheet(
                f"font-size:9px;font-weight:600;padding:1px 6px;"
                f"border-radius:3px;background:{t.blue}20;color:{t.blue};")
            hdr.addWidget(cat)
            sev_colors = {"critical": t.danger_txt, "high": t.amber,
                          "medium": t.txt3, "low": t.txt2}
            sev = QLabel(les.severity.upper())
            sev.setStyleSheet(
                f"font-size:9px;font-weight:600;padding:1px 6px;"
                f"border-radius:3px;"
                f"color:{sev_colors.get(les.severity, t.txt3)};")
            hdr.addWidget(sev)
            hdr.addStretch()
            cl.addLayout(hdr)

            # Rule
            rule = QLabel(
                f'<span style="font-size:11px;color:{t.txt};">'
                f'{les.rule[:120]}</span>')
            rule.setWordWrap(True)
            cl.addWidget(rule)

            # 操作按钮
            btn_row = QHBoxLayout()
            verify_btn = QPushButton(
                _tr("lesson.verify", "Verify"))
            verify_btn.setProperty("variant", "ghost")
            verify_btn.clicked.connect(
                lambda checked, lid=les.id: self._verify(lid))
            btn_row.addWidget(verify_btn)

            if not les.abstract:
                promote_btn = QPushButton(
                    _tr("lesson.promote", "Promote"))
                promote_btn.setProperty("variant", "ghost")
                promote_btn.clicked.connect(
                    lambda checked, lid=les.id: self._promote(lid))
                btn_row.addWidget(promote_btn)

            btn_row.addStretch()
            cl.addLayout(btn_row)

            lay.insertWidget(lay.count() - 1, card)

    def _on_search(self):
        self._load_data(self.search_input.text().strip())

    def _verify(self, lesson_id: str):
        result = LessonManager.verify(
            self.ws_path, lesson_id, self.project_name)
        if result:
            self._log_parent(
                _tr("lesson.verified", "已验证: {id}").format(id=lesson_id[:12]))
            self._load_data(self.search_input.text().strip())
        else:
            QMessageBox.warning(
                self, _tr("dialog.hint", "提示"),
                _tr("lesson.not_found", "未找到该 lesson"))

    def _promote(self, lesson_id: str):
        tech, ok = QInputDialog.getText(
            self,
            _tr("lesson.promote_title", "提升为抽象层"),
            _tr("lesson.promote_hint", "输入 tech_stack（如 PySide6, React）："),
        )
        if not ok or not tech.strip():
            return
        result = LessonManager.promote_to_abstract(
            self.ws_path, lesson_id, self.project_name, tech.strip())
        if result:
            self._log_parent(
                _tr("lesson.promoted", "已提升: {id}").format(id=lesson_id[:12]))
            self._load_data(self.search_input.text().strip())
        else:
            QMessageBox.warning(
                self, _tr("dialog.hint", "提示"),
                _tr("lesson.not_found", "未找到该 lesson"))

    def _log_parent(self, msg: str):
        # 通知父窗口打 log
        parent = self.parent()
        if parent and hasattr(parent, '_log'):
            parent._log(msg)
```

### 改动：`governance.py` — 替换 _on_view_lessons

将现有的 `_on_view_lessons` 方法替换为：

```python
    def _on_view_lessons(self):
        from frontend.lesson_dialog import LessonDialog
        dlg = LessonDialog(
            self.state.project.workspace_path,
            self.state.project.name,
            self,
        )
        dlg.exec()
```

同时删除 `governance.py` 中 `_on_view_lessons` 原来的整个方法体（约 20 行只读列表实现）。

---

## Task K：Contract 更新功能

### 目标

Governance Tab 的 Contract 卡片加 "Update Contract" 按钮，弹出对话框添加/编辑 decided features。

### 改动：`governance.py`

#### 在 `_build_contract_card` 按钮行加一个 Update 按钮

```python
        btn_row = QHBoxLayout()
        view_btn = QPushButton(_tr("gov.view_contract", "View Contract"))
        view_btn.setProperty("variant", "secondary")
        view_btn.clicked.connect(self._on_view_contract)
        btn_row.addWidget(view_btn)

        update_btn = QPushButton(_tr("gov.update_contract", "Update"))
        update_btn.setProperty("variant", "secondary")
        update_btn.clicked.connect(self._on_update_contract)
        btn_row.addWidget(update_btn)

        btn_row.addStretch()
        card.layout().addLayout(btn_row)
```

#### 新增回调方法

```python
    def _on_update_contract(self):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, \
            QLabel, QLineEdit, QPushButton, QMessageBox
        from backend.core.contract import ContractManager

        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("gov.update_contract_dialog", "更新合约"))
        dlg.setMinimumSize(450, 300)
        lo = QVBoxLayout(dlg)

        lo.addWidget(QLabel(
            _tr("gov.update_hint",
                "添加一条 decided feature（已存在则增加确认计数）：")))

        # Feature name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel(_tr("gov.feature_name", "名称:")))
        name_input = QLineEdit()
        name_row.addWidget(name_input, 1)
        lo.addLayout(name_row)

        # Location
        loc_row = QHBoxLayout()
        loc_row.addWidget(QLabel(_tr("gov.feature_location", "文件:")))
        loc_input = QLineEdit()
        loc_row.addWidget(loc_input, 1)
        lo.addLayout(loc_row)

        # Signature
        sig_row = QHBoxLayout()
        sig_row.addWidget(QLabel(_tr("gov.feature_sig", "签名:")))
        sig_input = QLineEdit()
        sig_row.addWidget(sig_input, 1)
        lo.addLayout(sig_row)

        # 按钮
        btn_row = QHBoxLayout()
        ok_btn = QPushButton(_tr("settings.ok", "确认"))
        cancel_btn = QPushButton(_tr("settings.cancel", "取消"))
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        lo.addLayout(btn_row)

        def on_ok():
            name = name_input.text().strip()
            if not name:
                QMessageBox.warning(dlg, _tr("dialog.hint", "提示"),
                                    _tr("gov.name_required", "名称不能为空"))
                return
            ws = Path(self.state.project.workspace_path)
            ContractManager.update_feature(
                ws,
                self.state.project.name,
                name,
                loc_input.text().strip(),
                sig_input.text().strip(),
            )
            dlg.accept()
            self._load_governance_data()  # 刷新卡片

        ok_btn.clicked.connect(on_ok)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()
```

---

## Task L：Template CRUD 对话框

### 目标

Settings 或其子页新增模板管理能力（add / edit / delete）。

### 方案

最简单方式：在现有 `SettingsDialog` 中加一个 Tab，或者做成独立弹窗从 Workshop Tab 的 Template 下拉旁边触发。

### 改动：`workshop_tab.py` — Template 下拉旁加 ⚙ 按钮

在 template_combo 之后加一个 `⚙` 按钮：

```python
        lo.addWidget(self.state.template_combo)

        # ── Template 管理按钮 ──
        tmpl_mgr_btn = QPushButton("⚙")
        tmpl_mgr_btn.setFixedWidth(26)
        tmpl_mgr_btn.setProperty("variant", "ghost")
        tmpl_mgr_btn.setToolTip(_tr("action.template_mgr", "管理模板"))
        tmpl_mgr_btn.clicked.connect(self._on_manage_templates)
        lo.addWidget(tmpl_mgr_btn)
```

### 新增方法：`workshop_tab.py` → WorkspacePanel

添加到 `class WorkshopTabMixin`（或放在 `panel.py` 中作为 `WorkspacePanel` 方法，因为 builder/workshop_tab/panel 共享方法空间）：

```python
    def _on_manage_templates(self):
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
            QPushButton, QComboBox, QPlainTextEdit, QMessageBox,
            QListWidget, QStackedWidget,
        )
        from backend.core.template_manager import TemplateManager, CommitTemplate

        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("template.dialog_title", "模板管理"))
        dlg.setMinimumSize(600, 450)
        t = get_theme()
        lo = QVBoxLayout(dlg)
        lo.setSpacing(8)

        # ── 左侧列表 + 右侧编辑 ──
        body = QHBoxLayout()

        # 左侧：模板列表
        left = QVBoxLayout()
        tmpl_list = QListWidget()
        left.addWidget(QLabel(_tr("template.list_header", "模板列表:")))
        left.addWidget(tmpl_list, 1)

        # + / - 按钮
        add_btn = QPushButton(_tr("template.add", "＋ 新建"))
        add_btn.setProperty("variant", "secondary")
        left.addWidget(add_btn)

        del_btn = QPushButton(_tr("template.delete", "✕ 删除"))
        del_btn.setProperty("variant", "danger")
        del_btn.setEnabled(False)
        left.addWidget(del_btn)
        body.addLayout(left, 1)

        # 右侧：编辑区
        right = QVBoxLayout()
        right.addWidget(QLabel(_tr("template.edit_header", "编辑:")))

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel(_tr("template.name", "名称:")))
        name_edit = QLineEdit()
        name_row.addWidget(name_edit, 1)
        right.addLayout(name_row)

        desc_row = QHBoxLayout()
        desc_row.addWidget(QLabel(_tr("template.desc", "描述:")))
        desc_edit = QLineEdit()
        desc_row.addWidget(desc_edit, 1)
        right.addLayout(desc_row)

        right.addWidget(QLabel(_tr("template.header_fmt", "Header 格式:")))
        header_edit = QPlainTextEdit()
        header_edit.setMaximumHeight(60)
        right.addWidget(header_edit)

        right.addWidget(QLabel(_tr("template.body_fmt", "Body 格式:")))
        body_edit = QPlainTextEdit()
        body_edit.setMaximumHeight(120)
        right.addWidget(body_edit)

        save_btn = QPushButton(_tr("template.save", "保存"))
        save_btn.setProperty("variant", "primary")
        right.addWidget(save_btn)
        body.addLayout(right, 2)

        lo.addLayout(body)
        close_btn = QPushButton(_tr("settings.ok", "关闭"))
        close_btn.clicked.connect(dlg.accept)
        lo.addWidget(close_btn)

        # ── 逻辑 ──

        def refresh_list():
            tmpl_list.clear()
            for tpl in TemplateManager.load():
                tmpl_list.addItem(tpl.name)

        def load_template(name: str):
            tpl = TemplateManager.get_template(name)
            if tpl:
                name_edit.setText(tpl.name)
                desc_edit.setText(tpl.description)
                header_edit.setPlainText(tpl.header_format)
                body_edit.setPlainText(tpl.body_format)

        def on_select():
            item = tmpl_list.currentItem()
            if item:
                del_btn.setEnabled(item.text() != "default")
                load_template(item.text())

        def on_add():
            name = _tr("template.new_name", "new_template")
            # 找不重名
            existing = {t.name for t in TemplateManager.load()}
            i = 1
            while name in existing:
                name = f"new_template_{i}"
                i += 1
            TemplateManager.save(
                TemplateManager.load() + [CommitTemplate(name=name)])
            refresh_list()
            # 选中新建的
            for i in range(tmpl_list.count()):
                if tmpl_list.item(i).text() == name:
                    tmpl_list.setCurrentRow(i)
                    break
            load_template(name)

        def on_delete():
            item = tmpl_list.currentItem()
            if not item or item.text() == "default":
                return
            reply = QMessageBox.question(
                dlg,
                _tr("dialog.confirm_delete", "确认删除"),
                _tr("template.confirm_delete",
                    "删除模板「{name}」？").format(name=item.text()),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            templates = TemplateManager.load()
            templates = [t for t in templates if t.name != item.text()]
            TemplateManager.save(templates)
            refresh_list()

        def on_save():
            item = tmpl_list.currentItem()
            if not item:
                return
            old_name = item.text()
            new_name = name_edit.text().strip()
            if not new_name:
                return
            templates = TemplateManager.load()
            for tpl in templates:
                if tpl.name == old_name:
                    tpl.name = new_name
                    tpl.description = desc_edit.text().strip()
                    tpl.header_format = header_edit.toPlainText()
                    tpl.body_format = body_edit.toPlainText()
                    break
            TemplateManager.save(templates)
            refresh_list()
            self._log(_tr("template.saved", "模板已保存: {name}").format(
                name=new_name))

        tmpl_list.currentItemChanged.connect(lambda: on_select())
        add_btn.clicked.connect(on_add)
        del_btn.clicked.connect(on_delete)
        save_btn.clicked.connect(on_save)

        refresh_list()
        dlg.exec()
```

---

## i18n 汇总

```
zh.json 新增:
  "gov.snap_now": "立即快照",
  "gov.snap_list": "快照列表",
  "gov.snap_restore": "恢复最新",
  "gov.snap_ok": "已快照: {files}",
  "gov.snap_empty": "无记忆文件可快照",
  "gov.snap_dialog": "Memory Snapshots",
  "gov.snap_none": "没有快照记录",
  "gov.restore_this": "恢复此版本",
  "gov.restore_confirm_title": "确认恢复",
  "gov.restore_confirm": "将用快照覆盖当前 workspace 中的 .claude/ .codex/ .codebuddy/ 文件？",
  "gov.restore_ok": "已恢复: {files}",
  "gov.restore_fail": "恢复失败: {e}",
  "gov.update_contract": "更新合约",
  "gov.update_contract_dialog": "更新合约",
  "gov.update_hint": "添加一条 decided feature（已存在则增加确认计数）：",
  "gov.feature_name": "名称:",
  "gov.feature_location": "文件:",
  "gov.feature_sig": "签名:",
  "gov.name_required": "名称不能为空",
  "lesson.dialog_title": "Lesson 管理",
  "lesson.search_placeholder": "搜索 lesson...",
  "lesson.search": "搜索",
  "lesson.tab_instance": "Instance",
  "lesson.tab_abstract": "Abstract",
  "lesson.tab_pending": "Pending",
  "lesson.verify": "Verify",
  "lesson.promote": "Promote",
  "lesson.verified": "已验证: {id}",
  "lesson.promoted": "已提升: {id}",
  "lesson.promote_title": "提升为抽象层",
  "lesson.promote_hint": "输入 tech_stack（如 PySide6, React）：",
  "lesson.not_found": "未找到该 lesson",
  "action.template_mgr": "管理模板",
  "template.dialog_title": "模板管理",
  "template.list_header": "模板列表:",
  "template.add": "＋ 新建",
  "template.delete": "✕ 删除",
  "template.edit_header": "编辑:",
  "template.name": "名称:",
  "template.desc": "描述:",
  "template.header_fmt": "Header 格式:",
  "template.body_fmt": "Body 格式:",
  "template.save": "保存",
  "template.new_name": "new_template",
  "template.confirm_delete": "删除模板「{name}」？",
  "template.saved": "模板已保存: {name}",

en.json: (对应英文)
```

---

## 风险标注

1. **`frontend/lesson_dialog.py` 新文件** → `from frontend.lesson_dialog import LessonDialog` 在 `governance.py` 中引用。PyInstaller 需要添加 hidden import：`frontend.lesson_dialog`。

2. **CommitTemplate 导入** → `workshop_tab.py` 需 `from backend.core.template_manager import CommitTemplate`（已在 `TemplateManager` 的 `from` 行补充即可）。

3. **LessonDialog 搜索** → `LessonManager.search()` 参数是 `(workspace_path, query, project_name, tech_stack)`——注意参数顺序。

4. **Template 保存后 ComboBox 不刷新** → `_on_manage_templates` 关闭对话框后应调用 `self.state.template_combo.clear(); self.state.template_combo.addItems(...)` 刷新下拉。

---

## 文件清单

| Task | 文件 | 类型 |
|------|------|------|
| H | `workshop_tab.py` | Bug fix (2行) |
| I | `governance.py` | 3个新方法 + Identity 卡片加按钮 |
| J | `frontend/lesson_dialog.py` (新) | 新文件 ~180行 |
| J | `governance.py` | 替换 _on_view_lessons |
| K | `governance.py` | 1个按钮 + 1个回调方法 |
| L | `workshop_tab.py` | 1个⚙按钮 + 1个方法 ~120行 |
| — | `locales/zh.json` `locales/en.json` | ~30个新key |
