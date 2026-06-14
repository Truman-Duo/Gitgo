# Claude Code 执行规格 — Round 4

> 本轮最小化：2 个 Task，0 个新文件，填实 2 个 Action Bar 空壳 + Governance Tab 顶部加质量迷你行。

---

## Task M：Governance Tab 质量迷你指标行

### 目标

在 Governance Tab 三张卡片上方加一行 4 个迷你指标 pill：总建议数 / 采纳率 / 修改率 / 拒绝率。
数据为空时自动隐藏该行。

### 后端 API

```python
from backend.core.governance.quality import load_suggestion_pairs, compute_quality_metrics
pairs = load_suggestion_pairs(self.state.project.name)
metrics = compute_quality_metrics(pairs)
# metrics["suggestion_count"] → int
# metrics["by_type"]["formalize"]["acceptance_rate"] → float
```

### 改动：`governance.py`

在 `_build_governance_cards` 方法开头（L96 `lay = self.state.gov_layout` 之前），加载质量数据并插入迷你行：

```python
    def _build_governance_cards(self):
        lay = self.state.gov_layout
```

改为：

```python
    def _build_governance_cards(self):
        # ── 质量迷你行 ──
        self._build_quality_row()
        lay = self.state.gov_layout
```

### 新增方法：`governance.py`

在 `_build_lesson_card` 方法之后、`_on_view_contract` 之前插入：

```python
    def _build_quality_row(self):
        """在 Governance Tab 顶部显示质量迷你指标"""
        try:
            from backend.core.governance.quality import load_suggestion_pairs, compute_quality_metrics
            pairs = load_suggestion_pairs(self.state.project.name)
            if not pairs:
                return
            metrics = compute_quality_metrics(pairs)
            fm = metrics.get("by_type", {}).get("formalize", {})
            if not fm:
                return
        except Exception:
            return

        t = get_theme()
        row = QWidget()
        row.setObjectName("gov_quality_row")
        lo = QHBoxLayout(row)
        lo.setContentsMargins(4, 0, 4, 0)
        lo.setSpacing(12)

        total = metrics.get("suggestion_count", 0)
        lo.addWidget(self._mini_pill(
            _tr("gov.q_total", "建议"), str(total), t.blue))
        lo.addWidget(self._mini_pill(
            _tr("gov.q_accept", "采纳"),
            f"{int(fm.get('acceptance_rate', 0) * 100)}%", t.success))
        lo.addWidget(self._mini_pill(
            _tr("gov.q_modify", "修改"),
            f"{int(fm.get('modification_rate', 0) * 100)}%", t.amber))
        lo.addWidget(self._mini_pill(
            _tr("gov.q_reject", "拒绝"),
            f"{int(fm.get('rejection_rate', 0) * 100)}%", t.danger_txt))
        lo.addStretch()

        # 插入到 layout 最顶部
        gov_lay = self.state.gov_layout
        # 移除旧的 quality row（如果有）
        for i in range(gov_lay.count()):
            w = gov_lay.itemAt(i).widget()
            if w and w.objectName() == "gov_quality_row":
                w.deleteLater()
        gov_lay.insertWidget(0, row)

    def _mini_pill(self, label: str, value: str, color: str) -> QWidget:
        t = get_theme()
        w = QWidget()
        lo = QHBoxLayout(w)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(4)
        lbl = QLabel(f'<span style="font-size:10px;color:{t.txt3};">{label}</span>')
        lo.addWidget(lbl)
        val = QLabel(f'<span style="font-size:13px;font-weight:700;color:{color};">{value}</span>')
        lo.addWidget(val)
        return w
```

### 不需要 i18n 新 key

复用 `_tr()` 的 fallback 机制——key 不存在时直接用英文 fallback。

---

## Task N：Wire up Export 按钮

### 目标

Workshop Tab 的 Action Bar「Export Tasks」按钮目前只打日志。改为弹出 `QFileDialog` 保存 JSON。

### 后端

```python
from backend.core.state_reader import StateReader
# 或直接序列化 self.state.session 数据
```

### 改动：`builder.py`

找到 `_on_action_export` 方法（L128-129）：

```python
    def _on_action_export(self):
        self._log(_tr("action.export", "Export — not implemented in UI"))
```

改为：

```python
    def _on_action_export(self):
        idx = self.state.tab_bar.currentIndex()
        if idx == 0:
            self._do_export_workshop()
        elif idx == 3:
            self._do_export_history()
        else:
            self._log(_tr("action.export_unavailable", "当前 Tab 不支持导出"))
```

### 新增方法：`builder.py`

在 `_on_action_export` 之后插入：

```python
    def _do_export_workshop(self):
        import json
        from PySide6.QtWidgets import QFileDialog

        data = {
            "project": self.state.project.name,
            "workspace_commits": [
                {"hash": c.hash[:8], "type": c.type, "subject": c.subject}
                for c in getattr(self.state.session, 'commits', [])
            ],
            "formal_commits": [
                {"message": fc.message, "synced": fc.synced, "pushed": fc.pushed}
                for fc in getattr(self.state.session, 'formal_commits', [])
            ],
            "file_count": len(getattr(self.state.session, 'entries', [])),
        }
        path, _ = QFileDialog.getSaveFileName(
            self,
            _tr("export.dialog_title", "导出任务"),
            f"{self.state.project.name}_export.json",
            "JSON (*.json)",
        )
        if path:
            Path(path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._log(_tr("export.saved", "已导出到: {path}").format(path=path))

    def _do_export_history(self):
        import json
        from PySide6.QtWidgets import QFileDialog
        from backend.core.history import HistoryManager, HistoryEntry

        entries = HistoryManager.load()
        data = [
            {
                "timestamp": e.timestamp,
                "project": e.project_name,
                "operation": e.operation,
                "message": e.commit_message[:80] if e.commit_message else "",
            }
            for e in entries
            if e.project_name == self.state.project.name
        ]
        path, _ = QFileDialog.getSaveFileName(
            self,
            _tr("export.history_title", "导出历史"),
            f"{self.state.project.name}_history.json",
            "JSON (*.json)",
        )
        if path:
            Path(path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._log(_tr("export.saved", "已导出到: {path}").format(path=path))
```

### 需要额外 import

`builder.py` 顶部已有 `from pathlib import Path`（检查确认）。如果没有，加 `from pathlib import Path`。

---

## i18n（最小集，仅 4 个 key）

zh.json:
```json
  "export.dialog_title": "导出任务",
  "export.history_title": "导出历史",
  "export.saved": "已导出到: {path}",
  "action.export_unavailable": "当前 Tab 不支持导出",
```

en.json:
```json
  "export.dialog_title": "Export Tasks",
  "export.history_title": "Export History",
  "export.saved": "Exported to: {path}",
  "action.export_unavailable": "Export not available for this tab",
```

---

## 验证

```
grep "_build_quality_row" frontend/workspace/governance.py
grep "_do_export_workshop" frontend/workspace/builder.py
```
