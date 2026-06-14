# Claude Code 执行规格 — Round 3（精确版）

> **执行顺序**：Task H → I → J → K → L（按数字顺序，不可跳步）
>
> **执行方式**：每个 Task 内先 Read 相关文件确认当前状态，再 Edit。

---

## Task H：修复 Template 下拉 crash

**问题**：`workshop_tab.py` L149 调用不存在的 `TemplateManager.list_templates()`

### Step H1：读文件确认当前状态

```
Read: frontend/workspace/workshop_tab.py#L143-159
```

### Step H2：改 2 行

文件：`frontend/workspace/workshop_tab.py`

找到：
```python
        templates = TemplateManager.list_templates()
        self.state.template_combo.addItems(templates)
        current = self.state.project.commit_format.get("template_name", "default")
        if current in templates:
```

改为：
```python
        templates = TemplateManager.load()
        template_names = [t.name for t in templates]
        self.state.template_combo.addItems(template_names)
        current = self.state.project.commit_format.get("template_name", "default")
        if current in template_names:
```

### 验证 H

运行 `python -m gitgo`，打开项目 → Workshop Tab → Template 下拉应显示选项不报错。

---

## Task I：Memory Snapshot 管理（governance.py）

**后端 API**（无需 import 验证，Round 2 已验证）：
```python
from backend.core.identity.snapshot import (
    snapshot_tool_memories,   # (ws_path, bk_path, project) -> dict
    restore_tool_memories,    # (bk_path, ws_path, ts) -> dict
    list_memory_snapshots,    # (bk_path) -> list[dict]
)
```

### Step I1：Read 确认当前状态

```
Read: frontend/workspace/governance.py#L255-261  （Identity 卡片 return 前）
Read: frontend/workspace/governance.py#L324-373  （交互回调区域）
```

### Step I2：Identity 卡片加 Memory 按钮行

文件：`frontend/workspace/governance.py`

在 L259（`if not any(warnings):` 块的 `card.layout().addWidget(ok)` 之后、L261（`return card`）之前：

找到：
```python
            if not any(warnings):
                ok = QLabel(
                    f'<span style="color:{t.success_txt};font-size:11px;">'
                    f'✓ {_tr("gov.integrity_ok", "All checks passed")}</span>')
                card.layout().addWidget(ok)

        return card
```

改为：
```python
            if not any(warnings):
                ok = QLabel(
                    f'<span style="color:{t.success_txt};font-size:11px;">'
                    f'✓ {_tr("gov.integrity_ok", "All checks passed")}</span>')
                card.layout().addWidget(ok)

        # ── Memory 按钮行 ──
        mem_row = QHBoxLayout()
        snap_btn = QPushButton(_tr("gov.snap_now", "Snapshot Now"))
        snap_btn.setProperty("variant", "ghost")
        snap_btn.clicked.connect(self._on_snapshot_now)
        mem_row.addWidget(snap_btn)

        list_btn = QPushButton(_tr("gov.snap_list", "Snapshots"))
        list_btn.setProperty("variant", "ghost")
        list_btn.clicked.connect(self._on_list_snapshots)
        mem_row.addWidget(list_btn)

        restore_btn = QPushButton(_tr("gov.snap_restore", "Restore Latest"))
        restore_btn.setProperty("variant", "ghost")
        restore_btn.clicked.connect(self._on_restore_latest)
        mem_row.addWidget(restore_btn)
        mem_row.addStretch()
        card.layout().addLayout(mem_row)

        return card
```

### Step I3：添加 4 个 Memory 回调方法

文件：`frontend/workspace/governance.py`

在 `_on_view_contract` 方法结束后（L335，`_show_text_dialog(...)` 调用后的空行处），`_on_view_lessons` 方法之前（L337），插入以下 4 个方法：

