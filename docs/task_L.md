# L — Template CRUD 对话框

## Step 1: Read
```
Read frontend/workspace/workshop_tab.py L158-L162
```

## Step 2: Template 下拉旁加 ⚙ 按钮

文件: `frontend/workspace/workshop_tab.py`

```python
# old
        lo.addWidget(self.state.template_combo)

        self.state.delete_formal_btn = QPushButton("✕")
```

```python
# new
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

## Step 3: Read 文件末尾
```
Read frontend/workspace/workshop_tab.py L180-L185
```

## Step 4: 在文件末尾（class 内最后）加 _on_manage_templates 方法

在 `ctr_layout.addWidget(row)` 之后、文件结束之前插入：

```python
    def _on_manage_templates(self):
        from PySide6.QtWidgets import QListWidget, QPlainTextEdit, QInputDialog, QMessageBox
        from backend.core.template_manager import TemplateManager, CommitTemplate
        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("template.dialog_title", "模板管理"))
        dlg.setMinimumSize(600, 450)
        t = get_theme()
        lo = QVBoxLayout(dlg)
        lo.setSpacing(8)
        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel(_tr("template.list_header", "模板列表:")))
        tl = QListWidget()
        left.addWidget(tl, 1)
        ab = QPushButton(_tr("template.add", "＋ 新建"))
        ab.setProperty("variant", "secondary")
        left.addWidget(ab)
        db = QPushButton(_tr("template.delete", "✕ 删除"))
        db.setProperty("variant", "danger")
        db.setEnabled(False)
        left.addWidget(db)
        body.addLayout(left, 1)
        right = QVBoxLayout()
        right.addWidget(QLabel(_tr("template.edit_header", "编辑:")))
        nr = QHBoxLayout()
        nr.addWidget(QLabel(_tr("template.name", "名称:")))
        ne = QLineEdit()
        nr.addWidget(ne, 1)
        right.addLayout(nr)
        dr = QHBoxLayout()
        dr.addWidget(QLabel(_tr("template.desc", "描述:")))
        de = QLineEdit()
        dr.addWidget(de, 1)
        right.addLayout(dr)
        right.addWidget(QLabel(_tr("template.header_fmt", "Header 格式:")))
        he = QPlainTextEdit()
        he.setMaximumHeight(60)
        right.addWidget(he)
        right.addWidget(QLabel(_tr("template.body_fmt", "Body 格式:")))
        be = QPlainTextEdit()
        be.setMaximumHeight(120)
        right.addWidget(be)
        sb = QPushButton(_tr("template.save", "保存"))
        sb.setProperty("variant", "primary")
        right.addWidget(sb)
        body.addLayout(right, 2)
        lo.addLayout(body)
        cb = QPushButton(_tr("settings.ok", "关闭"))
        cb.clicked.connect(dlg.accept)
        lo.addWidget(cb)

        def refresh_list():
            tl.clear()
            for x in TemplateManager.load():
                tl.addItem(x.name)

        def load_current():
            it = tl.currentItem()
            if it:
                db.setEnabled(it.text() != "default")
                x = TemplateManager.get_template(it.text())
                if x:
                    ne.setText(x.name)
                    de.setText(x.description)
                    he.setPlainText(x.header_format)
                    be.setPlainText(x.body_format)

        def on_add():
            ex = {x.name for x in TemplateManager.load()}
            nm = "new_template"
            i = 1
            while nm in ex:
                nm = f"new_template_{i}"
                i += 1
            TemplateManager.save(TemplateManager.load() + [CommitTemplate(name=nm)])
            refresh_list()
            for i2 in range(tl.count()):
                if tl.item(i2).text() == nm:
                    tl.setCurrentRow(i2)
                    break
            load_current()

        def on_delete():
            it = tl.currentItem()
            if not it or it.text() == "default":
                return
            r = QMessageBox.question(dlg, _tr("dialog.confirm_delete", "确认删除"),
                _tr("template.confirm_delete", "删除模板「{name}」？").format(name=it.text()),
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                return
            TemplateManager.save([x for x in TemplateManager.load() if x.name != it.text()])
            refresh_list()

        def on_save():
            it = tl.currentItem()
            if not it:
                return
            old = it.text()
            nw = ne.text().strip()
            if not nw:
                return
            all_t = TemplateManager.load()
            for x in all_t:
                if x.name == old:
                    x.name = nw
                    x.description = de.text().strip()
                    x.header_format = he.toPlainText()
                    x.body_format = be.toPlainText()
                    break
            TemplateManager.save(all_t)
            refresh_list()
            if hasattr(self.state, 'template_combo'):
                cb2 = self.state.template_combo
                cb2.clear()
                cb2.addItems([x.name for x in TemplateManager.load()])
                cb2.setCurrentText(nw)
            self._log(_tr("template.saved", "模板已保存: {name}").format(name=nw))

        tl.currentItemChanged.connect(lambda: load_current())
        ab.clicked.connect(on_add)
        db.clicked.connect(on_delete)
        sb.clicked.connect(on_save)
        refresh_list()
        dlg.exec()
```

## 验证
```
grep "_on_manage_templates" frontend/workspace/workshop_tab.py
```
