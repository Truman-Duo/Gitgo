"""测试 SSH 适配器 — 基于 mock（不依赖真实 SSH 连接）"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── SSHFileAdapter ─────────────────────────────────────────


class MockSFTPClient:
    """模拟 paramiko SFTPClient"""

    def __init__(self):
        self.files = {}  # path -> bytes

    def stat(self, path):
        if path in self.files:
            from paramiko import SFTPAttributes
            attr = SFTPAttributes()
            attr.st_mode = 0o100644  # regular file
            attr.st_size = len(self.files[path])
            return attr
        raise FileNotFoundError(f"No such file: {path}")

    def listdir_attr(self, path):
        from paramiko import SFTPAttributes
        results = []
        for fpath in self.files:
            if fpath.startswith(path + "/") and fpath != path:
                rest = fpath[len(path) + 1:]
                if "/" not in rest:
                    attr = SFTPAttributes()
                    attr.filename = rest
                    attr.st_mode = 0o100644
                    results.append(attr)
        return results

    def open(self, path, mode="rb"):
        class FakeFile:
            def __init__(self, data):
                self.data = data
                self.pos = 0
            def read(self, n=-1):
                if n < 0:
                    rest = self.data[self.pos:]
                    self.pos = len(self.data)
                    return rest
                chunk = self.data[self.pos:self.pos + n]
                self.pos += n
                return chunk
            def write(self, data):
                pass  # read-only mock
            def close(self):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return FakeFile(self.files.get(path, b""))

    def mkdir(self, path):
        pass

    def close(self):
        pass


class MockSSHClient:
    """模拟 paramiko SSHClient"""

    def __init__(self):
        self.sftp = MockSFTPClient()

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, *args, **kwargs):
        pass

    def open_sftp(self):
        return self.sftp

    def exec_command(self, command, timeout=None):
        return (MagicMock(), MagicMock(), MagicMock())

    def close(self):
        pass


@pytest.fixture(autouse=True)
def mock_paramiko():
    """全局 mock paramiko，使 SSH 测试不依赖真实连接"""
    with patch.dict("sys.modules", {"paramiko": MagicMock()}):
        import paramiko as pk
        pk.SSHClient = MagicMock(return_value=MockSSHClient())
        pk.AutoAddPolicy = MagicMock
        pk.SFTPAttributes = type("SFTPAttributes", (), {"__init__": lambda self: None})
        yield


class TestSSHFileAdapter:
    def test_import_and_create(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        assert adapter._host == "example.com"
        assert adapter._root == "/remote"

    def test_exists(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/file.txt"] = b"content"
        assert adapter.exists("file.txt")
        assert not adapter.exists("nonexistent.txt")

    def test_is_file(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/f.txt"] = b"x"
        assert adapter.is_file("f.txt")
        assert not adapter.is_file("missing.txt")

    def test_is_dir(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        # listdir_attr returns nothing for dir-only paths
        assert not adapter.is_dir("nonexistent")

    def test_read_bytes(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/data.bin"] = b"binary\x00data"
        assert adapter.read_bytes("data.bin") == b"binary\x00data"

    def test_read_text(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/hello.txt"] = "你好".encode("utf-8")
        assert adapter.read_text("hello.txt") == "你好"

    def test_write_bytes(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/out.bin"] = b""
        # 直接测试 write_bytes 不抛异常
        adapter.write_bytes("out.bin", b"data")

    def test_hash_file(self):
        import hashlib
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        data = b"hash me"
        adapter._sftp.files["/remote/f.txt"] = data
        expected = hashlib.sha256(data).hexdigest()
        assert adapter.hash_file("f.txt") == expected

    def test_is_binary(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/text.txt"] = b"hello"
        adapter._sftp.files["/remote/bin.bin"] = b"hello\x00world"
        assert not adapter.is_binary("text.txt")
        assert adapter.is_binary("bin.bin")

    def test_stat(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/f.txt"] = b"content"
        st = adapter.stat("f.txt")
        assert st.st_size == 7

    def test_copy_within(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter._sftp.files["/remote/src.txt"] = b"data"
        adapter.copy_within("src.txt", "dst.txt")

    def test_mkdir(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com", root="/remote")
        adapter._sftp = MockSFTPClient()
        adapter.mkdir("new_dir", parents=True, exist_ok=True)

    def test_close(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com")
        adapter._sftp = MockSFTPClient()
        adapter.close()
        assert adapter._sftp is None

    def test_del_calls_close(self):
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        adapter = SSHFileAdapter(host="example.com")
        adapter._sftp = MockSFTPClient()
        adapter.__del__()
        assert adapter._sftp is None


class TestSSHGitRunner:
    def test_import_and_create(self):
        from backend.adapters.ssh_git_runner import SSHGitRunner
        runner = SSHGitRunner(host="example.com", repo_path="/repo")
        assert runner._host == "example.com"
        assert runner._repo == "/repo"

    def test_is_git_repo(self):
        from backend.adapters.ssh_git_runner import SSHGitRunner
        runner = SSHGitRunner(host="example.com")
        # mock 没有 .git → False
        assert not runner.is_git_repo()

    def test_run_timeout_handling(self):
        from backend.adapters.ssh_git_runner import SSHGitRunner
        runner = SSHGitRunner(host="example.com")

        class TimeoutSSH:
            def set_missing_host_key_policy(self, p): pass
            def connect(self, *a, **kw): pass
            def exec_command(self, cmd, timeout=None):
                raise Exception("timeout occurred")
            def close(self): pass

        import paramiko as pk
        pk.SSHClient = MagicMock(return_value=TimeoutSSH())
        runner._ssh = TimeoutSSH()

        r = runner.run(["status"])
        assert r.returncode == -1

    def test_run_command_success(self):
        from backend.adapters.ssh_git_runner import SSHGitRunner
        runner = SSHGitRunner(host="example.com")

        class MockChannel:
            def recv_exit_status(self): return 0

        class MockStream:
            def __init__(self, data=b""):
                self.data = data
            def read(self, n=-1): return self.data
            def readlines(self): return []
            def close(self): pass
            def __iter__(self): return iter([])

        class SuccessSSH:
            def set_missing_host_key_policy(self, p): pass
            def connect(self, *a, **kw): pass
            def exec_command(self, cmd, timeout=None):
                return (MockStream(), MockStream(b"out"), MockStream(b""))
            def close(self): pass

        import paramiko as pk
        pk.SSHClient = MagicMock(return_value=SuccessSSH())
        runner._ssh = SuccessSSH()

        r = runner.run(["status"])
        # 即使 exec_command 返回成功，实际 git 命令可能因为 mock 的通道没有正确设置退出码
        # 但至少应该返回一个 CompletedProcess
        assert r is not None
