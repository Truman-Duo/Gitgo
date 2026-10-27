"""Deterministic, bounded release packages from immutable Git objects.

The verifier accepts an independently supplied SHA-256. A manifest checksum is
integrity evidence, not a signature or permission to install/run its contents.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile

from .git import OID
from .state import PublishError, encoded

MAX_PACKAGE = 272 * 1024 * 1024
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_ARTIFACTS = 8
MAX_BUILD_TOTAL = 128 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z")


def version_name(value):
    if not isinstance(value, str) or len(value) > 80 or not _VERSION.fullmatch(value):
        raise PublishError("PUBLISH_INVALID_VERSION")
    prerelease = value.split("+", 1)[0].split("-", 1)
    if len(prerelease) == 2 and any(x.isdigit() and len(x) > 1 and x.startswith("0") for x in prerelease[1].split(".")):
        raise PublishError("PUBLISH_INVALID_VERSION")
    return value


def portable_name(value):
    from backend.core.sandbox_read import relative_parts
    if not isinstance(value, str) or len(value) > 1024 or "\\" in value:
        raise PublishError("PUBLISH_PACKAGE_INVALID")
    relative_parts(value)
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{kind}{n}" for kind in ("COM", "LPT") for n in "123456789¹²³")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part.endswith((" ", ".")) or part.split(".")[0].upper() in reserved
                or any(ord(c) < 32 or c in ':*?"<>|' for c in part)):
            raise PublishError("PUBLISH_PACKAGE_INVALID")
    return value


def recipe(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"entry", "outputs", "timeout"}:
        raise PublishError("PUBLISH_INVALID_BUILD")
    entry, outputs, timeout = value.get("entry"), value.get("outputs"), value.get("timeout", 120)
    portable_name(entry)
    if not entry.endswith(".py") or any(p.casefold() in {".git", ".gitgo"} for p in entry.split("/")):
        raise PublishError("PUBLISH_INVALID_BUILD")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= MAX_ARTIFACTS or type(timeout) is not int or not 1 <= timeout <= 120:
        raise PublishError("PUBLISH_INVALID_BUILD")
    names = set()
    for name in outputs:
        portable_name(name)
        if not name.startswith("dist/") or name.casefold() in names or any(p.casefold() in {".git", ".gitgo"} for p in name.split("/")):
            raise PublishError("PUBLISH_INVALID_BUILD")
        names.add(name.casefold())
    return {"entry": entry, "outputs": sorted(outputs), "timeout": timeout}


def _object(kind, raw, algorithm):
    return hashlib.new(algorithm, f"{kind} {len(raw)}\0".encode() + raw).hexdigest()


def _tree(rows, algorithm):
    root = {}
    for row in rows:
        current = root
        parts = row["path"].split("/")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
            if not isinstance(current, dict):
                raise PublishError("PUBLISH_PACKAGE_INVALID")
        if parts[-1] in current:
            raise PublishError("PUBLISH_PACKAGE_INVALID")
        current[parts[-1]] = (row["mode"], row["blob_oid"])
    def finish(node):
        raw = bytearray()
        for name, value in sorted(node.items(), key=lambda row: (row[0] + ("/" if isinstance(row[1], dict) else "")).encode("utf8")):
            mode, oid = ("40000", finish(value)) if isinstance(value, dict) else value
            raw.extend(mode.encode() + b" " + name.encode("utf8") + b"\0" + bytes.fromhex(oid))
        return _object("tree", bytes(raw), algorithm)
    return finish(root)


def package_bytes(git, fixed_oid, version, evidence, *, artifacts=(), build_recipe=None, environment=None):
    from .formal import tree_files
    version = version_name(version)
    if not isinstance(fixed_oid, str) or not OID.fullmatch(fixed_oid):
        raise PublishError("PUBLISH_SOURCE_UNAVAILABLE")
    tree_oid = git.oid(fixed_oid, "tree")
    files, payloads, sources = tree_files(git, tree_oid), {}, []
    for name, mode, blob, raw in files:
        portable_name(name)
        sources.append({"path": name, "mode": mode, "blob_oid": blob, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        payloads["source/" + name] = raw
    assets, total = [], 0
    if len(artifacts) > MAX_ARTIFACTS:
        raise PublishError("PUBLISH_BUILD_LIMIT")
    build_recipe = recipe(build_recipe)
    if bool(artifacts) != bool(build_recipe):
        raise PublishError("PUBLISH_BUILD_RESULT_INVALID")
    for name, raw in sorted(artifacts):
        portable_name(name)
        if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT or name not in build_recipe["outputs"]:
            raise PublishError("PUBLISH_BUILD_RESULT_INVALID")
        total += len(raw)
        if total > MAX_BUILD_TOTAL or "artifacts/" + name in payloads:
            raise PublishError("PUBLISH_BUILD_LIMIT")
        assets.append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        payloads["artifacts/" + name] = raw
    if build_recipe and [a["path"] for a in assets] != build_recipe["outputs"]:
        raise PublishError("PUBLISH_BUILD_RESULT_INVALID")
    manifest = {"schema": "gitgo.release.v1", "version": version, "source_oid": fixed_oid,
        "tree_oid": tree_oid, "files": sources, "artifacts": assets, "checks": evidence,
        "build": {"recipe": build_recipe, "environment": environment} if build_recipe else None}
    from .environment import dependencies
    manifest["dependencies"] = dependencies(files)
    payloads["manifest.json"] = encoded(manifest)
    if len(payloads["manifest.json"]) > 2 * 1024 * 1024:
        raise PublishError("PUBLISH_PACKAGE_LIMIT")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, raw in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = next((r["mode"] for r in sources if "source/" + r["path"] == name), "100644")
            info.external_attr = (int(mode, 8) << 16)
            archive.writestr(info, raw)
    result = output.getvalue()
    if len(result) > MAX_PACKAGE:
        raise PublishError("PUBLISH_PACKAGE_LIMIT")
    validate_package(result, hashlib.sha256(result).hexdigest(), expected_source_oid=fixed_oid, expected_tree_oid=tree_oid)
    return result, manifest


def validate_package(raw, expected_sha256, *, expected_source_oid=None, expected_tree_oid=None):
    """Validate every byte and Git tree without extracting or executing code."""
    if (not isinstance(raw, bytes) or len(raw) > MAX_PACKAGE or not isinstance(expected_sha256, str)
            or not _HASH.fullmatch(expected_sha256) or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names, spellings, total = set(), {}, 0
            if len(entries) > 4096 + MAX_ARTIFACTS + 1 or archive.comment:
                raise ValueError("Package capacity")
            for entry in entries:
                portable_name(entry.filename)
                folded = entry.filename.casefold()
                if (folded in names or entry.compress_type != zipfile.ZIP_STORED or entry.is_dir() or entry.extra
                        or entry.flag_bits & 1 or entry.file_size > 16 * 1024 * 1024 and entry.filename != "manifest.json"
                        or stat.S_IFMT(entry.external_attr >> 16) != stat.S_IFREG):
                    raise ValueError("Unsupported package member")
                names.add(folded)
                parts = entry.filename.split("/")
                for index in range(1, len(parts) + 1):
                    name = "/".join(parts[:index])
                    key = name.casefold()
                    if key in spellings and spellings[key] != name:
                        raise ValueError("Nonportable case alias")
                    spellings[key] = name
                total += entry.file_size
                if total > MAX_PACKAGE:
                    raise ValueError("Package capacity")
            info = archive.getinfo("manifest.json")
            if info.file_size > 2 * 1024 * 1024:
                raise ValueError("Manifest capacity")
            body = archive.read(info)
            manifest = json.loads(body)
            if body != encoded(manifest) or manifest.get("schema") != "gitgo.release.v1":
                raise ValueError("Unknown/noncanonical manifest")
            version_name(manifest["version"])
            if not OID.fullmatch(manifest["source_oid"]) or not OID.fullmatch(manifest["tree_oid"]):
                raise ValueError("Invalid fixed source")
            if (expected_source_oid and manifest["source_oid"] != expected_source_oid
                    or expected_tree_oid and manifest["tree_oid"] != expected_tree_oid):
                raise ValueError("Source binding changed")
            algorithm = "sha1" if len(manifest["tree_oid"]) == 40 else "sha256"
            expected, source_names = {"manifest.json"}, set()
            for kind, prefix in (("files", "source/"), ("artifacts", "artifacts/")):
                rows = manifest[kind]
                used = 0
                if not isinstance(rows, list) or len(rows) > (4096 if kind == "files" else MAX_ARTIFACTS):
                    raise ValueError("Manifest capacity")
                for row in rows:
                    path = portable_name(row["path"])
                    name = prefix + path
                    if name in expected:
                        raise ValueError("Duplicate manifest entry")
                    expected.add(name)
                    content = archive.read(name)
                    used += len(content)
                    if (type(row["size"]) is not int or len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]
                            or used > 128 * 1024 * 1024 or len(content) > MAX_ARTIFACT):
                        raise ValueError("Member digest changed")
                    if kind == "files":
                        if (row["mode"] not in {"100644", "100755"} or _object("blob", content, algorithm) != row["blob_oid"]
                                or archive.getinfo(name).external_attr >> 16 != int(row["mode"], 8)):
                            raise ValueError("Git blob changed")
                        source_names.add(path)
            if expected != {entry.filename for entry in entries} or not manifest["files"] or _tree(manifest["files"], algorithm) != manifest["tree_oid"]:
                raise ValueError("Git tree changed")
            build = manifest["build"]
            if build:
                declared = recipe(build["recipe"])
                if declared["entry"] not in source_names or declared["outputs"] != [row["path"] for row in manifest["artifacts"]]:
                    raise ValueError("Build binding changed")
            elif manifest["artifacts"]:
                raise ValueError("Unbound artifact")
            return manifest
    except (KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, PublishError):
            raise
        raise PublishError("PUBLISH_PACKAGE_INVALID") from exc