```python
    def _on_snapshot_now(self):
        """手动触发一次记忆快照"""
        from backend.core.identity.snapshot import snapshot_tool_memories
        ws = Path(self.state.project.workspace_path)
        bk = Path(self.state.project.backup_path)
        result = snapshot_tool_memories(str(ws), str(bk), self.state.project)
        snapped = result.get("snapped", [])
        if snapped:
            self._log(_tr("gov.snap_ok", "已快照: {files}").format(
                files=", ".join(snapped)))
        else:
            self._log(_tr("gov.snap_empty", "无记忆文件可快照"))

    def _on_list_snapshots(self):
        """弹出快照列表对话框"""
        from backend.core.identity.snapshot import list_memory_snapshots
        from PySide6.QtWidgets import QPushButton as QPB

        bk = Path(self.state.project.backup_path)
        snaps = list_memory_snapshots(str(bk))

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
                hdr.addWidget(QLabel(
                    f'<span style="color:{t.txt3};font-size:10px;">'
                    f'{s["timestamp"]}</span>'))
                cl.addLayout(hdr)

                row2 = QHBoxLayout()
                row2.addWidget(QLabel(
                    f'<span style="font-size:10px;color:{t.txt3};">'
                    f'{s["path"]}</span>'))
                row2.addStretch()
                restore_btn = QPB(_tr("gov.restore_this", "恢复此版本"))
                restore_btn.setProperty("variant", "ghost")
                ts_val = s["timestamp"]
                restore_btn.clicked.connect(
                    lambda checked, ts=ts_val, d=dlg:
                    self._do_restore(ts) and d.accept())
                row2.addWidget(restore_btn)
                cl.addLayout(row2)

                lo.addWidget(card)

        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(dlg.accept)
        lo.addWidget(close_btn)
        dlg.exec()

    def _on_restore_latest(self):
        """恢复最新快照"""
        self._do_restore(None)

    def _do_restore(self, ts=None):
        """执行恢复（ts=None 表示最新）"""
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
            return False

        ws = Path(self.state.project.workspace_path)
        bk = Path(self.state.project.backup_path)
        result = restore_tool_memories(str(bk), str(ws), ts)
        restored = result.get("restored", [])
        if "error" in result:
            self._log(_tr("gov.restore_fail", "恢复失败: {e}").format(
                e=result["error"]))
        else:
            self._log(_tr("gov.restore_ok", "已恢复: {files}").format(
                files=", ".join(restored)))
        return True
```

### 验证 I

打开项目 → Governance Tab → Identity Guard 卡片底部应出现 3 个按钮：Snapshot Now / Snapshots / Restore Latest。点击 Snapshots 如有快照数据应弹出列表。

---

## Task J：Lesson 管理弹窗（新文件 + 替换旧方法）

### Step J1：创建新文件 `frontend/lesson_dialog.py`

创建文件：`frontend/lesson_dialog.py`

完整内容：

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
from themes import get_theme
from backend.core.knowledge.models import Lesson


class LessonDialog(QDialog):
    """Lesson 管理弹窗"""

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
            _tr("lesson.search_placeholder", "搜索..."))
        self.search_input.textChanged.connect(self._on_search)
        search_row.addWidget(self.search_input, 1)
        search_btn = QPushButton(_tr("lesson.search", "搜索"))
        search_btn.setProperty("variant", "secondary")
        search_btn.clicked.connect(self._on_search)
        search_row.addWidget(search_btn)
        lo.addLayout(search_row)

        # Tab 子页
        self.tabs = QTabWidget()
        self.instance_page = self._make_list_page()
        self.abstract_page = self._make_list_page()
        self.pending_page = self._make_list_page()
        self.tabs.addTab(self.instance_page,
                         _tr("lesson.tab_instance", "Instance"))
        self.tabs.addTab(self.abstract_page,
                         _tr("lesson.tab_abstract", "Abstract"))
        self.tabs.addTab(self.pending_page,
                         _tr("lesson.tab_pending", "Pending"))
        lo.addWidget(self.tabs, 1)

        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(self.accept)
        lo.addWidget(close_btn)

    def _make_list_page(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 8, 0, 0)
        lo.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        setattr(self, '_result_container', container)
        setattr(self, '_result_layout', QVBoxLayout(container))
        getattr(self, '_result_layout').setSpacing(4)
        getattr(self, '_result_layout').addStretch()
        scroll.setWidget(container)
        lo.addWidget(scroll)
        return w

    def _load_data(self, search_query: str = ""):
        from backend.core.knowledge.manager import LessonManager

        if search_query:
            lessons = LessonManager.search(
                self.ws_path, search_query, self.project_name)
            # 搜索结果全部显示在 instance 页
            self._populate_page(0, lessons)
            self._populate_page(1, [])
            self._populate_page(2, [])
        else:
            self._populate_page(0,
                LessonManager.load_instance(self.ws_path, self.project_name))
            self._populate_page(1,
                LessonManager.load_abstract(self.ws_path))
            self._populate_page(2,
                LessonManager.load_pending(self.ws_path, self.project_name))

    def _populate_page(self, tab_idx: int, lessons: list[Lesson]):
        page = self.tabs.widget(tab_idx)
        scroll = page.findChild(QScrollArea)
        container = scroll.widget()
        layout = container.layout()

        # 清空旧卡片（保留 stretch）
        while layout.count() > 1:
            item = layout.takeAt(0)
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

            sev_map = {"critical": t.danger_txt, "high": t.amber,
                       "medium": t.txt3, "low": t.txt2}
            sev = QLabel(les.severity.upper())
            sev.setStyleSheet(
                f"font-size:9px;font-weight:600;padding:1px 6px;"
                f"border-radius:3px;"
                f"color:{sev_map.get(les.severity, t.txt3)};")
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
            verify_btn = QPushButton(_tr("lesson.verify", "Verify"))
            verify_btn.setProperty("variant", "ghost")
            lid = les.id
            verify_btn.clicked.connect(
                lambda checked, lid=lid: self._verify(lid))
            btn_row.addWidget(verify_btn)

            if not les.abstract:
                promote_btn = QPushButton(_tr("lesson.promote", "Promote"))
                promote_btn.setProperty("variant", "ghost")
                promote_btn.clicked.connect(
                    lambda checked, lid=lid: self._promote(lid))
                btn_row.addWidget(promote_btn)

            btn_row.addStretch()
            cl.addLayout(btn_row)

            layout.insertWidget(layout.count() - 1, card)

    def _on_search(self):
        self._load_data(self.search_input.text().strip())

    def _verify(self, lesson_id: str):
        from backend.core.knowledge.manager import LessonManager
        result = LessonManager.verify(self.ws_path, lesson_id, self.project_name)
        if result:
            self._log_parent(
                _tr("lesson.verified", "已验证: {id}").format(id=lesson_id[:12]))
            self._load_data(self.search_input.text().strip())
        else:
            QMessageBox.warning(
                self, _tr("dialog.hint", "提示"),
                _tr("lesson.not_found", "未找到该 lesson"))

    def _promote(self, lesson_id: str):
        from backend.core.knowledge.manager import LessonManager
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
        parent = self.parent()
        if parent and hasattr(parent, '_log'):
            parent._log(msg)
