"""测试 SMBFileAdapter — UNC 路径构建 + 工厂接线"""

import pytest

from backend.adapters.file_adapter import FileAdapter
from backend.adapters.smb_file_adapter import SMBFileAdapter
from backend.adapters.factory import create_adapters_for_node
from backend.models import FileAccess, FileAccessKind, RepoNode


# ── UNC 路径构建 ────────────────────────────────────────

def test_unc_construction_basic():
    fa = SMBFileAdapter(host="192.168.1.100", share="projects")
    assert fa.unc_path == "\\\\192.168.1.100\\projects"


def test_unc_construction_with_subroot():
    fa = SMBFileAdapter(host="server", share="data", root="/sub/dir")
    assert "\\\\server\\data\\sub\\dir" in fa.unc_path.replace("/", "\\")


def test_unc_construction_no_subroot():
    fa = SMBFileAdapter(host="nas", share="backup", root="")
    assert fa.unc_path == "\\\\nas\\backup"


def test_unc_construction_windows_slashes():
    fa = SMBFileAdapter(host="10.0.0.5", share="share", root="a\\b")
    assert "\\\\10.0.0.5\\share\\a\\b" == fa.unc_path.replace("/", "\\")


# ── 路径解析 ──────────────────────────────────────────

def test_resolve_relative():
    fa = SMBFileAdapter(host="srv", share="s")
    p = fa._resolve("foo/bar.txt")
    assert str(p).startswith("\\\\srv\\s")
    assert "foo\\bar.txt" in str(p)


def test_resolve_empty():
    fa = SMBFileAdapter(host="srv", share="s", root="subdir")
    p = fa._resolve("")
    assert str(p).endswith("subdir") or str(p).endswith("subdir\\")


# ── FileAdapter 接口合规 ──────────────────────────────

def test_is_file_adapter_subclass():
    assert issubclass(SMBFileAdapter, FileAdapter)


def test_can_instantiate():
    fa = SMBFileAdapter(host="localhost", share="test")
    assert fa is not None
    assert isinstance(fa, FileAdapter)


# ── 工厂接线 ──────────────────────────────────────────

def test_factory_creates_smb_adapter():
    node = RepoNode(
        file_access=FileAccess(
            kind=FileAccessKind.SMB,
            host="10.0.0.1",
            share="repo",
            path="/work",
            port=445,
        ),
    )
    fa, gr = create_adapters_for_node(node)
    assert isinstance(fa, SMBFileAdapter)
    assert "\\\\10.0.0.1\\repo\\work" in fa.unc_path.replace("/", "\\")


def test_factory_smb_uses_local_git_runner():
    node = RepoNode(
        file_access=FileAccess(
            kind=FileAccessKind.SMB,
            host="nas",
            share="git",
            path="/repo",
        ),
    )
    from backend.adapters.local_git_runner import LocalGitRunner
    fa, gr = create_adapters_for_node(node)
    assert isinstance(gr, LocalGitRunner)


def test_factory_smb_no_share_uses_host():
    """当 share 为空时回退到 host 作为共享名"""
    node = RepoNode(
        file_access=FileAccess(
            kind=FileAccessKind.SMB,
            host="fileserver",
            share="",
            path="/code",
        ),
    )
    fa, gr = create_adapters_for_node(node)
    assert "\\\\fileserver\\fileserver" in fa.unc_path.replace("/", "\\")


# ── 默认端口 ──────────────────────────────────────────

def test_default_port_is_445():
    fa = SMBFileAdapter(host="h", share="s")
    assert fa._port == 445


def test_custom_port():
    fa = SMBFileAdapter(host="h", share="s", port=1445)
    assert fa._port == 1445
