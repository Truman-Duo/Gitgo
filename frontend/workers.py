"""Frontend workers — 后台线程"""
from PySide6.QtCore import QObject, Signal, Slot
from backend.core import SyncSession
from backend.models import TrialAction
from backend.core.i18n import _tr

class SyncWorker(QObject):
    """在后台线程执行同步操作"""

    progress = Signal(int, int, str)
    finished = Signal(bool, str)

    def __init__(self, session: SyncSession, formal_index: int):
        super().__init__()
        self.session = session
        self.formal_index = formal_index

    @Slot()
    def run(self):
        self.session.on_progress = lambda c, t, m: self.progress.emit(c, t, m)
        success = self.session.step_sync(formal_index=self.formal_index)
        self.finished.emit(success, _tr("exec.sync_success", "同步完成") if success else _tr("exec.sync_failed", "同步失败"))

class ScanWorker(QObject):
    """后台扫描工作线程"""

    progress = Signal(int, int, str)
    finished = Signal(list, str)

    def __init__(self, session: SyncSession):
        super().__init__()
        self.session = session

    @Slot()
    def run(self):
        self.session.on_progress = lambda c, t, m: self.progress.emit(c, t, m)
        entries = self.session.step_scan()

        if not entries:
            self.finished.emit([], _tr("scan.no_files", "工作区无文件"))
            return

        new = sum(1 for e in entries if e.status == "new")
        mod = sum(1 for e in entries if e.status == "modified")
        same = sum(1 for e in entries if e.status == "same")
        renamed = sum(1 for e in entries if e.status == "renamed")
        summary = _tr("scan.compare_complete", "对比完成: {n} 个文件变更").format(n=len(entries))
        summary += _tr("scan.compare_detail", "（{new} 新增, {mod} 修改, {same} 相同, {renamed} 重命名）").format(new=new, mod=mod, same=same, renamed=renamed)
        self.finished.emit(entries, summary)

class PushWorker(QObject):
    """后台 push 工作线程 — 统一走 step_push()"""

    progress = Signal(int, int, str)
    finished = Signal(bool, str)
    security_warning = Signal(list)  # 安全检查发现敏感信息时发射

    def __init__(self, session: SyncSession, skip_scan: bool = False):
        super().__init__()
        self.session = session
        self._skip_scan = skip_scan

    @Slot()
    def run(self):
        self.session.on_progress = lambda c, t, m: self.progress.emit(c, t, m)
        # worker 线程中永远不自动 force push —
        # 安全检查命中时返回 warnings 由 GUI 主线程处理确认
        self.session.on_security_warning = lambda w: False
        success, warnings = self.session.step_push(skip_scan=self._skip_scan)
        if not success and warnings:
            self.security_warning.emit(warnings)
        else:
            self.finished.emit(success, _tr("exec.push_success", "Push 成功") if success else _tr("exec.push_failed", "Push 失败"))

class TrialCheckWorker(QObject):
    """后台线程：检查 Trial 仓库"""

    finished = Signal(list, str)

    def __init__(self, session: SyncSession):
        super().__init__()
        self.session = session

    @Slot()
    def run(self):
        changes = self.session.step_check_trial()
        n = len(changes)
        pending = sum(1 for c in changes if c.triage == TrialAction.PENDING)
        summary = _tr("trial.found", "发现 {n} 个新 commit").format(n=n) if pending > 0 else _tr("trial.no_new", "无新 commit")
        self.finished.emit(changes, summary)

class RemoteStatusWorker(QObject):
    """后台获取远程仓库状态 — branch / ahead-behind / last push / reachable"""

    finished = Signal(dict)

    def __init__(self, git_runner, remote_url: str, remote_name: str = "origin"):
        super().__init__()
        self.runner = git_runner
        self.remote_url = remote_url
        self.remote_name = remote_name

    @Slot()
    def run(self):
        info = {"url": self.remote_url, "branch": "", "ahead": 0, "behind": 0,
                "last_push": "", "reachable": False, "error": ""}
        try:
            r = self.runner.run(["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
            if r.returncode == 0:
                info["branch"] = r.stdout.strip()

            r = self.runner.run(["ls-remote", self.remote_url, "HEAD"], timeout=30)
            if r.returncode != 0:
                info["error"] = r.stderr.strip()[:120]
            else:
                info["reachable"] = True

                if info["branch"]:
                    ref = f"{self.remote_name}/{info['branch']}"
                    r = self.runner.run(["rev-list", "--count", "--left-right",
                                         f"HEAD...{ref}"], timeout=15)
                    if r.returncode == 0:
                        parts = r.stdout.strip().split()
                        info["ahead"] = int(parts[0]) if len(parts) > 0 else 0
                        info["behind"] = int(parts[1]) if len(parts) > 1 else 0

                    r = self.runner.run(["log", "-1", "--format=%ci", ref], timeout=15)
                    if r.returncode == 0:
                        info["last_push"] = r.stdout.strip()[:19]
        except Exception as e:
            info["error"] = str(e)[:120]
            info["reachable"] = False
        self.finished.emit(info)


class TriageWorker(QObject):
    """后台线程：执行三叉决策"""

    finished = Signal(bool, str)

    def __init__(self, session: SyncSession, index: int, action: str):
        super().__init__()
        self.session = session
        self.index = index
        self.action = action

    @Slot()
    def run(self):
        success = self.session.step_triage_incoming(self.index, self.action)
        action_names = {
            "accept": _tr("trial.accept_done", "Accept 完成"),
            "promote": _tr("trial.promote_done", "Promote 完成"),
            "discard": _tr("trial.discard_done", "已忽略"),
        }
        msg = action_names.get(self.action, "") if success else _tr("trial.failed", "操作失败")
        self.finished.emit(success, msg)