```

### Step J2：替换 governance.py 中的 `_on_view_lessons`

文件：`frontend/workspace/governance.py`

找到 L337-359（整个 `_on_view_lessons` 方法），用以下代码**完整替换**：

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

### Step J3：PyInstaller hidden import

文件：`build.py`（或 `gitgo_core.spec`），在 `_HIDDEN_IMPORTS` 列表中添加：
```python
"frontend.lesson_dialog",
```

### 验证 J

打开 Governance Tab → Lesson 卡片 → View All → 应弹出三页 Tab 对话框。如有 lesson 数据，每条应有 Verify / Promote 按钮。

---

## Task K：Contract 更新功能

### Step K1：Read 确认当前状态

```
Read: frontend/workspace/governance.py#L177-186  （Contract 按钮行）
Read: frontend/workspace/governance.py#L43-61    （_load_governance_data Contract 部分）
```

### Step K2：Contract 卡片加「Update」按钮

文件：`frontend/workspace/governance.py` L177-184

找到：
```python
        # 按钮
        btn_row = QHBoxLayout()
        view_btn = QPushButton(_tr("gov.view_contract", "View Contract"))
        view_btn.setProperty("variant", "secondary")
        view_btn.clicked.connect(self._on_view_contract)
        btn_row.addWidget(view_btn)
        btn_row.addStretch()
        card.layout().addLayout(btn_row)
```

改为：
```python
        # 按钮
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

### Step K3：添加 _on_update_contract 回调

文件：`frontend/workspace/governance.py`

在 L335结束、L337 `_on_view_lessons` 之前（即 Task I 插入的 Memory 方法之后）：

插入：

```python
    def _on_update_contract(self):
        """弹出对话框，添加/更新 decided feature"""
        from PySide6.QtWidgets import QLineEdit, QMessageBox
        from backend.core.contract import ContractManager

        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("gov.update_contract_dialog", "更新合约"))
        dlg.setMinimumSize(450, 300)
        lo = QVBoxLayout(dlg)

        lo.addWidget(QLabel(
            _tr("gov.update_hint",
                "添加一条 decided feature（已存在则增加确认计数）：")))

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel(_tr("gov.feature_name", "名称:")))
        name_input = QLineEdit()
        name_row.addWidget(name_input, 1)
        lo.addLayout(name_row)

        loc_row = QHBoxLayout()
        loc_row.addWidget(QLabel(_tr("gov.feature_location", "文件:")))
        loc_input = QLineEdit()
        loc_row.addWidget(loc_input, 1)
        lo.addLayout(loc_row)

        sig_row = QHBoxLayout()
        sig_row.addWidget(QLabel(_tr("gov.feature_sig", "签名:")))
        sig_input = QLineEdit()
        sig_row.addWidget(sig_input, 1)
        lo.addLayout(sig_row)

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
            self._load_governance_data()

        ok_btn.clicked.connect(on_ok)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()
```

