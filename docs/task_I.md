# I — Memory Snapshot 管理

在 Identity Guard 卡片底部加 3 个按钮 + 4 个回调方法。

## Step 1: Read
```
Read frontend/workspace/governance.py L255-L263
```

## Step 2: 卡片底部加按钮行

文件: `frontend/workspace/governance.py`

```python
# old (L259-L261)
            card.layout().addWidget(ok)

        return card
```

```python
# new
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

## Step 3: Read 确认插入位置
```
Read frontend/workspace/governance.py L333-L340
```
找到 `_on_view_lessons` 方法签名的行号。

## Step 4: 在 _on_view_lessons 之前插入 4 个方法

在 `def _on_view_lessons(self):` 这一行**之前**插入：

```python
    def _on_snapshot_now(self):
        from backend.core.identity.snapshot import snapshot_tool_memories
        ws = Path(self.state.project.workspace_path)
        bk = Path(self.state.project.backup_path)
        result = snapshot_tool_memories(str(ws), str(bk), self.state.project)
        snapped = result.get("snapped", [])
        if snapped:
            self._log(_tr("gov.snap_ok", "已快照: {files}").format(files=", ".join(snapped)))
        else:
            self._log(_tr("gov.snap_empty", "无记忆文件可快照"))

    def _on_list_snapshots(self):
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
                card.setStyleSheet(f"background:{t.bg};border:.5px solid {t.bdr};border-radius:4px;padding:8px;margin:2px 0;")
                cl = QVBoxLayout(card)
                cl.setSpacing(2)
                hdr = QHBoxLayout()
                hdr.addWidget(QLabel(f'<b style="color:{t.txt};">{s["source"]}</b>'))
                hdr.addStretch()
                hdr.addWidget(QLabel(f'<span style="color:{t.txt3};font-size:10px;">{s["timestamp"]}</span>'))
                cl.addLayout(hdr)
                row2 = QHBoxLayout()
                row2.addStretch()
                rbtn = QPB(_tr("gov.restore_this", "恢复此版本"))
                rbtn.setProperty("variant", "ghost")
                ts = s["timestamp"]
                rbtn.clicked.connect(lambda checked, ts=ts, d=dlg: self._do_restore(ts) and d.accept())
                row2.addWidget(rbtn)
                cl.addLayout(row2)
                lo.addWidget(card)
        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(dlg.accept)
        lo.addWidget(close_btn)
        dlg.exec()

    def _on_restore_latest(self):
        self._do_restore(None)

    def _do_restore(self, ts=None):
        from backend.core.identity.snapshot import restore_tool_memories
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(self, _tr("gov.restore_confirm_title", "确认恢复"),
            _tr("gov.restore_confirm", "将用快照覆盖 workspace 中的 .claude/ .codex/ .codebuddy/？"),
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return False
        ws = Path(self.state.project.workspace_path)
        bk = Path(self.state.project.backup_path)
        result = restore_tool_memories(str(bk), str(ws), ts)
        restored = result.get("restored", [])
        if "error" in result:
            self._log(_tr("gov.restore_fail", "恢复失败: {e}").format(e=result["error"]))
        else:
            self._log(_tr("gov.restore_ok", "已恢复: {files}").format(files=", ".join(restored)))
        return True

```

## 验证
```
grep "_on_snapshot_now" frontend/workspace/governance.py
grep "_do_restore" frontend/workspace/governance.py
```
