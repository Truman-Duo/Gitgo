"""项目编辑对话框 — 添加/编辑项目配置"""
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)
from typing import Optional
from backend.core.config import ProjectConfig
from backend.models import FileAccess, FileAccessKind, RepoNode
from backend.core.i18n import _tr


class _ProjectEditDialog(QDialog):
    """添加/编辑项目的对话框"""

    def __init__(self, parent=None, project: Optional[ProjectConfig] = None, existing_names: list[str] = None):
        super().__init__(parent)
        self._existing_names = existing_names or []
        self.setWindowTitle(_tr("dialog.edit_project", "编辑项目") if project else _tr("dialog.add_project", "添加项目"))
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(_tr("project.name_placeholder", "例如: MyProject"))
        form.addRow(_tr("project.name", "项目名:"), self.name_edit)

        # 每个节点：类型选择 + 本地/SSH 输入组
        ws_data = self._add_node_section(
            form,
            _tr("project.workspace", "工作区(workspace node):"),
            _tr("project.workspace_placeholder", "工作区目录路径"),
        )
        (self.ws_type, self.ws_edit, self._ws_local, self._ws_ssh,
         self.ws_host, self.ws_port, self.ws_user, self.ws_key) = ws_data

        bk_data = self._add_node_section(
            form,
            _tr("project.backup", "发布备份区(release backup node):"),
            _tr("project.backup_placeholder", "备份仓库路径"),
        )
        (self.bk_type, self.bk_edit, self._bk_local, self._bk_ssh,
         self.bk_host, self.bk_port, self.bk_user, self.bk_key) = bk_data

        tl_data = self._add_node_section(
            form,
            _tr("project.trial", "试验区(trial node):"),
            _tr("project.trial_placeholder", "Trial 仓库路径（可选）"),
        )
        (self.tl_type, self.tl_edit, self._tl_local, self._tl_ssh,
         self.tl_host, self.tl_port, self.tl_user, self.tl_key) = tl_data

        # 备注（可选）
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText(_tr("project.note_placeholder", "备注（可选）"))
        form.addRow(_tr("project.note", "备注:"), self.note_edit)

        layout.addLayout(form)
        layout.addSpacing(10)

        btn_row = QHBoxLayout()
        self.ok_btn = QPushButton(_tr("settings.ok", "确认"))
        self.ok_btn.clicked.connect(self._on_ok)
        cancel_btn = QPushButton(_tr("settings.cancel", "取消"))
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self.ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        if project:
            self.name_edit.setText(project.name)
            self._fill_node(project.workspace, self.ws_type,
                            self.ws_edit, self.ws_host, self.ws_port,
                            self.ws_user, self.ws_key)
            self._fill_node(project.release, self.bk_type,
                            self.bk_edit, self.bk_host, self.bk_port,
                            self.bk_user, self.bk_key)
            if project.trial:
                self._fill_node(project.trial, self.tl_type,
                                self.tl_edit, self.tl_host, self.tl_port,
                                self.tl_user, self.tl_key)
            self.note_edit.setText(project.note)
            self._original = project
        else:
            self._original = None

    def _add_node_section(self, form: QFormLayout, label: str, placeholder: str):
        """为单个 RepoNode 创建类型选择 + 本地/SSH 双输入组"""
        # 类型选择
        type_combo = QComboBox()
        type_combo.addItem(_tr("node.type_local", "本地路径"), "local")
        type_combo.addItem(_tr("node.type_ssh", "SSH 远程"), "ssh")
        form.addRow(label, type_combo)

        # 本地路径组
        local_group = QWidget()
        local_layout = QHBoxLayout(local_group)
        local_layout.setContentsMargins(0, 0, 0, 0)
        path_edit = QLineEdit()
        path_edit.setPlaceholderText(placeholder)
        browse_btn = QPushButton(_tr("config.browse", "浏览..."))
        browse_btn.clicked.connect(lambda: self._browse(path_edit))
        local_layout.addWidget(path_edit)
        local_layout.addWidget(browse_btn)
        form.addRow("", local_group)

        # SSH 远程组
        ssh_group = QWidget()
        ssh_layout = QFormLayout(ssh_group)
        ssh_layout.setContentsMargins(0, 0, 0, 0)
        host_edit = QLineEdit()
        port_edit = QLineEdit("22")
        port_edit.setMaximumWidth(60)
        user_edit = QLineEdit()
        key_edit = QLineEdit()
        key_browse = QPushButton(_tr("config.browse", "浏览..."))
        key_browse.clicked.connect(lambda: self._browse_file(key_edit))
        key_row = QHBoxLayout()
        key_row.addWidget(key_edit)
        key_row.addWidget(key_browse)
        ssh_layout.addRow(_tr("node.ssh_host", "主机:"), host_edit)
        ssh_layout.addRow(_tr("node.ssh_port", "端口:"), port_edit)
        ssh_layout.addRow(_tr("node.ssh_user", "用户:"), user_edit)
        ssh_layout.addRow(_tr("node.ssh_key", "密钥路径:"), key_row)
        ssh_group.setVisible(False)
        form.addRow("", ssh_group)

        # 切换逻辑
        def _on_type_changed(idx):
            is_local = type_combo.currentData() == "local"
            local_group.setVisible(is_local)
            ssh_group.setVisible(not is_local)
        type_combo.currentIndexChanged.connect(_on_type_changed)

        return (type_combo, path_edit, local_group, ssh_group,
                host_edit, port_edit, user_edit, key_edit)

    def _browse(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, _tr("dialog.select_dir", "选择目录"))
        if d:
            edit.setText(d)

    def _browse_file(self, edit: QLineEdit):
        f, _ = QFileDialog.getOpenFileName(self, _tr("dialog.select_dir", "选择文件"))
        if f:
            edit.setText(f)

    @staticmethod
    def _fill_node(node, type_combo, path_edit, host_edit, port_edit, user_edit, key_edit):
        """回填节点数据到对话框"""
        fa = node.file_access
        is_ssh = fa.kind == FileAccessKind.SSH
        type_combo.setCurrentIndex(1 if is_ssh else 0)
        path_edit.setText(fa.path)
        if is_ssh:
            host_edit.setText(fa.host)
            port_edit.setText(str(fa.port))
            user_edit.setText(fa.username)
            key_edit.setText(fa.key_path)

    def _on_ok(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, _tr("dialog.hint", "提示"), _tr("project.name_empty", "项目名不能为空"))
            return
        # 检查名称唯一性
        if self._original:
            existing = [n for n in self._existing_names if n != self._original.name]
        else:
            existing = self._existing_names
        if name in existing:
            QMessageBox.warning(self, _tr("dialog.hint", "提示"), _tr("project.name_exists", "该项目名已存在"))
            return
        ws = self.ws_edit.text().strip()
        bk = self.bk_edit.text().strip()
        if ws and bk and ws == bk:
            QMessageBox.warning(self, _tr("dialog.hint", "提示"), _tr("project.same_path", "工作区路径与备份路径不能相同"))
            return
        self.accept()

    def get_project(self) -> ProjectConfig:
        trial_path = self.tl_edit.text().strip()
        pc = ProjectConfig(
            name=self.name_edit.text().strip(),
            note=self.note_edit.text().strip(),
            workspace=RepoNode(file_access=self._build_file_access(
                self.ws_type, self.ws_edit, self.ws_host, self.ws_port,
                self.ws_user, self.ws_key)),
            release=RepoNode(file_access=self._build_file_access(
                self.bk_type, self.bk_edit, self.bk_host, self.bk_port,
                self.bk_user, self.bk_key)),
            trial=RepoNode(file_access=self._build_file_access(
                self.tl_type, self.tl_edit, self.tl_host, self.tl_port,
                self.tl_user, self.tl_key)) if trial_path or self.tl_type.currentData() == "ssh" else None,
        )
        # 保留原有项目的其他设置（commit_format, force_exclude, sync_base）
        if self._original:
            pc.commit_format = self._original.commit_format
            pc.force_exclude = self._original.force_exclude
            pc.sync_base = self._original.sync_base
            if self._original.trial and pc.trial:
                pc.trial.remote = self._original.trial.remote
                pc.trial.last_known_head = self._original.trial.last_known_head
        return pc

    @staticmethod
    def _build_file_access(type_combo, path_edit, host_edit, port_edit,
                           user_edit, key_edit) -> FileAccess:
        is_ssh = type_combo.currentData() == "ssh"
        return FileAccess(
            kind=FileAccessKind.SSH if is_ssh else FileAccessKind.LOCAL,
            path=path_edit.text().strip(),
            host=host_edit.text().strip() if is_ssh else "",
            port=int(port_edit.text() or 22) if is_ssh else 22,
            username=user_edit.text().strip() if is_ssh else "",
            key_path=key_edit.text().strip() if is_ssh else "",
        )
