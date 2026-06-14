# K — Contract 更新按钮

## Step 1: Read
```
Read frontend/workspace/governance.py L177-L186
```

## Step 2: 加 Update 按钮

文件: `frontend/workspace/governance.py`

```python
# old
        # 按钮
        btn_row = QHBoxLayout()
        view_btn = QPushButton(_tr("gov.view_contract", "View Contract"))
        view_btn.setProperty("variant", "secondary")
        view_btn.clicked.connect(self._on_view_contract)
        btn_row.addWidget(view_btn)
        btn_row.addStretch()
        card.layout().addLayout(btn_row)
```

```python
# new
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

## Step 3: Read 确认插入位置
```
Read frontend/workspace/governance.py L335-L340
```
找到两个回调方法之间的空行位置。

## Step 4: 在 _on_view_contract 和 _on_view_lessons 之间插入

```python
    def _on_update_contract(self):
        from PySide6.QtWidgets import QLineEdit, QMessageBox
        from backend.core.contract import ContractManager
        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("gov.update_contract_dialog", "更新合约"))
        dlg.setMinimumSize(450, 300)
        lo = QVBoxLayout(dlg)
        lo.addWidget(QLabel(_tr("gov.update_hint", "添加一条 decided feature（已存在则增加确认计数）：")))
        nr = QHBoxLayout()
        nr.addWidget(QLabel(_tr("gov.feature_name", "名称:")))
        ni = QLineEdit()
        nr.addWidget(ni, 1)
        lo.addLayout(nr)
        lr = QHBoxLayout()
        lr.addWidget(QLabel(_tr("gov.feature_location", "文件:")))
        li = QLineEdit()
        lr.addWidget(li, 1)
        lo.addLayout(lr)
        sr = QHBoxLayout()
        sr.addWidget(QLabel(_tr("gov.feature_sig", "签名:")))
        si = QLineEdit()
        sr.addWidget(si, 1)
        lo.addLayout(sr)
        br = QHBoxLayout()
        okb = QPushButton(_tr("settings.ok", "确认"))
        cb = QPushButton(_tr("settings.cancel", "取消"))
        br.addStretch()
        br.addWidget(okb)
        br.addWidget(cb)
        lo.addLayout(br)
        def on_ok():
            n = ni.text().strip()
            if not n:
                QMessageBox.warning(dlg, _tr("dialog.hint", "提示"), _tr("gov.name_required", "名称不能为空"))
                return
            ContractManager.update_feature(Path(self.state.project.workspace_path), self.state.project.name, n, li.text().strip(), si.text().strip())
            dlg.accept()
            self._load_governance_data()
        okb.clicked.connect(on_ok)
        cb.clicked.connect(dlg.reject)
        dlg.exec()
```

## 验证
```
grep "_on_update_contract" frontend/workspace/governance.py
```
