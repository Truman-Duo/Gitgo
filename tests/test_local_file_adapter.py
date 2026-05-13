"""测试 LocalFileAdapter — 本地文件系统实现"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.adapters.local_file_adapter import LocalFileAdapter


class TestLocalFileAdapter:
    """对 LocalFileAdapter 14 个 public 方法的全面测试。"""

    def test_exists(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        p = tmp_path_factory / "foo.txt"
        assert not file_adapter.exists("foo.txt")
        p.write_text("hello")
        assert file_adapter.exists("foo.txt")

    def test_is_file(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        (tmp_path_factory / "f").write_text("x")
        (tmp_path_factory / "d").mkdir()
        assert file_adapter.is_file("f")
        assert not file_adapter.is_file("d")

    def test_is_dir(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        (tmp_path_factory / "d").mkdir()
        (tmp_path_factory / "f").write_text("x")
        assert file_adapter.is_dir("d")
        assert not file_adapter.is_dir("f")

    def test_is_symlink(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        target = tmp_path_factory / "target"
        target.write_text("x")
        link = tmp_path_factory / "link"
        try:
            link.symlink_to("target")
        except (OSError, NotImplementedError):
            pytest.skip("No symlink permission on this system")
        assert file_adapter.is_symlink("link")

    def test_walk(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        (tmp_path_factory / "a").mkdir()
        (tmp_path_factory / "a" / "f1.txt").write_text("1")
        (tmp_path_factory / "b").mkdir()
        (tmp_path_factory / "b" / "f2.txt").write_text("2")

        results = list(file_adapter.walk())
        dirs_found = {r[0] for r in results}
        assert "." in dirs_found or "" in dirs_found
        # 确保所有文件都被遍历到
        all_files = set()
        for rel, dirs, files in results:
            for f in files:
                all_files.add(f"{rel}/{f}" if rel else f)
        assert "a/f1.txt" in all_files or "f1.txt" in all_files
        assert "b/f2.txt" in all_files or "f2.txt" in all_files

    def test_walk_top(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        (tmp_path_factory / "sub" / "nested").mkdir(parents=True)
        (tmp_path_factory / "sub" / "a.txt").write_text("x")
        (tmp_path_factory / "other.txt").write_text("x")

        results = list(file_adapter.walk("sub"))
        assert len(results) > 0
        # 不应当包含根目录下的文件
        paths = set()
        for rel, dirs, files in results:
            for f in files:
                paths.add(f"{rel}/{f}" if rel else f)
        assert "other.txt" not in paths

    def test_read_write_bytes(self, file_adapter: LocalFileAdapter):
        data = b"binary\x00data"
        file_adapter.write_bytes("out.bin", data)
        assert file_adapter.read_bytes("out.bin") == data

    def test_read_write_text(self, file_adapter: LocalFileAdapter):
        text = "你好，世界"
        file_adapter.write_text("hello.txt", text)
        assert file_adapter.read_text("hello.txt") == text

    def test_mkdir(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        file_adapter.mkdir("new_dir")
        assert (tmp_path_factory / "new_dir").is_dir()

    def test_mkdir_parents(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        file_adapter.mkdir("a/b/c", parents=True)
        assert (tmp_path_factory / "a" / "b" / "c").is_dir()

    def test_mkdir_exist_ok(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        (tmp_path_factory / "d").mkdir()
        file_adapter.mkdir("d", exist_ok=True)

    def test_hash_file(self, file_adapter: LocalFileAdapter):
        import hashlib
        data = b"content"
        file_adapter.write_bytes("f", data)
        expected = hashlib.sha256(data).hexdigest()
        assert file_adapter.hash_file("f") == expected

    def test_is_binary(self, file_adapter: LocalFileAdapter):
        file_adapter.write_bytes("text.txt", b"hello")
        file_adapter.write_bytes("bin.bin", b"hello\x00world")
        assert not file_adapter.is_binary("text.txt")
        assert file_adapter.is_binary("bin.bin")

    def test_copy_within(self, file_adapter: LocalFileAdapter):
        file_adapter.write_text("src.txt", "source content")
        file_adapter.copy_within("src.txt", "dst.txt")
        assert file_adapter.read_text("dst.txt") == "source content"

    def test_stat(self, file_adapter: LocalFileAdapter, tmp_path_factory: Path):
        (tmp_path_factory / "f").write_text("x")
        st = file_adapter.stat("f")
        assert st.st_size == 1

    def test_resolve_empty_path_is_root(self, tmp_path_factory: Path):
        # 空 path 应当解析为 root
        adapter = LocalFileAdapter(tmp_path_factory)
        assert adapter._resolve("") == tmp_path_factory.resolve()
