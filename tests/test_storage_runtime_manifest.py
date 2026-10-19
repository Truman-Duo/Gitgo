from dataclasses import replace
import hashlib
import json

import pytest

from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage.envelope import FileEnvelope, StorageEnvelope, StorageEnvelopeInvalid
from backend.core.storage.runtime_manifest import (
    MAX_MANIFEST_BYTES, RuntimeContent, RuntimeManifest, RuntimeManifestInvalid,
    RuntimeMember, canonical, with_runtime_copies,
)


def content():
    return RuntimeContent("cpython-314-win_amd64", "a"*64, (
        RuntimeMember("app.zip", 17, "b"*64), RuntimeMember("lib/module.py", 0, "c"*64),
        RuntimeMember("python.exe", 8193, "d"*64)))


def manifest():
    return RuntimeManifest(r"C:\installed\runtime", ObjectIdentity("win32", 17, 29), content())


def test_manifest_identity_and_location_are_distinct_from_content_contract():
    original = manifest()
    moved = replace(original, root=r"D:\other\runtime", root_identity=ObjectIdentity("win32", 31, 47))
    assert original.content.contract == moved.content.contract
    assert original.sha256 != moved.sha256
    assert RuntimeManifest.from_bytes(original.to_bytes(), original.sha256) == original
    changed = replace(original.content, storage_profile="e"*64)
    assert changed.contract != original.content.contract
    with pytest.raises(RuntimeManifestInvalid):
        RuntimeManifest.from_bytes(moved.to_bytes(), original.sha256)


@pytest.mark.parametrize("path", ["../code.py", "./code.py", "a//code.py", "/absolute", "A\\b",
    "NUL", "CON.py", "COM1", "code.py:stream", "trailing.", "trailing ", "a\0b",
    "runtime-manifest.json", "Runtime-Manifest.json/child", "a/../code.py"])
def test_manifest_rejects_alias_and_reserved_paths(path):
    with pytest.raises(RuntimeManifestInvalid):
        RuntimeMember(path, 4, "a"*64)


@pytest.mark.parametrize("length", [-1, True, 2**64, 1.5])
def test_manifest_refuses_wrapped_or_coerced_sizes(length):
    with pytest.raises(RuntimeManifestInvalid):
        RuntimeMember("member.py", length, "a"*64)


@pytest.mark.parametrize("additional", ["App.zip", "lib", "LIB/other.py", "python.exe/child"])
def test_file_directory_and_case_aliases_refuse_content(additional):
    original = content()
    files = tuple(sorted((*original.files, RuntimeMember(additional, 0, "f"*64)), key=lambda item:item.path))
    with pytest.raises(RuntimeManifestInvalid):
        replace(original, files=files)


@pytest.mark.parametrize("case", ["duplicate", "whitespace", "unknown", "version", "identity-bool", "length-bool", "nonfinite", "unsorted", "missing-entry"])
def test_even_matching_digest_does_not_admit_invalid_manifest(case):
    raw = manifest().to_bytes()
    body = json.loads(raw)
    if case == "duplicate":
        raw = raw[:-1]+b',"version":1}'
    elif case == "whitespace":
        raw = b" "+raw
    else:
        if case == "unknown": body["approval"] = True
        elif case == "version": body["version"] = True
        elif case == "identity-bool": body["root_identity"]["volume"] = True
        elif case == "length-bool": body["content"]["files"][0]["length"] = True
        elif case == "nonfinite": body["root_identity"]["volume"] = float("inf")
        elif case == "unsorted": body["content"]["files"].reverse()
        elif case == "missing-entry": body["content"]["files"].pop()
        raw = canonical(body)
    with pytest.raises(RuntimeManifestInvalid):
        RuntimeManifest.from_bytes(raw, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("root", [r"\\server\share\runtime", r"C:relative", "C:\\installed\\..\\runtime",
    "C:\\installed\\runtime\\", "C:/installed/runtime", "C:\\NUL\\runtime", "C:\\a\0b"])
def test_installation_location_has_no_path_fallback(root):
    with pytest.raises(RuntimeManifestInvalid):
        replace(manifest(), root=root)


def test_three_runtime_copies_join_the_existing_host_limit_without_refund():
    envelope = StorageEnvelope(1, 4096, 2, 1024*1024,
        (FileEnvelope("authority", 4096, 1),), (FileEnvelope("state", 8192, 1),))
    joined = with_runtime_copies(envelope, content())
    one_copy = 4096+4096+16384+MAX_MANIFEST_BYTES
    assert joined.reserved_max_bytes == envelope.reserved_max_bytes+3*one_copy
    assert joined.host_max_bytes == envelope.host_max_bytes and joined.files == envelope.files
    with pytest.raises(StorageEnvelopeInvalid):
        with_runtime_copies(joined, content())
    with pytest.raises(StorageEnvelopeInvalid):
        with_runtime_copies(replace(envelope, host_max_bytes=65536), content())


def test_all_files_are_rounded_individually_before_admission():
    original = content()
    # Raw bytes fit 64 MiB, but independently rounded retained copies do not.
    files = (RuntimeMember("app.zip", 33*1024*1024, "b"*64), RuntimeMember("python.exe", 1, "c"*64))
    with pytest.raises(RuntimeManifestInvalid):
        replace(original, files=files)


def test_directory_and_manifest_nodes_count_toward_native_capacity():
    files = tuple(sorted((RuntimeMember(f"d{i}/file", 0, "a"*64) for i in range(126)),key=lambda item:item.path))
    with pytest.raises(RuntimeManifestInvalid):
        replace(content(), files=tuple(sorted((*content().files, *files), key=lambda item:item.path)))