### 验证 K

Governance Tab → Contract 卡片 → 点击 Update → 弹窗填 name/file/sig → 确认 → 卡片数据应刷新。

---

## Task L：Template CRUD 对话框

### Step L1：Read 确认当前状态

```
Read: frontend/workspace/workshop_tab.py#L143-159  （Template selector 区域）
Read: frontend/workspace/workshop_tab.py#L1-6      （imports）
```

### Step L2：Template 下拉旁加 ⚙ 按钮

文件：`frontend/workspace/workshop_tab.py`

在 L159（`lo.addWidget(self.state.template_combo)` 之后、L160 空行处）：

找到：
```python
        lo.addWidget(self.state.template_combo)

        self.state.delete_formal_btn = QPushButton("✕")
```

改为：
```python
        lo.addWidget(self.state.template_combo)

        # ── Template 管理 ⚙ ──
        tmpl_mgr_btn = QPushButton("⚙")
        tmpl_mgr_btn.setFixedWidth(26)
        tmpl_mgr_btn.setProperty("variant", "ghost")
        tmpl_mgr_btn.setToolTip(_tr("action.template_mgr", "管理模板"))
        tmpl_mgr_btn.clicked.connect(self._on_manage_templates)
        lo.addWidget(tmpl_mgr_btn)

        self.state.delete_formal_btn = QPushButton("✕")
```

### Step L3：添加 _on_manage_templates 方法

文件：`frontend/workshop/workshop_tab.py`（或 `frontend/workspace/commits.py`）

因为 `WorkshopTabMixin` 通过 `WorkspacePanel` 多重继承可以访问 `CommitMixin` 的方法，且 Template 管理和 commit 密切相关。放在 `workshop_tab.py` 末尾（L184 `ctr_layout.addWidget(row)` 之后，文件最后）即可。

或者新建一个独立的 Mixin 方法：直接加在 `workshop_tab.py` 里 `class WorkshopTabMixin` 的最后（但在它继承的 Mixin 体系中也能被访问）。

在 `workshop_tab.py` 末尾（L184 之后）添加：

```python
    def _on_manage_templates(self):
        """模板管理弹窗 — 列表 + 编辑 + 新建 + 删除"""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
            QPushButton, QPlainTextEdit, QMessageBox,
            QListWidget, QInputDialog,
        )
        from backend.core.template_manager import TemplateManager, CommitTemplate

        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("template.dialog_title", "模板管理"))
        dlg.setMinimumSize(600, 450)
        t = get_theme()
        lo = QVBoxLayout(dlg)
        lo.setSpacing(8)

        body = QHBoxLayout()

        # 左侧：列表
        left = QVBoxLayout()
        left.addWidget(QLabel(_tr("template.list_header", "模板列表:")))
        tmpl_list = QListWidget()
        left.addWidget(tmpl_list, 1)

        add_btn = QPushButton(_tr("template.add", "＋ 新建"))
        add_btn.setProperty("variant", "secondary")
        left.addWidget(add_btn)

        del_btn = QPushButton(_tr("template.delete", "✕ 删除"))
        del_btn.setProperty("variant", "danger")
        del_btn.setEnabled(False)
        left.addWidget(del_btn)
        body.addLayout(left, 1)

        # 右侧：编辑
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

        def load_current():
            item = tmpl_list.currentItem()
            if item:
                del_btn.setEnabled(item.text() != "default")
                tpl = TemplateManager.get_template(item.text())
                if tpl:
                    name_edit.setText(tpl.name)
                    desc_edit.setText(tpl.description)
                    header_edit.setPlainText(tpl.header_format)
                    body_edit.setPlainText(tpl.body_format)

        def on_add():
            existing = {t.name for t in TemplateManager.load()}
            name = "new_template"
            i = 1
            while name in existing:
                name = f"new_template_{i}"
                i += 1
            TemplateManager.save(
                TemplateManager.load() + [CommitTemplate(name=name)])
            refresh_list()
            for i2 in range(tmpl_list.count()):
                if tmpl_list.item(i2).text() == name:
                    tmpl_list.setCurrentRow(i2)
                    break
            load_current()

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
            templates = [t for t in TemplateManager.load()
                         if t.name != item.text()]
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
            # 刷新 Workshop 的 template_combo
            if hasattr(self.state, 'template_combo'):
                cb = self.state.template_combo
                cb.clear()
                cb.addItems([t.name for t in TemplateManager.load()])
                cb.setCurrentText(new_name)
            self._log(_tr("template.saved", "模板已保存: {name}").format(
                name=new_name))

        tmpl_list.currentItemChanged.connect(lambda: load_current())
        add_btn.clicked.connect(on_add)
        del_btn.clicked.connect(on_delete)
        save_btn.clicked.connect(on_save)

        refresh_list()
        dlg.exec()
```

