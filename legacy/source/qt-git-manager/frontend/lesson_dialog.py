"""LessonDialog — 知识传承浏览 / 搜索 / 操作"""
import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)
from backend.core.i18n import _tr
from themes import get_theme


class LessonDialog(QDialog):
    """Lesson 浏览器 — Abstract / Instance / Pending 三 Tab + 搜索 + 右键操作"""

    def __init__(self, workspace_path: str, project_name: str, parent=None):
        super().__init__(parent)
        self.ws_path = Path(workspace_path)
        self.project_name = project_name
        self.setWindowTitle(_tr("gov.lessons_dialog", "Lessons"))
        self.setMinimumSize(650, 480)

        self._abstract: list = []
        self._instance: list = []
        self._pending: list = []

        self._init_ui()
        self._load_data()

    # ── UI 构建 ─────────────────────────────────────────

    def _init_ui(self):
        t = get_theme()
        lo = QVBoxLayout(self)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            _tr("gov.lesson_search_placeholder", "Search lesson rule..."))
        self.search_input.textChanged.connect(self._on_search)
        search_row.addWidget(self.search_input)
        lo.addLayout(search_row)

        self.tabs = QTabWidget()
        self.abstract_list = QListWidget()
        self.instance_list = QListWidget()
        self.pending_list = QListWidget()
        for lst in [self.abstract_list, self.instance_list, self.pending_list]:
            lst.setContextMenuPolicy(Qt.CustomContextMenu)
            lst.customContextMenuRequested.connect(self._on_context_menu)
        self.tabs.addTab(self.abstract_list,
                         _tr("gov.abstract_lessons", "Abstract"))
        self.tabs.addTab(self.instance_list,
                         _tr("gov.instance_lessons", "Instance"))
        self.tabs.addTab(self.pending_list,
                         _tr("gov.pending_lessons", "Pending"))
        lo.addWidget(self.tabs)

        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(self.accept)
        lo.addWidget(close_btn, alignment=Qt.AlignRight)

    # ── 数据加载 ─────────────────────────────────────────

    def _load_data(self):
        from backend.core.knowledge.manager import LessonManager
        self._abstract = list(LessonManager.load_abstract(self.ws_path))
        self._instance = list(LessonManager.load_instance(
            self.ws_path, self.project_name))
        self._pending = list(LessonManager.load_pending(
            self.ws_path, self.project_name))
        self._populate_lists()

    def _populate_lists(self, filter_text: str = ""):
        ft = filter_text.lower()

        def _fill(lst, items):
            lst.clear()
            for item in items:
                rule = getattr(item, 'rule', '')
                if ft and ft not in rule.lower():
                    continue
                sev = getattr(item, 'severity', '?') if hasattr(item, 'severity') else '?'
                text = f"[{sev}] {rule[:120]}"
                list_item = QListWidgetItem(text)
                list_item.setData(Qt.UserRole, item)
                lst.addItem(list_item)

        _fill(self.abstract_list, self._abstract)
        _fill(self.instance_list, self._instance)
        _fill(self.pending_list, self._pending)

    def _on_search(self, text: str):
        self._populate_lists(text)

    # ── 右键菜单 ─────────────────────────────────────────

    def _on_context_menu(self, pos):
        lst = self.sender()
        if not lst:
            return
        item = lst.itemAt(pos)
        if not item:
            return
        lesson = item.data(Qt.UserRole)
        menu = QMenu(self)

        verify_action = menu.addAction(_tr("gov.lesson_verify", "Verify"))
        delete_action = menu.addAction(_tr("gov.lesson_delete", "Delete"))

        if not getattr(lesson, 'abstract', False):
            promote_action = menu.addAction(
                _tr("gov.lesson_create_abstract", "Promote to Abstract"))
        else:
            promote_action = None
            demote_action = menu.addAction(
                _tr("gov.lesson_demote", "Demote to Instance"))

        action = menu.exec(lst.viewport().mapToGlobal(pos))
        if action == verify_action:
            self._verify_lesson(lesson)
        elif action == delete_action:
            self._delete_lesson(lesson)
        elif promote_action and action == promote_action:
            self._promote_lesson(lesson)
        elif not getattr(lesson, 'abstract', False) and action == demote_action:
            self._demote_lesson(lesson)

    # ── 操作 ─────────────────────────────────────────────

    def _verify_lesson(self, lesson):
        from backend.core.knowledge.manager import LessonManager
        lesson_id = getattr(lesson, 'id', '')
        LessonManager.verify(self.ws_path, lesson_id, self.project_name)
        self._parent_log(
            _tr("gov.lesson_verified", "Lesson verified"))
        self._reload()

    def _promote_lesson(self, lesson):
        from backend.core.knowledge.manager import LessonManager
        reply = QMessageBox.question(
            self, _tr("gov.lesson_promote_confirm_title", "Confirm Promote"),
            _tr("gov.lesson_promote_confirm",
                "Promote this lesson to cross-project abstract?"))
        if reply != QMessageBox.Yes:
            return
        lesson_id = getattr(lesson, 'id', '')
        tech_stack = getattr(lesson, 'tech_stack', '') or 'general'
        LessonManager.promote_to_abstract(
            self.ws_path, lesson_id, self.project_name, tech_stack)
        self._parent_log(
            _tr("gov.lesson_promoted", "Promoted to abstract"))
        self._reload()

    def _demote_lesson(self, lesson):
        reply = QMessageBox.question(
            self, _tr("gov.lesson_demote_confirm_title", "Confirm Demote"),
            _tr("gov.lesson_demote_confirm",
                "Demote this abstract lesson to project instance?"))
        if reply != QMessageBox.Yes:
            return
        lesson.abstract = False
        lesson.project_name = self.project_name
        from backend.core.knowledge.manager import LessonManager
        LessonManager.save(self.ws_path, lesson)
        self._parent_log(
            _tr("gov.lesson_demoted", "Demoted to instance"))
        self._reload()

    def _delete_lesson(self, lesson):
        reply = QMessageBox.question(
            self, "",
            _tr("gov.lesson_confirm_delete",
                "Delete lesson \"{rule}\"?").format(
                rule=getattr(lesson, 'rule', '')[:40]))
        if reply != QMessageBox.StandardButton.Yes:
            return
        lesson_id = getattr(lesson, 'id', '')
        # 从对应文件中移除该行
        from backend.core.knowledge.manager import LessonManager
        is_abstract = getattr(lesson, 'abstract', False)
        if is_abstract:
            tech_stack = getattr(lesson, 'tech_stack', '') or 'general'
            fp = LessonManager._abstract_path(self.ws_path, tech_stack)
        else:
            fp = LessonManager._instance_path(self.ws_path, self.project_name)
        _remove_lesson_from_file(fp, lesson_id)
        # 同时从 pending 中移除
        pp = LessonManager._pending_path(self.ws_path, self.project_name)
        _remove_lesson_from_file(pp, lesson_id)
        self._parent_log(
            _tr("gov.lesson_deleted", "Lesson deleted"))
        self._reload()

    def _reload(self):
        self.lesson_list.clear() if hasattr(self, 'lesson_list') else None
        self._load_data()

    def _parent_log(self, msg: str):
        parent = self.parent()
        if parent and hasattr(parent, '_log'):
            parent._log(msg)


def _remove_lesson_from_file(filepath: Path, lesson_id: str):
    """从 JSONL 文件中移除指定 id 的 lesson 行。"""
    if not filepath.exists():
        return
    lines = filepath.read_text(encoding="utf-8").splitlines()
    kept = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if obj.get("id") != lesson_id:
            kept.append(line)
    filepath.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
