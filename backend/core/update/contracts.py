"""Fail-closed read-only candidate contract gate; never import candidate code.

The data-contract file is an updater proposal, NOT an existing shipping format.
Existing releases without it cannot pass this gate until publisher integration.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import stat

from .manifest import DataCompatibility, UpdateManifest
from .tree import inventory
from .validation import digest, fields, json_object, number, reject


def _metadata(root, name, records):
    item = records.get(name)
    if item is None or item["kind"] != "file" or item["bytes"] > 2 * 1024**2:
        reject("UPDATE_PACKAGE_CONTRACT", "Missing or oversized regular package metadata")
    if os.name == "nt":
        from .persistence import open_readonly
        fd = open_readonly(root / name)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(2 * 1024**2 + 1)
        if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            reject("UPDATE_PACKAGE_CONTRACT", "Windows package metadata changed during inspection")
        return json_object(raw, 2 * 1024**2)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != item["bytes"]:
                reject("UPDATE_PACKAGE_CONTRACT", "Package metadata changed during inspection")
            raw = handle.read(2 * 1024**2 + 1)
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            reject("UPDATE_PACKAGE_CONTRACT", "Package metadata changed during inspection")
        return json_object(raw, 2 * 1024**2)
    finally:
        os.close(directory)


def verify_bundle_contract(root: Path, manifest: UpdateManifest) -> None:
    records = {r["path"]: r for r in inventory(root)}
    if any(name in records for name in (".gitgo-install-owner", ".gitgo-path-bash", ".gitgo-path-zsh", ".gitgo-path-fish")):
        reject("UPDATE_PACKAGE_CONTRACT", "Release must not supply local installer ownership records")
    product = _metadata(root, "product.json", records)
    for key, expected in (("schema_version", 2), ("product_id", manifest.product_id),
                          ("version", manifest.version), ("platform", manifest.platform),
                          ("architecture", manifest.architecture), ("primary_command", "gitgo")):
        if type(product.get(key)) is not type(expected) or product[key] != expected:
            reject("UPDATE_PACKAGE_CONTRACT", "Product identity differs from the signed release")
    if "layouts" in product:
        reject("UPDATE_PACKAGE_CONTRACT", "Package product contract must be target-rendered")
    expected_layout = {"dashboard": "gitgo.exe", "native_host": "internal/gitgo-host/gitgo-host.exe",
                       "uninstaller": "uninstall.exe"} if manifest.platform == "windows" else {
        "dashboard": "gitgo", "native_host": "internal/gitgo-host/gitgo-host",
        "installer": "install.sh", "uninstaller": "uninstall.sh"}
    if "installation_mode" in product:
        if manifest.platform != "windows" or product["installation_mode"] != "managed-windows-v1":
            reject("UPDATE_PACKAGE_CONTRACT", "Unsupported installation mode")
        expected_layout = {"dashboard": "gitgo.exe", "native_host": "internal/gitgo-host/gitgo-host.exe",
                           "installer": "install-managed.ps1"}
    if product.get("layout") != expected_layout:
        reject("UPDATE_PACKAGE_CONTRACT", "Package component layout is unsupported")
    for name in expected_layout.values():
        if records.get(name, {}).get("kind") != "file":
            reject("UPDATE_PACKAGE_CONTRACT", "Required component is missing or redirected")
        if manifest.platform != "windows" and not records[name]["mode"] & 0o100:
            reject("UPDATE_PACKAGE_CONTRACT", "Unix component lacks owner execution permission")
    build = _metadata(root, "build-manifest.json", records)
    source = fields(build.get("source"), {"commit", "tree", "dirty"})
    if (type(build.get("schema_version")) is not int or build["schema_version"] != 1
            or build.get("version") != manifest.version
            or build.get("architecture") != manifest.architecture
            or build.get("platform") != ("win-x64" if manifest.platform == "windows" else manifest.platform)
            or source != {"commit": manifest.source_commit, "tree": manifest.source_tree, "dirty": False}
            or type(source["dirty"]) is not bool):
        reject("UPDATE_PACKAGE_CONTRACT", "Build identity differs from the signed release")
    expected_files = []
    for name, item in sorted(records.items()):
        if name == "build-manifest.json" or item["kind"] == "directory":
            continue
        if item["kind"] == "symlink":
            expected_files.append({"path": name, "link": item["link"]})
        else:
            expected_files.append({"path": name, "bytes": item["bytes"], "sha256": item["sha256"]})
    files = build.get("files")
    if type(files) is not list or len(files) > 10000:
        reject("UPDATE_PACKAGE_CONTRACT", "Build inventory must be a bounded list")
    for item in files:
        if type(item) is not dict:
            reject("UPDATE_PACKAGE_CONTRACT", "Invalid build inventory entry")
        if "link" in item:
            fields(item, {"path", "link"})
            if type(item["link"]) is not str:
                reject("UPDATE_PACKAGE_CONTRACT", "Invalid build link evidence")
        else:
            fields(item, {"path", "bytes", "sha256"})
            number(item["bytes"], minimum=0, maximum=2 * 1024**3)
            digest(item["sha256"])
        if type(item["path"]) is not str:
            reject("UPDATE_PACKAGE_CONTRACT", "Invalid build inventory path")
    # Publishers may use Path/component ordering rather than byte-string order.
    # Compare complete, typed inventories independent of serialization order.
    if sorted(files, key=lambda item: item["path"]) != expected_files:
        reject("UPDATE_PACKAGE_CONTRACT", "Package inventory does not match build evidence")
    contract = _metadata(root, "update-data-contract.json", records)
    fields(contract, {"schema_version", "data_compatibility", "state_migrations", "observability_migrations",
                      "config", "provider"})
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        reject("UPDATE_PACKAGE_CONTRACT", "Unsupported data evidence schema")
    compatibility = DataCompatibility.parse(contract["data_compatibility"])
    if compatibility != manifest.data_compatibility:
        reject("UPDATE_PACKAGE_CONTRACT", "Package data compatibility differs from the signed release")

    def verify_file(value, expected_hash):
        fields(value, {"path", "sha256"})
        path = value["path"]
        if type(path) is not str:
            reject("UPDATE_PACKAGE_CONTRACT", "Data fingerprint path is invalid")
        record = records.get(path)
        if (record is None or record["kind"] != "file" or record["sha256"] != digest(value["sha256"])
                or record["sha256"] != expected_hash):
            reject("UPDATE_PACKAGE_CONTRACT", "Actual data migration fingerprint differs")

    for name, history in (("state_migrations", compatibility.state_migrations),
                          ("observability_migrations", compatibility.observability_migrations)):
        items = contract[name]
        if type(items) is not list or len(items) != len(history):
            reject("UPDATE_PACKAGE_CONTRACT", "Package migration history is incomplete")
        for item, (version, sha) in zip(items, history):
            fields(item, {"version", "path", "sha256"})
            if number(item["version"], maximum=256) != version:
                reject("UPDATE_PACKAGE_CONTRACT", "Package migration order differs")
            verify_file({k: item[k] for k in ("path", "sha256")}, sha)
    for name, version, sha in (("config", compatibility.config_schema_version, compatibility.config_migration_sha256),
                               ("provider", compatibility.provider_schema_version, compatibility.provider_migration_sha256)):
        item = fields(contract[name], {"schema_version", "path", "sha256"})
        if number(item["schema_version"]) != version:
            reject("UPDATE_PACKAGE_CONTRACT", "Package configuration schema differs")
        verify_file({k: item[k] for k in ("path", "sha256")}, sha)
