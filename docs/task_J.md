# J — Lesson 管理弹窗

## Step 1: 创建新文件

创建 `frontend/lesson_dialog.py`，完整内容如下：

```python
"""LessonDialog — Lesson 搜索/验证/提升"""
from pathlib import Path
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
    QMessageBox, QInputDialog, QScrollArea,
)
from backend.core.i18n import _tr
from themes import get_theme
from backend.core.knowledge.models import Lesson


class LessonDialog(QDialog):
    def __init__(self, workspace_path: str, project_name: str, parent=None):
        super().__init__(parent)
        self.ws_path = Path(workspace_path)
        self.project_name = project_name
        self.setWindowTitle(_tr("lesson.dialog_title", "Lesson 管理"))
        self.setMinimumSize(650, 500)
        lo = QVBoxLayout(self)
        sr = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(_tr("lesson.search_placeholder", "搜索..."))
        self.search_input.textChanged.connect(self._on_search)
        sr.addWidget(self.search_input, 1)
        sb = QPushButton(_tr("lesson.search", "搜索"))
        sb.setProperty("variant", "secondary")
        sb.clicked.connect(self._on_search)
        sr.addWidget(sb)
        lo.addLayout(sr)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._page(), _tr("lesson.tab_instance", "Instance"))
        self.tabs.addTab(self._page(), _tr("lesson.tab_abstract", "Abstract"))
        self.tabs.addTab(self._page(), _tr("lesson.tab_pending", "Pending"))
        lo.addWidget(self.tabs, 1)
        cb = QPushButton(_tr("settings.ok", "OK"))
        cb.clicked.connect(self.accept)
        lo.addWidget(cb)
        self._load_data()

    def _page(self):
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 8, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        c = QWidget()
        c.setLayout(QVBoxLayout())
        c.layout().addStretch()
        sc.setWidget(c)
        lo.addWidget(sc)
        return w

    def _load_data(self, search_query: str = ""):
        from backend.core.knowledge.manager import LessonManager
        if search_query:
            lessons = LessonManager.search(self.ws_path, search_query, self.project_name)
            self._fill(0, lessons)
            self._fill(1, [])
            self._fill(2, [])
        else:
            self._fill(0, LessonManager.load_instance(self.ws_path, self.project_name))
            self._fill(1, LessonManager.load_abstract(self.ws_path))
            self._fill(2, LessonManager.load_pending(self.ws_path, self.project_name))

    def _fill(self, idx, lessons):
        page = self.tabs.widget(idx)
        sc = page.findChild(QScrollArea)
        c = sc.widget()
        lay = c.layout()
        while lay.count() > 1:
            w = lay.takeAt(0).widget()
            if w: w.deleteLater()
        t = get_theme()
        for les in lessons:
            card = QFrame()
            card.setStyleSheet(f"background:{t.bg};border:.5px solid {t.bdr};border-radius:6px;padding:10px;margin:2px 0;")
            cl = QVBoxLayout(card)
            cl.setSpacing(4)
            hdr = QHBoxLayout()
            cat = QLabel(les.category or "general")
            cat.setStyleSheet(f"font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;background:{t.blue}20;color:{t.blue};")
            hdr.addWidget(cat)
            scm = {"critical": t.danger_txt, "high": t.amber, "medium": t.txt3, "low": t.txt2}
            sev = QLabel(les.severity.upper())
            sev.setStyleSheet(f"font-size:9px;font-weight:600;padding:1px 6px;border-radius:3px;color:{scm.get(les.severity, t.txt3)};")
            hdr.addWidget(sev)
            hdr.addStretch()
            cl.addLayout(hdr)
            rl = QLabel(f'<span style="font-size:11px;color:{t.txt};">{les.rule[:120]}</span>')
            rl.setWordWrap(True)
            cl.addWidget(rl)
            br = QHBoxLayout()
            vb = QPushButton(_tr("lesson.verify", "Verify"))
            vb.setProperty("variant", "ghost")
            lid = les.id
            vb.clicked.connect(lambda ch, lid=lid: self._verify(lid))
            br.addWidget(vb)
            if not les.abstract:
                pb = QPushButton(_tr("lesson.promote", "Promote"))
                pb.setProperty("variant", "ghost")
                pb.clicked.connect(lambda ch, lid=lid: self._promote(lid))
                br.addWidget(pb)
            br.addStretch()
            cl.addLayout(br)
            lay.insertWidget(lay.count() - 1, card)

    def _on_search(self):
        self._load_data(self.search_input.text().strip())

    def _verify(self, lid):
        from backend.core.knowledge.manager import LessonManager
        r = LessonManager.verify(self.ws_path, lid, self.project_name)
        if r:
            p = self.parent()
            if p and hasattr(p, '_log'): p._log(_tr("lesson.verified", "已验证: {id}").format(id=lid[:12]))
            self._load_data(self.search_input.text().strip())
        else:
            QMessageBox.warning(self, _tr("dialog.hint", "提示"), _tr("lesson.not_found", "未找到"))

    def _promote(self, lid):
        from backend.core.knowledge.manager import LessonManager
        tech, ok = QInputDialog.getText(self, _tr("lesson.promote_title", "提升为抽象层"),
            _tr("lesson.promote_hint", "输入 tech_stack（如 PySide6）："))
        if not ok or not tech.strip(): return
        r = LessonManager.promote_to_abstract(self.ws_path, lid, self.project_name, tech.strip())
        if r:
            p = self.parent()
            if p and hasattr(p, '_log'): p._log(_tr("lesson.promoted", "已提升: {id}").format(id=lid[:12]))
            self._load_data(self.search_input.text().strip())
        else:
            QMessageBox.warning(self, _tr("dialog.hint", "提示"), _tr("lesson.not_found", "未找到"))
```

## Step 2: 替换 governance.py 中的 _on_view_lessons

文件: `frontend/workspace/governance.py`

```python
# old — 整个 _on_view_lessons 方法 (约 L337-L359)
    def _on_view_lessons(self):
        """打开 Lesson 列表弹窗 — 简版（完整版在 Round 3）"""
        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("gov.lessons_dialog", "Lessons"))
        dlg.setMinimumSize(500, 400)
        lo = QVBoxLayout(dlg)
        # 简单列表
        from backend.core.knowledge.manager import LessonManager
        ws_path = Path(self.state.project.workspace_path)
        instance = LessonManager.load_instance(ws_path, self.state.project.name)
        abstract = LessonManager.load_abstract(ws_path)
        all_lessons = list(abstract) + list(instance)
        if not all_lessons:
            lo.addWidget(QLabel(_tr("gov.no_lessons", "No lessons recorded")))
        else:
            for les in all_lessons[:20]:
                tag = "ABS" if getattr(les, 'abstract', False) else "INS"
                lo.addWidget(QLabel(
                    f'[{tag}] {getattr(les, "rule", "")[:100]}'))
        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(dlg.accept)
        lo.addWidget(close_btn)
        dlg.exec()
```

```python
# new
    def _on_view_lessons(self):
        from frontend.lesson_dialog import LessonDialog
        dlg = LessonDialog(
            self.state.project.workspace_path,
            self.state.project.name,
            self,
        )
        dlg.exec()
```

## Step 3: build.py 加 hidden import

文件: `build.py`

在文件中的 `_HIDDEN_IMPORTS` 列表（搜索 `_HIDDEN_IMPORTS` 找到），追加一条：
```python
    "frontend.lesson_dialog",
```

## 验证
```
ls frontend/lesson_dialog.py && echo "EXISTS"
grep "from frontend.lesson_dialog" frontend/workspace/governance.py
grep "frontend.lesson_dialog" build.py
```
