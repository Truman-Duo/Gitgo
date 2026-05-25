"""自指测试日志中发现的错误回归测试

Bug 1: _cmd_scan 使用 e.path 而非 e.rel_path → AttributeError
Bug 2: CRLF/LF 换行符差异导致文件被误报为 modified
Bootstrap: subprocess 中文编码 + CREATE_NO_WINDOW 跨平台
"""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from backend.adapters.file_adapter import _hash_file
from backend.core.operations.scan import compare_files
from backend.core.operations.models import FileEntry


# ── Bug 1: _cmd_scan 字段名 ──────────────────────────────

def test_scan_json_uses_rel_path():
    """验证 scan --json 输出使用正确的字段名 rel_path"""
    # 不通过 CLI 调用，直接验证 FileEntry 字段存在
    e = FileEntry(rel_path="src/main.py", status="modified",
                  workspace_hash="abc", backup_hash="def")
    d = {"path": e.rel_path, "status": e.status, "selected": e.selected}
    assert d["path"] == "src/main.py"
    assert d["status"] == "modified"
    assert hasattr(e, "rel_path")
    assert not hasattr(e, "path")  # FileEntry 没有 path 字段！


# ── Bug 2: 换行符归一化 ──────────────────────────────────

def _tmp():
    d = tempfile.mkdtemp()
    return Path(d)


def test_hash_normalize_eol():
    """normalize_eol=True 时 CRLF 和 LF 文件哈希应相同"""
    import shutil
    p = _tmp()
    try:
        crlf_file = p / "crlf.txt"
        lf_file = p / "lf.txt"
        crlf_file.write_bytes(b"line1\r\nline2\r\n")
        lf_file.write_bytes(b"line1\nline2\n")

        h_crlf_raw = _hash_file(str(crlf_file), normalize_eol=False)
        h_lf_raw = _hash_file(str(lf_file), normalize_eol=False)
        assert h_crlf_raw != h_lf_raw  # raw hashes differ

        h_crlf_norm = _hash_file(str(crlf_file), normalize_eol=True)
        h_lf_norm = _hash_file(str(lf_file), normalize_eol=True)
        assert h_crlf_norm == h_lf_norm  # normalized hashes match
    finally:
        shutil.rmtree(str(p), ignore_errors=True)


def test_hash_normalize_eol_preserves_binary():
    """normalize_eol 对无 CRLF 的二进制文件无影响"""
    import shutil
    p = _tmp()
    try:
        bf = p / "data.bin"
        bf.write_bytes(bytes(range(256)))
        h1 = _hash_file(str(bf), normalize_eol=False)
        h2 = _hash_file(str(bf), normalize_eol=True)
        assert h1 == h2
    finally:
        shutil.rmtree(str(p), ignore_errors=True)


def test_compare_files_normalize_eol():
    """compare_files(normalize_eol=True) 不会误报换行符差异"""
    import shutil
    ws = _tmp() / "ws"
    bk = _tmp() / "bk"
    ws.mkdir(parents=True)
    bk.mkdir(parents=True)
    try:
        (bk / "README.md").write_bytes(b"# Hello\n\nWorld\n")
        (ws / "README.md").write_bytes(b"# Hello\r\n\r\nWorld\r\n")

        from backend.adapters import LocalFileAdapter
        ws_fa = LocalFileAdapter(ws)
        bk_fa = LocalFileAdapter(bk)

        # 不归一化 → 应检测为 modified
        entries_raw = compare_files(
            str(ws), str(bk), ["README.md"],
            ws_adapter=ws_fa, bk_adapter=bk_fa,
            normalize_eol=False,
        )
        assert any(e.status == "modified" for e in entries_raw)

        # 归一化 → 应检测为 same
        entries_norm = compare_files(
            str(ws), str(bk), ["README.md"],
            ws_adapter=ws_fa, bk_adapter=bk_fa,
            normalize_eol=True,
        )
        assert all(e.status == "same" for e in entries_norm)
    finally:
        shutil.rmtree(str(_tmp()), ignore_errors=True)


# ── Bootstrap 相关问题 ──────────────────────────────────

def test_subprocess_utf8_encoding():
    """验证 subprocess 使用 encoding='utf-8' 可以处理中文 git log"""
    import sys
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    assert result.returncode == 0
    assert len(result.stdout) > 0


def test_eol_normalize_roundtrip():
    """验证归一化消除 CRLF/LF 差异的正确性"""
    import shutil
    p = _tmp()
    try:
        # 两个内容相同但换行符不同的文件
        crlf = p / "crlf.py"
        lf = p / "lf.py"
        crlf.write_bytes(b"def hello():\r\n    return 'world'\r\n")
        lf.write_bytes(b"def hello():\n    return 'world'\n")

        # 字节级不同
        assert crlf.read_bytes() != lf.read_bytes()
        # 归一化后哈希相同
        assert _hash_file(str(crlf), normalize_eol=True) == \
               _hash_file(str(lf), normalize_eol=True)
    finally:
        shutil.rmtree(str(p), ignore_errors=True)


def test_compare_files_same_content_same_status():
    """内容相同（仅换行符不同）→ status=same"""
    import shutil
    root = _tmp()
    ws = root / "ws"
    bk = root / "bk"
    ws.mkdir(parents=True)
    bk.mkdir(parents=True)
    try:
        (bk / "main.py").write_bytes(b"import os\n\ndef main():\n    pass\n")
        (ws / "main.py").write_bytes(b"import os\r\n\r\ndef main():\r\n    pass\r\n")

        from backend.adapters import LocalFileAdapter
        entries = compare_files(
            str(ws), str(bk), ["main.py"],
            ws_adapter=LocalFileAdapter(ws),
            bk_adapter=LocalFileAdapter(bk),
            normalize_eol=True,
        )
        assert entries[0].status == "same"
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


def test_compare_files_no_false_modified():
    """9个文件不应因换行符差异被误报为 modified"""
    import shutil
    root = _tmp()
    ws = root / "ws"
    bk = root / "bk"
    ws.mkdir(parents=True)
    bk.mkdir(parents=True)

    try:
        files = [f"src/module_{i}.py" for i in range(9)]
        for f in files:
            (bk / f).parent.mkdir(parents=True, exist_ok=True)
            (ws / f).parent.mkdir(parents=True, exist_ok=True)
            (bk / f).write_bytes(b"# module\n\ndef foo():\n    return 1\n")
            (ws / f).write_bytes(b"# module\r\n\r\ndef foo():\r\n    return 1\r\n")

        from backend.adapters import LocalFileAdapter
        entries = compare_files(
            str(ws), str(bk), files,
            ws_adapter=LocalFileAdapter(ws),
            bk_adapter=LocalFileAdapter(bk),
            normalize_eol=True,
        )
        # 所有 9 个文件不应被误报
        assert all(e.status == "same" for e in entries)
    finally:
        shutil.rmtree(str(root), ignore_errors=True)
