"""Input and expansion limits apply before importing document parsers."""
from __future__ import annotations
import hashlib
import os
import shutil
import zipfile
from pathlib import Path

MAX_INPUT_BYTES = 16 * 1024**2
MAX_EXPANDED_BYTES = 64 * 1024**2
MAX_TEXT_CHARS = 2_000_000
MAX_ITEMS = 200_000


class DocumentLimit(ValueError):
    pass


def check_input(path):
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise DocumentLimit("document exceeds the 16MiB input boundary; split it before extraction")
    if path.suffix.lower() in {".docx", ".pptx", ".xlsx", ".xlsm"}:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 4096 or sum(i.file_size for i in members) > MAX_EXPANDED_BYTES:
                raise DocumentLimit("document archive exceeds the expansion boundary")
            if any(i.file_size > 1024**2 and i.file_size > max(1, i.compress_size) * 200 for i in members):
                raise DocumentLimit("document archive contains an excessive compression ratio")


def bounded_join(chunks, separator="\n"):
    retained = []; size = 0
    for index, chunk in enumerate(chunks):
        text = str(chunk)
        size += len(text) + len(separator)
        if index >= MAX_ITEMS or size > MAX_TEXT_CHARS:
            raise DocumentLimit("extracted document exceeds the text/item boundary; split or convert it")
        retained.append(text)
    return separator.join(retained)


def verified_antiword(workspace=None):
    candidate = shutil.which("antiword")
    if not candidate:
        raise ValueError("ANTIWORD_UNAVAILABLE: convert to docx or install a verified adapter")
    candidate = Path(candidate)
    if not candidate.is_absolute():
        raise ValueError("CONVERTER_IDENTITY_UNVERIFIED: adapter path must be absolute")
    candidate = candidate.resolve(strict=True)
    if workspace is not None and candidate.is_relative_to(Path(workspace).resolve(strict=True)):
        raise ValueError("CONVERTER_IDENTITY_UNVERIFIED: adapter cannot reside in the workspace")
    expected = str(os.environ.get("GITGO_ANTIWORD_SHA256") or "").lower()
    if len(expected) != 64 or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
        raise ValueError("CONVERTER_IDENTITY_UNVERIFIED: antiword requires a Host-configured SHA256 of a trusted adapter")
    return candidate, expected


def check_converter(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("CONVERTER_IDENTITY_CHANGED: converter replaced before execution")
