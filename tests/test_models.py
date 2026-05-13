"""测试数据模型 — Phase 4 Trial 相关 + 基础模型"""

from __future__ import annotations

from backend.models import (
    FileAccess,
    FileAccessKind,
    IncomingChange,
    RemoteTarget,
    RepoNode,
    SyncStatus,
    TrialAction,
)


class TestTrialAction:
    def test_values(self):
        assert TrialAction.PENDING.value == "pending"
        assert TrialAction.ACCEPTED.value == "accepted"
        assert TrialAction.PROMOTED.value == "promoted"
        assert TrialAction.DISCARDED.value == "discarded"

    def test_default_order(self):
        members = list(TrialAction)
        assert members[0] == TrialAction.PENDING
        assert members[1] == TrialAction.ACCEPTED
        assert members[2] == TrialAction.PROMOTED
        assert members[3] == TrialAction.DISCARDED


class TestIncomingChange:
    def test_defaults(self):
        ic = IncomingChange()
        assert ic.hash == ""
        assert ic.message == ""
        assert ic.author == ""
        assert ic.timestamp == ""
        assert ic.triage == TrialAction.PENDING

    def test_full_init(self):
        ic = IncomingChange(
            hash="abc123",
            message="fix: bug",
            author="dev",
            timestamp="2025-01-01",
            body="details",
            triage=TrialAction.ACCEPTED,
        )
        assert ic.hash == "abc123"
        assert ic.message == "fix: bug"
        assert ic.triage == TrialAction.ACCEPTED


class TestFileAccess:
    def test_defaults(self):
        fa = FileAccess()
        assert fa.kind == FileAccessKind.LOCAL
        assert fa.path == ""
        assert fa.host == ""
        assert fa.port == 22
        assert fa.username == ""
        assert fa.key_path == ""

    def test_from_dict_local(self):
        d = {"kind": "local", "path": "/tmp/test"}
        fa = FileAccess.from_dict(d)
        assert fa.kind == FileAccessKind.LOCAL
        assert fa.path == "/tmp/test"

    def test_from_dict_ssh(self):
        d = {"kind": "ssh", "host": "example.com", "port": 2222, "username": "root", "key_path": "/keys/id_rsa"}
        fa = FileAccess.from_dict(d)
        assert fa.kind == FileAccessKind.SSH
        assert fa.host == "example.com"
        assert fa.port == 2222
        assert fa.username == "root"
        assert fa.key_path == "/keys/id_rsa"

    def test_from_dict_none(self):
        fa = FileAccess.from_dict(None)
        assert fa.kind == FileAccessKind.LOCAL

    def test_from_dict_invalid_kind_falls_back(self):
        d = {"kind": "ftp"}
        fa = FileAccess.from_dict(d)
        assert fa.kind == FileAccessKind.LOCAL


class TestRemoteTarget:
    def test_defaults(self):
        rt = RemoteTarget()
        assert rt.url == ""
        assert rt.name == "origin"

    def test_from_dict(self):
        rt = RemoteTarget.from_dict({"url": "git@github.com:user/repo.git", "name": "upstream"})
        assert rt.url == "git@github.com:user/repo.git"
        assert rt.name == "upstream"

    def test_from_dict_none(self):
        rt = RemoteTarget.from_dict(None)
        assert rt.url == ""
        assert rt.name == "origin"


class TestRepoNode:
    def test_defaults(self):
        rn = RepoNode()
        assert rn.file_access.kind == FileAccessKind.LOCAL
        assert rn.last_known_head == ""

    def test_from_dict_trial(self):
        d = {"file_access": {"kind": "local", "path": "/tmp/trial"}, "last_known_head": "def456"}
        rn = RepoNode.from_dict(d)
        assert rn is not None
        assert rn.file_access.path == "/tmp/trial"
        assert rn.last_known_head == "def456"

    def test_from_dict_none(self):
        assert RepoNode.from_dict(None) is None


class TestSyncStatus:
    def test_values(self):
        assert SyncStatus.MISSING.value == "missing"
        assert SyncStatus.EMPTY.value == "empty"
        assert SyncStatus.VALID.value == "valid"
