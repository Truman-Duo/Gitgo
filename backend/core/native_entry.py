"""Installer-owned native entry resources. Never touches terminal settings.json.

Resources live beside the stable installation entry, outside the replaceable
managed payload. External files have a write-ahead ownership receipt; changed
or unowned files are never adopted or removed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

LOCKED_SOURCE = "ed24abd2ea22f97c2c3964dd0103be41006b80e9d3deaf2fcf11a5ee93b72d9a"
AREA = "native-entry"


class NativeEntryError(RuntimeError):
    pass


def _safe(path):
    path = Path(path).absolute()
    if any(char in str(path) for char in "\0\r\n\t") or ".." in path.parts:
        raise NativeEntryError("NATIVE_ENTRY_PATH_INVALID")
    for part in (path, *path.parents):
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise NativeEntryError("NATIVE_ENTRY_REDIRECTED")
    return path


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(path):
    _safe(path)
    if path.stat().st_size > 256 * 1024:
        raise NativeEntryError("NATIVE_ENTRY_RECORD_INVALID")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_record(path, value):
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(data); output.flush(); os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(root):
    result = []
    for path in sorted(root.rglob("*")):
        _safe(path)
        if path.is_file():
            result.append({"path": path.relative_to(root).as_posix(), "sha256": _hash(path.read_bytes())})
    return result


def _brand(source, destination):
    manifest = _json(source / "manifest.json")
    if manifest.get("source", {}).get("sha256") != LOCKED_SOURCE or len(manifest.get("files", [])) != 12:
        raise NativeEntryError("NATIVE_ENTRY_ICON_SOURCE_CHANGED")
    destination.mkdir(parents=True)
    for item in manifest["files"]:
        name = item["name"]
        if Path(name).name != name or not name.startswith("gitgo"):
            raise NativeEntryError("NATIVE_ENTRY_ICON_INVENTORY_INVALID")
        path = _safe(source / name)
        data = path.read_bytes()
        if _hash(data) != item["sha256"] or len(data) != item["bytes"]:
            raise NativeEntryError("NATIVE_ENTRY_ICON_CHANGED")
        (destination / name).write_bytes(data)
    shutil.copyfile(source / "manifest.json", destination / "manifest.json")


def _desktop_exec(argument):
    # Desktop Entry string unescaping precedes Exec double-quote unescaping.
    quoted = '"' + argument.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace(chr(96), "\\" + chr(96)) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def _shortcut(path, target, arguments, icon):
    from backend.core.windows_shell_link import create_shortcut
    create_shortcut(path, target, arguments, icon)


def _context(root):
    root = _safe(root).resolve(strict=True)
    managed = (root / "control/registration.json").is_file()
    if managed:
        from backend.core.update.installation import open_managed
        store = open_managed(root)
        product = _json(root / "active/product.json")
        if product["product_id"] != store.registration["product_id"]:
            raise NativeEntryError("NATIVE_ENTRY_INSTALLATION_CHANGED")
        argv = [str(root / "bootstrap/gitgo-host" / ("gitgo-host.exe" if os.name == "nt" else "gitgo-host")),
                "--gitgo-internal-role", "managed-launch"]
        payload = root / "active"
    else:
        product = _json(root / "product.json")
        payload = root
        argv = [str(root / ("gitgo.exe" if product.get("platform") == "windows" else "gitgo"))]
    if product.get("product_id") != "gitgo-terminal" or product.get("primary_command") != "gitgo":
        raise NativeEntryError("NATIVE_ENTRY_PRODUCT_INVALID")
    return root, payload, product["platform"], argv


def _matches(entry):
    path = Path(entry["path"])
    _safe(path.parent)
    if entry["kind"] == "link":
        return path.is_symlink() and str(path.readlink()) == entry["target"]
    _safe(path)
    return path.is_file() and _hash(path.read_bytes()) == entry["sha256"]


def _validate(root, record):
    if (record.get("schema") != 1 or record.get("root") != str(root)
            or record.get("source_sha256") != LOCKED_SOURCE or record.get("state") not in {"pending", "ready"}):
        raise NativeEntryError("NATIVE_ENTRY_OWNERSHIP_INVALID")
    resources = root / AREA / "resources"
    if _inventory(resources) != record["resources"]:
        raise NativeEntryError("NATIVE_ENTRY_RESOURCES_CHANGED")
    token = _hash(str(root).encode())[:12]
    platform = record.get("platform")
    installer_shortcut = platform == "windows" and record.get("shortcut_owner") == "installer"
    expected_sources = ({"gitgo.desktop", *("brand/gitgo-" + str(size) + ".png"
        for size in (16, 20, 24, 32, 48, 64, 128, 256, 512, 1024))} if platform == "linux"
        else {"terminal-profile.json"} if installer_shortcut
        else {"terminal-profile.json", "Gitgo.lnk"} if platform == "windows" else set())
    if (len(record["entries"]) != {"linux": 11, "windows": 1 if installer_shortcut else 2, "macos": 1}.get(platform)
            or len({entry["path"] for entry in record["entries"]}) != len(record["entries"])
            or {entry.get("source") for entry in record["entries"] if entry["kind"] == "file"} != expected_sources):
        raise NativeEntryError("NATIVE_ENTRY_OWNERSHIP_INVALID")
    for entry in record["entries"]:
        path = Path(entry["path"])
        if not path.is_absolute() or ".." in path.parts:
            raise NativeEntryError("NATIVE_ENTRY_OWNERSHIP_INVALID")
        if entry["kind"] == "link":
            if (platform != "macos" or path.name != "Gitgo-" + token + ".app" or path.parent.name != "Applications"
                    or entry["target"] != str(resources / "Gitgo.app")):
                raise NativeEntryError("NATIVE_ENTRY_OWNERSHIP_INVALID")
        elif entry["kind"] == "file":
            source = entry["source"]
            if platform == "linux":
                expected = (("applications", "gitgo-" + token + ".desktop") if source == "gitgo.desktop"
                    else ("icons", "hicolor", Path(source).stem.split("-")[-1] + "x" + Path(source).stem.split("-")[-1], "apps", "gitgo-" + token + ".png"))
            else:
                expected = (("Microsoft", "Windows Terminal", "Fragments", "Gitgo", token + ".json") if source == "terminal-profile.json"
                    else ("Microsoft", "Windows", "Start Menu", "Programs", "Gitgo-" + token + ".lnk"))
            if tuple(path.parts[-len(expected):]) != expected:
                raise NativeEntryError("NATIVE_ENTRY_OWNERSHIP_INVALID")
        else:
            raise NativeEntryError("NATIVE_ENTRY_OWNERSHIP_INVALID")
        if path.exists() or path.is_symlink():
            if not _matches(entry):
                raise NativeEntryError("NATIVE_ENTRY_USER_FILE_CHANGED")
        if entry["kind"] == "file":
            source = _safe(resources / entry["source"])
            if not source.is_relative_to(resources) or _hash(source.read_bytes()) != entry["sha256"]:
                raise NativeEntryError("NATIVE_ENTRY_OWNERSHIP_INVALID")


def _publish(resources, entry):
    path = Path(entry["path"])
    _safe(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path.parent)
    if path.exists() or path.is_symlink():
        if not _matches(entry):
            raise NativeEntryError("NATIVE_ENTRY_UNOWNED_CONFLICT")
        return
    if entry["kind"] == "link":
        path.symlink_to(entry["target"], target_is_directory=True)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write((resources / entry["source"]).read_bytes()); output.flush(); os.fsync(output.fileno())
    try:
        temporary.chmod(0o644)
        # No replacement race: publish a completed file only if the name is free.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_entries(root, *, home=None, environment=None, shortcut_writer=None, shortcut_owner="native"):
    root, payload, platform, argv = _context(Path(root))
    area = root / AREA
    record_path = area / "owner.json"
    if record_path.exists():
        record = _json(record_path); _validate(root, record)
        if record["platform"] != platform:
            raise NativeEntryError("NATIVE_ENTRY_INSTALLATION_CHANGED")
        if record.get("shortcut_owner", "native") != shortcut_owner:
            raise NativeEntryError("NATIVE_ENTRY_SHORTCUT_OWNER_CHANGED")
    else:
        if area.exists():
            raise NativeEntryError("NATIVE_ENTRY_UNOWNED_CONFLICT")
        home = _safe(Path.home() if home is None else Path(home))
        environment = os.environ if environment is None else environment
        token = _hash(str(root).encode())[:12]
        area.mkdir(mode=0o700)
        resources = area / "resources"
        resources.mkdir()
        entries = []
        def file_entry(path, source):
            entries.append({"kind": "file", "path": str(path), "source": source,
                            "sha256": _hash((resources / source).read_bytes())})
        try:
            _brand(payload / "assets/brand", resources / "brand")
            if platform == "linux":
                data = _safe(Path(environment.get("XDG_DATA_HOME") or home / ".local/share"))
                # '=' is forbidden in an Exec executable name. A constant
                # shell program can pass such a path as a quoted argument.
                command = (["/bin/sh", "-c", 'exec "$0"', str(root / "gitgo")] if "=" in str(root)
                           else [str(root / "gitgo")])
                desktop = ("[Desktop Entry]\nType=Application\nName=Gitgo\nComment=Gitgo Dashboard\n"
                           "Exec=" + " ".join(_desktop_exec(arg) for arg in command) + "\n"
                           "Icon=" + str(resources / "brand/gitgo-256.png").replace("\\", "\\\\") + "\n"
                           "Terminal=true\nCategories=Development;\n")
                (resources / "gitgo.desktop").write_text(desktop, encoding="utf-8")
                file_entry(data / "applications" / ("gitgo-" + token + ".desktop"), "gitgo.desktop")
                for size in (16, 20, 24, 32, 48, 64, 128, 256, 512, 1024):
                    file_entry(data / "icons/hicolor" / (str(size) + "x" + str(size)) / "apps" / ("gitgo-" + token + ".png"),
                               "brand/gitgo-" + str(size) + ".png")
            elif platform == "macos":
                bundle = _safe(payload / "Gitgo.app")
                if not bundle.is_dir():
                    raise NativeEntryError("NATIVE_ENTRY_MACOS_BUNDLE_MISSING")
                _inventory(bundle)  # refuse redirected bundle resources
                shutil.copytree(bundle, resources / "Gitgo.app")
                # The identical bundle opens its sibling command; this shim
                # remains bound to the stable CLI through upgrades/rollback.
                (resources / "gitgo").write_text('#!/bin/sh\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"\nexec "$HERE/../../gitgo" "$@"\n')
                (resources / "gitgo").chmod(0o755)
                entries.append({"kind": "link", "path": str(home / "Applications" / ("Gitgo-" + token + ".app")),
                                "target": str(resources / "Gitgo.app")})
            elif platform == "windows":
                local = _safe(Path(environment.get("LOCALAPPDATA") or home / "AppData/Local"))
                roaming = _safe(Path(environment.get("APPDATA") or home / "AppData/Roaming"))
                guid = "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "gitgo:" + str(root))) + "}"
                profile = {"profiles": [{"guid": guid, "name": "Gitgo", "commandline": subprocess.list2cmdline(argv),
                                        "icon": str(resources / "brand/gitgo.ico"), "startingDirectory": str(home)}]}
                (resources / "terminal-profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                file_entry(local / "Microsoft/Windows Terminal/Fragments/Gitgo" / (token + ".json"), "terminal-profile.json")
                if shortcut_owner != "installer":
                    (shortcut_writer or _shortcut)(resources / "Gitgo.lnk", argv[0],
                        subprocess.list2cmdline(argv[1:]), resources / "brand/gitgo.ico")
                    file_entry(roaming / "Microsoft/Windows/Start Menu/Programs" / ("Gitgo-" + token + ".lnk"), "Gitgo.lnk")
            else:
                raise NativeEntryError("NATIVE_ENTRY_PLATFORM_UNSUPPORTED")
            for entry in entries:
                path = Path(entry["path"]); _safe(path.parent)
                if path.exists() or path.is_symlink():
                    raise NativeEntryError("NATIVE_ENTRY_UNOWNED_CONFLICT")
            record = {"schema": 1, "root": str(root), "platform": platform, "state": "pending", "shortcut_owner": shortcut_owner,
                      "source_sha256": LOCKED_SOURCE, "resources": _inventory(resources), "entries": entries}
            _write_record(record_path, record)
        except BaseException:
            # No public entries precede the ownership receipt.
            _safe(area)
            if area.resolve() != root / AREA or area.parent.resolve() != root:
                raise NativeEntryError("NATIVE_ENTRY_REDIRECTED")
            shutil.rmtree(area)
            raise
    _validate(root, record)
    for entry in record["entries"]:
        _publish(area / "resources", entry)
    record["state"] = "ready"
    _write_record(record_path, record)
    return {"state": "ready", "entries": [entry["path"] for entry in record["entries"]]}


def remove_entries(root, *, check_only=False):
    root = _safe(Path(root)).resolve(strict=True)
    record_path = root / AREA / "owner.json"
    if not record_path.exists():
        return {"state": "absent"}
    _context(root)
    record = _json(record_path); _validate(root, record)
    if check_only:
        return {"state": record["state"]}
    for entry in record["entries"]:
        path = Path(entry["path"])
        if path.exists() or path.is_symlink():
            if not _matches(entry):
                raise NativeEntryError("NATIVE_ENTRY_USER_FILE_CHANGED")
            path.unlink()
    # The inventory was verified before deletion; remove exactly that owned
    # tree, preserving any unknown/changed resource instead of guessing.
    for item in reversed(record["resources"]):
        path = root / AREA / "resources" / item["path"]
        if _hash(path.read_bytes()) != item["sha256"]:
            raise NativeEntryError("NATIVE_ENTRY_RESOURCES_CHANGED")
        path.unlink()
    for path in sorted((root / AREA / "resources").rglob("*"), key=lambda value: len(value.parts), reverse=True):
        path.rmdir()
    (root / AREA / "resources").rmdir()
    record_path.unlink()
    (root / AREA).rmdir()
    return {"state": "removed"}


def main(arguments):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--action", choices=("install", "check", "remove"), required=True)
    parser.add_argument("--shortcut-owner", choices=("native", "installer"), default="native")
    args = parser.parse_args(arguments)
    _root, _payload, platform, _argv = _context(args.root)
    if platform != ("windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"):
        raise NativeEntryError("NATIVE_ENTRY_PLATFORM_MISMATCH")
    result = install_entries(args.root, shortcut_owner=args.shortcut_owner) if args.action == "install" else remove_entries(args.root, check_only=args.action == "check")
    print(json.dumps(result, ensure_ascii=True))
    return 0
