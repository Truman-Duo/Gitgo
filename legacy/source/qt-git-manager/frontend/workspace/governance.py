"""GovernanceMixin — 治理 Tab：Contract / Identity / Lesson"""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget, QDialog, QPlainTextEdit,
)
from backend.core.i18n import _tr
from themes import get_theme


class GovernanceMixin:
    """合约 / 完整性 / 知识 — 三卡片垂直布局"""

    # ── Tab 构建入口（由 builder.py _init_ui 调用）─────────

    def _build_governance_tab(self) -> QWidget:
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        container.setObjectName("governance_container")
        self.state.gov_layout = QVBoxLayout(container)
        self.state.gov_layout.setContentsMargins(16, 16, 16, 16)
        self.state.gov_layout.setSpacing(16)
        self.state.gov_layout.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll)

        # Tab 打开时自动加载数据
        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, self._load_governance_data)
        return outer

    # ── 数据加载 ─────────────────────────────────────────

    def _load_governance_data(self):
        ws_path = Path(self.state.project.workspace_path)
        t = get_theme()

        # ── Contract ──
        from backend.core.contract import ContractManager, detect_drift
        contract = ContractManager.load(ws_path)
        if contract:
            d = contract.to_dict()
            self.state.contract_data = d
            # 漂移检测
            changed_files = []
            for e in (self.state.session.entries or []):
                if getattr(e, 'status', '') in ('new', 'modified'):
                    changed_files.append(getattr(e, 'rel_path', ''))
            alerts = detect_drift(ws_path, changed_files, contract)
            self.state.contract_data["drift_alerts"] = alerts
        else:
            self.state.contract_data = {}

        # ── Identity ──
        from backend.core.identity.guard import _run_integrity_checks
        warnings = _run_integrity_checks(
            list(self.state.session.entries or []),
            str(ws_path),
            self.state.project,
        )
        # 计算完整度分数
        total_checks = 3
        passed = total_checks - len([w for w in warnings if w])
        score = int(passed / total_checks * 100) if total_checks > 0 else 100
        self.state.integrity_status = {
            "score": score,
            "warnings": warnings,
        }

        # ── Lessons ──
        from backend.core.knowledge.manager import LessonManager
        abstract = LessonManager.load_abstract(ws_path)
        instance = LessonManager.load_instance(ws_path, self.state.project.name)
        pending = LessonManager.load_pending(ws_path, self.state.project.name)
        self.state.lesson_data = {
            "abstract": len(abstract),
            "instance": len(instance),
            "pending": len(pending),
            "recent": instance[:5],
        }

        self._build_governance_cards()

    # ── 卡片构建 ─────────────────────────────────────────

    def _build_governance_cards(self):
        # 清空旧卡片（Stretch 最后一个保留）
        lay = self.state.gov_layout
        while lay.count() > 1:
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        lay.insertWidget(0, self._build_contract_card())
        lay.insertWidget(1, self._build_identity_card())
        lay.insertWidget(2, self._build_lesson_card())

    # ══════════════════════════════════════════════════════
    # 1. Contract Card
    # ══════════════════════════════════════════════════════

    def _build_contract_card(self) -> QWidget:
        t = get_theme()
        d = self.state.contract_data
        alerts = d.get("drift_alerts", [])
        drift_count = len([a for a in alerts if a.get("level") == "error"])
        warn_count = len(alerts) - drift_count

        card = self._make_card("contract")

        # 标题行
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        icon = QLabel("◈")
        icon.setStyleSheet(f"color:{t.blue};font-size:14px;")
        hdr.addWidget(icon)
        title = QLabel(_tr("gov.contract", "Project Contract"))
        title.setStyleSheet(f"font-size:14px;font-weight:600;color:{t.txt};")
        hdr.addWidget(title)
        hdr.addStretch()

        if not d:
            hdr.addWidget(self._pill(
                _tr("gov.no_contract", "Not found"), "warning"))
        elif drift_count == 0 and warn_count == 0:
            hdr.addWidget(self._pill(
                _tr("gov.no_drift", "No Drift"), "ok"))
        elif drift_count > 0:
            hdr.addWidget(self._pill(
                _tr("gov.drift_errors", "{n} errors").format(n=drift_count),
                "danger"))
        else:
            hdr.addWidget(self._pill(
                _tr("gov.drift_warnings", "{n} warnings").format(n=warn_count),
                "warning"))
        card.layout().addLayout(hdr)

        # 内容
        if d:
            content = QVBoxLayout()
            content.setSpacing(4)

            # 版本 + 功能数 + 技术栈
            info = QHBoxLayout()
            info.addWidget(QLabel(
                f'<span style="color:{t.txt3};font-size:11px;">'
                f'{_tr("gov.version", "Version")}: {d.get("updated", "?")}</span>'))
            info.addWidget(QLabel(
                f'<span style="color:{t.txt3};font-size:11px;">'
                f'{_tr("gov.features", "Features")}: '
                f'{len(d.get("decided_features", []))}</span>'))
            info.addWidget(QLabel(
                f'<span style="color:{t.txt3};font-size:11px;">'
                f'{_tr("gov.arch_constraints", "Constraints")}: '
                f'{len(d.get("architecture_constraints", []))}</span>'))
            info.addStretch()
            content.addLayout(info)

            # 漂移告警
            for a in alerts[:3]:
                alert = QLabel(
                    f'<span style="color:{t.danger_txt if a.get("level")=="error" else t.amber};'
                    f'font-size:10px;">⚠ {a.get("message", "")[:120]}</span>')
                content.addWidget(alert)
            card.layout().addLayout(content)

        # 按钮
        btn_row = QHBoxLayout()
        view_btn = QPushButton(_tr("gov.view_contract", "View Contract"))
        view_btn.setProperty("variant", "secondary")
        view_btn.clicked.connect(self._on_view_contract)
        btn_row.addWidget(view_btn)
        btn_row.addStretch()
        card.layout().addLayout(btn_row)

        return card

    # ══════════════════════════════════════════════════════
    # 2. Identity Guard Card
    # ══════════════════════════════════════════════════════

    def _build_identity_card(self) -> QWidget:
        t = get_theme()
        s = self.state.integrity_status
        score = s.get("score", 100)
        warnings = s.get("warnings", [])

        # 严重度
        if score >= 95:
            severity = "ok"
        elif score >= 70:
            severity = "warning"
        else:
            severity = "danger"

        card = self._make_card("identity", severity)

        # 标题行
        hdr = QHBoxLayout()
        icon = QLabel("●")
        icon.setStyleSheet(
            f"color:{t.success_txt if severity == 'ok' else t.amber if severity == 'warning' else t.danger_txt};"
            f"font-size:12px;")
        hdr.addWidget(icon)
        title = QLabel(_tr("gov.identity", "Identity Guard"))
        title.setStyleSheet(f"font-size:14px;font-weight:600;color:{t.txt};")
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self._pill(f"{score}%", severity))
        card.layout().addLayout(hdr)

        # 进度条
        bar_bg = QFrame()
        bar_bg.setFixedHeight(8)
        bar_bg.setStyleSheet(
            f"background:{t.bg3};border-radius:4px;border:none;")
        bar_layout = QHBoxLayout(bar_bg)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        fill = QFrame()
        fill.setFixedHeight(8)
        fill_color = (
            t.success if severity == "ok"
            else t.amber if severity == "warning"
            else t.danger_txt
        )
        fill.setStyleSheet(
            f"background:{fill_color};border-radius:4px;border:none;")
        # 用 stretch 模拟宽度
        bar_inner = QHBoxLayout()
        bar_inner.setContentsMargins(0, 0, 0, 0)
        bar_inner.addWidget(fill, score)
        bar_inner.addStretch(100 - score)
        bar_layout.addLayout(bar_inner)
        card.layout().addWidget(bar_bg)

        # 告警列表
        for w in warnings:
            if not w:
                continue
            wl = QLabel(
                f'<span style="color:{t.amber};font-size:11px;">'
                f'⚠ {w.get("message", str(w))[:100]}</span>')
            card.layout().addWidget(wl)

        if not any(warnings):
            ok = QLabel(
                f'<span style="color:{t.success_txt};font-size:11px;">'
                f'✓ {_tr("gov.integrity_ok", "All checks passed")}</span>')
            card.layout().addWidget(ok)

        return card

    # ══════════════════════════════════════════════════════
    # 3. Lesson Card
    # ══════════════════════════════════════════════════════

    def _build_lesson_card(self) -> QWidget:
        t = get_theme()
        d = self.state.lesson_data

        card = self._make_card("lesson")

        # 标题
        hdr = QHBoxLayout()
        icon = QLabel("◆")
        icon.setStyleSheet(f"color:{t.success};font-size:12px;")
        hdr.addWidget(icon)
        title = QLabel(_tr("gov.lessons", "Lesson System"))
        title.setStyleSheet(f"font-size:14px;font-weight:600;color:{t.txt};")
        hdr.addWidget(title)
        hdr.addStretch()
        card.layout().addLayout(hdr)

        # 计数
        stats = QHBoxLayout()
        stats.setSpacing(24)
        stats.addWidget(self._stat_label(
            str(d.get("abstract", 0)),
            _tr("gov.abstract_lessons", "Abstract")))
        stats.addWidget(self._stat_label(
            str(d.get("instance", 0)),
            _tr("gov.instance_lessons", "Instance")))
        stats.addWidget(self._stat_label(
            str(d.get("pending", 0)),
            _tr("gov.pending_lessons", "Pending"),
            t.amber if d.get("pending", 0) > 0 else t.txt2))
        stats.addStretch()
        card.layout().addLayout(stats)

        # 最近 lessons
        for les in d.get("recent", [])[:3]:
            tag = _tr("gov.abstract", "ABSTRACT") if getattr(les, 'abstract', False) \
                  else _tr("gov.instance", "INSTANCE")
            tag_color = t.blue if getattr(les, 'abstract', False) else t.txt3
            row = QHBoxLayout()
            row.addWidget(self._tag_label(tag, tag_color))
            row.addWidget(QLabel(
                f'<span style="font-size:11px;color:{t.txt2};">'
                f'{getattr(les, "rule", "")[:80]}</span>'))
            row.addStretch()
            card.layout().addLayout(row)

        # 按钮
        btn_row = QHBoxLayout()
        view_btn = QPushButton(_tr("gov.view_lessons", "View All"))
        view_btn.setProperty("variant", "secondary")
        view_btn.clicked.connect(self._on_view_lessons)
        btn_row.addWidget(view_btn)
        btn_row.addStretch()
        card.layout().addLayout(btn_row)

        return card

    # ══════════════════════════════════════════════════════
    # 交互回调
    # ══════════════════════════════════════════════════════

    def _on_view_contract(self):
        d = self.state.contract_data
        if not d:
            return
        import json
        content = json.dumps(d, indent=2, ensure_ascii=False)
        self._show_text_dialog(
            _tr("gov.contract_dialog", "Project Contract"), content)

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

    def _show_text_dialog(self, title: str, content: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(550, 400)
        lo = QVBoxLayout(dlg)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(content)
        lo.addWidget(editor)
        close_btn = QPushButton(_tr("settings.ok", "OK"))
        close_btn.clicked.connect(dlg.accept)
        lo.addWidget(close_btn)
        dlg.exec()

    # ══════════════════════════════════════════════════════
    # 工具方法
    # ══════════════════════════════════════════════════════

    def _make_card(self, kind: str, severity: str = "ok") -> QFrame:
        t = get_theme()
        severity_colors = {
            "ok": t.bdr,
            "warning": t.amber,
            "danger": t.danger_txt,
        }
        border = severity_colors.get(severity, t.bdr)
        card = QFrame()
        card.setObjectName(f"gov_card_{kind}")
        card.setProperty("severity", severity)
        card.setStyleSheet(
            f"QFrame#{card.objectName()}{{"
            f"background:{t.bg};border:1px solid {border};"
            f"border-radius:8px;}}")
        lo = QVBoxLayout(card)
        lo.setContentsMargins(16, 14, 16, 14)
        lo.setSpacing(8)
        return card

    def _pill(self, text: str, severity: str) -> QLabel:
        t = get_theme()
        colors = {
            "ok": (t.success_bg, t.success_txt),
            "warning": (t.amber_bg, t.amber),
            "danger": (t.danger_bg, t.danger_txt),
        }
        bg, fg = colors.get(severity, (t.bg3, t.txt3))
        pill = QLabel(text)
        pill.setStyleSheet(
            f"font-size:10px;padding:2px 8px;border-radius:10px;"
            f"background:{bg};color:{fg};font-weight:500;")
        return pill

    def _stat_label(self, value: str, label: str,
                    color: str = None) -> QWidget:
        t = get_theme()
        c = color or t.txt
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(2)
        val = QLabel(value)
        val.setStyleSheet(
            f"font-size:20px;font-weight:700;color:{c};")
        val.setAlignment(Qt.AlignCenter)
        lo.addWidget(val)
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"font-size:10px;color:{t.txt3};")
        lbl.setAlignment(Qt.AlignCenter)
        lo.addWidget(lbl)
        return w

    def _tag_label(self, text: str, color: str) -> QLabel:
        tag = QLabel(text)
        tag.setStyleSheet(
            f"font-size:9px;font-weight:600;padding:1px 6px;"
            f"border-radius:3px;background:{color}20;color:{color};")
        return tag
