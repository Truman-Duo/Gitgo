"""RemotesMixin — 远程卡片详情 / 独立 Push / 状态检测"""
from PySide6.QtCore import QThread
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)
from backend.core.i18n import _tr
from themes import get_theme
from ..workers import RemoteStatusWorker


class RemotesMixin:
    """远程仓库卡片 — 状态展示 + 独立 Push + 刷新"""

    def _populate_remotes(self):
        # 清理旧线程
        if hasattr(self.state, '_remote_data'):
            for url, data in self.state._remote_data.items():
                if "thread" in data and data["thread"].isRunning():
                    data["thread"].quit()
                    data["thread"].wait(500)
        self._clear_box_layout(self.state.remotes_layout)
        self.state._remote_data = {}  # remote_url -> status info

        t = get_theme()
        for node, node_name in [(self.state.project.release, "release"),
                                (self.state.project.trial, "trial")]:
            if not node or not node.remote or not node.remote.url:
                continue
            rt = node.remote
            url = rt.url
            is_trial = node_name == "trial"
            dot_color = "#c98b2a" if is_trial else "#3b6d11"

            card = QFrame()
            card.setStyleSheet(
                f"QFrame{{background:{t.bg};border:.5px solid {t.bdr};"
                f"border-radius:8px;}}")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(0)

            # ── 顶栏: dot + name + kind + url ──
            hdr = QHBoxLayout()
            hdr.setContentsMargins(12, 9, 12, 9)
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{dot_color};font-size:7px;")
            hdr.addWidget(dot)
            hdr.addWidget(QLabel(f"<b>{rt.name}</b>"))
            hdr.addWidget(QLabel(
                f'<span style="font-size:11px;color:{t.txt3}">{rt.kind or "remote"}</span>'))
            hdr.addStretch()
            hdr.addWidget(QLabel(
                f'<span style="font-size:11px;color:{t.txt2}">{url}</span>'))
            cl.addLayout(hdr)

            # ── 详情行: branch / ahead-behind / last push ──
            detail = QLabel(
                _tr("remote.checking", "  正在检查状态..."))
            detail.setStyleSheet(
                f"font-size:10px;color:{t.txt3};padding:2px 12px 6px;")
            cl.addWidget(detail)

            # ── 底栏: 状态指示 + Push + Refresh ──
            foot = QHBoxLayout()
            foot.setContentsMargins(12, 4, 12, 9)

            status_dot = QLabel("●")
            status_dot.setStyleSheet(f"color:{t.txt3};font-size:7px;")
            foot.addWidget(status_dot)
            status_label = QLabel(
                _tr("remote.checking_short", "检查中"))
            status_label.setStyleSheet(f"font-size:10px;color:{t.txt3};")
            foot.addWidget(status_label)

            foot.addStretch()

            push_btn = QPushButton(_tr("remote.push", "↑ Push"))
            push_btn.setProperty("variant", "secondary")
            push_btn.setEnabled(False)
            push_btn.clicked.connect(
                lambda checked, u=url: self._on_remote_push(u))
            foot.addWidget(push_btn)

            refresh_btn = QPushButton(_tr("remote.refresh", "↻"))
            refresh_btn.setProperty("variant", "ghost")
            refresh_btn.clicked.connect(
                lambda checked, u=url, d=detail, sd=status_dot,
                       sl=status_label, pb=push_btn:
                self._check_remote_status(u, d, sd, sl, pb))
            foot.addWidget(refresh_btn)

            cl.addLayout(foot)

            # ── 存引用供后续更新 ──
            if url not in self.state._remote_data:
                self.state._remote_data[url] = {}
            self.state._remote_data[url].update({
                "card": card, "detail": detail,
                "status_dot": status_dot, "status_label": status_label,
                "push_btn": push_btn, "node": node, "node_name": node_name,
            })

            self.state.remotes_layout.insertWidget(
                self.state.remotes_layout.count() - 1, card)

        # ── 启动异步状态检查 ──
        for url, data in self.state._remote_data.items():
            self._check_remote_status(url, data["detail"],
                                      data["status_dot"],
                                      data["status_label"],
                                      data["push_btn"])

    def _check_remote_status(self, url: str, detail_label: QLabel,
                             status_dot: QLabel, status_label: QLabel,
                             push_btn: QPushButton):
        data = self.state._remote_data.get(url)
        if not data:
            return
        node = data["node"]
        node_name = data["node_name"]

        git_runner = None
        if node_name == "release":
            git_runner = getattr(self.state.session, "bk_git_runner", None)
        elif node_name == "trial":
            git_runner = getattr(self.state.session, "trial_git_runner", None)
        if not git_runner:
            status_label.setText(_tr("remote.no_runner", "无 git runner"))
            return

        detail_label.setText(_tr("remote.checking", "  正在检查状态..."))
        status_dot.setStyleSheet("color:#888780;font-size:7px;")
        status_label.setText(_tr("remote.checking_short", "检查中"))
        push_btn.setEnabled(False)

        remote_name = node.remote.name if node.remote else "origin"
        worker = RemoteStatusWorker(git_runner, url, remote_name)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda info: self._on_remote_status(
            info, detail_label, status_dot, status_label, push_btn))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.state._remote_data[url]["thread"] = thread
        thread.start()

    def _on_remote_status(self, info: dict, detail_label: QLabel,
                          status_dot: QLabel, status_label: QLabel,
                          push_btn: QPushButton):
        t = get_theme()
        branch = info.get("branch", "?")
        ahead = info.get("ahead", 0)
        behind = info.get("behind", 0)
        last_push = info.get("last_push", "")
        reachable = info.get("reachable", False)
        err = info.get("error", "")

        if reachable:
            parts = []
            parts.append(
                _tr("remote.branch", "Branch: {b}").format(b=branch))
            parts.append(
                _tr("remote.ahead_behind", "Ahead: {a}  Behind: {be}").format(
                    a=ahead, be=behind))
            if last_push:
                parts.append(
                    _tr("remote.last_push", "Last push: {t}").format(t=last_push))
            detail_label.setText("  " + "  |  ".join(parts))
            detail_label.setStyleSheet(
                f"font-size:10px;color:{t.txt2};padding:2px 12px 6px;")
            status_dot.setStyleSheet(f"color:{t.success_txt};font-size:7px;")
            status_label.setText(_tr("remote.reachable", "Reachable"))
            status_label.setStyleSheet(f"font-size:10px;color:{t.success_txt};")
            push_btn.setEnabled(True)
        else:
            detail_label.setText(
                _tr("remote.unreachable_detail",
                    "  Unreachable: {e}").format(e=err or "connection failed"))
            detail_label.setStyleSheet(
                f"font-size:10px;color:{t.txt3};padding:2px 12px 6px;")
            status_dot.setStyleSheet(f"color:{t.danger_txt};font-size:7px;")
            status_label.setText(_tr("remote.unreachable", "Unreachable"))
            status_label.setStyleSheet(f"font-size:10px;color:{t.danger_txt};")
            push_btn.setEnabled(False)

    def _on_remote_push(self, url: str):
        data = self.state._remote_data.get(url)
        if not data:
            return
        node_name = data["node_name"]

        reply = QMessageBox.question(
            self,
            _tr("remote.push_confirm_title", "确认 Push"),
            _tr("remote.push_confirm_msg",
                "推送到远程仓库 {url}？").format(url=url),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        git_runner = None
        remote_name = "origin"
        if node_name == "release":
            git_runner = getattr(self.state.session, "bk_git_runner", None)
        elif node_name == "trial":
            git_runner = getattr(self.state.session, "trial_git_runner", None)
        if not git_runner:
            QMessageBox.warning(
                self, _tr("dialog.hint", "提示"),
                _tr("remote.no_git", "无法获取 git runner"))
            return

        self._log(_tr("remote.pushing", "正在推送到 {url}...").format(url=url))
        try:
            from backend.core.operations import push_to_backup
            success, warnings = push_to_backup(
                remote=remote_name,
                progress_callback=lambda c, t, m: self._log(m),
                git_runner=git_runner,
            )
            if success:
                QMessageBox.information(
                    self, _tr("exec.push_success", "Push 成功"),
                    _tr("remote.push_ok", "已推送到 {url}").format(url=url))
                self._log(_tr("remote.push_done", "Push 完成 → {url}").format(url=url))
                # 刷新状态
                self._check_remote_status(url, data["detail"],
                                          data["status_dot"],
                                          data["status_label"],
                                          data["push_btn"])
            else:
                self._log(_tr("remote.push_fail", "Push 失败 → {url}").format(url=url))
        except Exception as e:
            QMessageBox.critical(
                self, _tr("exec.push_failed", "Push 失败"),
                str(e))
