import zipfile
from backend.core.tools.document_tools import document_open
from backend.core.tools.document_limits import verified_antiword, bounded_join, DocumentLimit
from backend.core.tools.file_compat import file_write
import hashlib
from backend.core.tools.execution_profile import safe_environment
import pytest


def test_zip_expansion_is_rejected_before_parser(tmp_path_factory):
    path = tmp_path_factory / "bomb.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 2_000_000)
    result = document_open({"_workspace": str(tmp_path_factory), "path": path.name})
    assert result["error_info"]["details"]["code"] == "DOCUMENT_RESOURCE_LIMIT"


def test_converter_name_is_not_identity(tmp_path_factory, monkeypatch):
    fake = tmp_path_factory / "antiword.exe"; fake.write_text("fake", encoding="utf-8")
    monkeypatch.setattr("backend.core.tools.document_limits.shutil.which", lambda name: str(fake))
    monkeypatch.delenv("GITGO_ANTIWORD_SHA256", raising=False)
    with pytest.raises(ValueError, match="CONVERTER_IDENTITY_UNVERIFIED"): verified_antiword()


def test_join_stops_unbounded_generator():
    with pytest.raises(DocumentLimit): bounded_join(("x" * 10_000 for _ in range(1000)))


def test_legacy_append_retains_previous_content(tmp_path_factory):
    path = tmp_path_factory / "log.txt"
    path.write_text("first\n", encoding="utf-8")
    result = file_write({"_workspace": str(tmp_path_factory), "path": str(path), "content": "second\n", "mode": "a",
                         "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    assert "error" not in result and path.read_text(encoding="utf-8") == "first\nsecond\n"


def test_all_execution_paths_share_secret_and_injection_filter(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test")
    monkeypatch.setenv("NODE_OPTIONS", "--require untrusted")
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    env = safe_environment()
    assert "DEEPSEEK_API_KEY" not in env and "NODE_OPTIONS" not in env and "PYTHONPATH" not in env