### 验证 L

Workshop Tab → Template 下拉旁的 ⚙ → 弹出管理对话框，可新建/编辑/删除模板。

---

## i18n

两个文件各追加 30 个 key。**追加在文件末尾倒数第三行 `}` 之前**（每个 key 上一行加逗号）。

### `locales/zh.json`

在最后的 `}` 前插入：

```json
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
  "lesson.search_placeholder": "搜索...",
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
  "template.confirm_delete": "删除模板「{name}」？",
  "template.saved": "模板已保存: {name}",
```

### `locales/en.json`

同上位置：

```json
  "gov.snap_now": "Snapshot Now",
  "gov.snap_list": "Snapshots",
  "gov.snap_restore": "Restore Latest",
  "gov.snap_ok": "Snapshotted: {files}",
  "gov.snap_empty": "No memory files to snapshot",
  "gov.snap_dialog": "Memory Snapshots",
  "gov.snap_none": "No snapshots found",
  "gov.restore_this": "Restore This",
  "gov.restore_confirm_title": "Confirm Restore",
  "gov.restore_confirm": "Overwrite .claude/ .codex/ .codebuddy/ in workspace with the snapshot?",
  "gov.restore_ok": "Restored: {files}",
  "gov.restore_fail": "Restore failed: {e}",
  "gov.update_contract": "Update",
  "gov.update_contract_dialog": "Update Contract",
  "gov.update_hint": "Add a decided feature (existing ones get confirmation count +1):",
  "gov.feature_name": "Name:",
  "gov.feature_location": "File:",
  "gov.feature_sig": "Signature:",
  "gov.name_required": "Name required",
  "lesson.dialog_title": "Lesson Manager",
  "lesson.search_placeholder": "Search...",
  "lesson.search": "Search",
  "lesson.tab_instance": "Instance",
  "lesson.tab_abstract": "Abstract",
  "lesson.tab_pending": "Pending",
  "lesson.verify": "Verify",
  "lesson.promote": "Promote",
  "lesson.verified": "Verified: {id}",
  "lesson.promoted": "Promoted: {id}",
  "lesson.promote_title": "Promote to Abstract",
  "lesson.promote_hint": "Enter tech_stack (e.g. PySide6, React):",
  "lesson.not_found": "Lesson not found",
  "action.template_mgr": "Manage Templates",
  "template.dialog_title": "Template Manager",
  "template.list_header": "Templates:",
  "template.add": "＋ New",
  "template.delete": "✕ Delete",
  "template.edit_header": "Edit:",
  "template.name": "Name:",
  "template.desc": "Description:",
  "template.header_fmt": "Header Format:",
  "template.body_fmt": "Body Format:",
  "template.save": "Save",
  "template.confirm_delete": "Delete template \"{name}\"?",
  "template.saved": "Template saved: {name}",
```

---

## 最终文件清单

| Task | 文件 | 操作 |
|------|------|------|
| H | `workshop_tab.py` | 改 2 行（L149-152） |
| I | `governance.py` | Identity 卡片加按钮行（L259→L261 之间）+ 4 个新方法（L335→L337 之间） |
| J | `frontend/lesson_dialog.py` | **创建新文件** ~160 行 |
| J | `governance.py` | 替换 `_on_view_lessons`（L337-359→新 8 行） |
| J | `build.py` | hidden_imports 追加 `frontend.lesson_dialog` |
| K | `governance.py` | Contract 按钮行加 1 按钮（L183）+ 1 个新方法 |
| L | `workshop_tab.py` | 加 ⚙ 按钮（L159→L160 之间）+ 1 个新方法（L184 后） |
| — | `zh.json` | 追加 47 行 |
| — | `en.json` | 追加 47 行 |

## 验证脚本（全部完成后运行）

```bash
python -c "
from frontend.workspace.workshop_tab import WorkshopTabMixin
from frontend.workspace.governance import GovernanceMixin
from frontend.lesson_dialog import LessonDialog
print('All imports OK')
"
```
