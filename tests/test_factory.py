"""测试适配器工厂 — create_adapters_for_node"""

from __future__ import annotations

from pathlib import Path

from backend.adapters.factory import create_adapters_for_node
from backend.adapters.local_file_adapter import LocalFileAdapter
from backend.adapters.local_git_runner import LocalGitRunner
from backend.models import FileAccess, FileAccessKind, RepoNode


class TestCreateAdaptersForNode:
    def test_local_node(self):
        node = RepoNode()
        node.file_access = FileAccess(kind=FileAccessKind.LOCAL, path="/tmp/test")
        fa, gr = create_adapters_for_node(node)
        assert isinstance(fa, LocalFileAdapter)
        assert isinstance(gr, LocalGitRunner)

    def test_ssh_node(self):
        node = RepoNode()
        node.file_access = FileAccess(
            kind=FileAccessKind.SSH,
            host="example.com",
            port=22,
            username="root",
            path="/remote/path",
        )
        fa, gr = create_adapters_for_node(node)
        from backend.adapters.ssh_file_adapter import SSHFileAdapter
        from backend.adapters.ssh_git_runner import SSHGitRunner
        assert isinstance(fa, SSHFileAdapter)
        assert isinstance(gr, SSHGitRunner)

    def test_node_empty_path_defaults_cwd(self):
        node = RepoNode()
        fa, gr = create_adapters_for_node(node)
        assert isinstance(fa, LocalFileAdapter)
        assert isinstance(gr, LocalGitRunner)
        assert str(gr._repo) == str(Path.cwd().resolve())
