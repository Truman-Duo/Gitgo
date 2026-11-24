"""Native entry contracts and owned fixture files, not desktop visual QA."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import shlex
import subprocess
import struct

import pytest

from backend.core import native_entry as entry
from scripts.render_product_manifest import render_manifest
from scripts.stage_native_entry import stage_entry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["windows", "macos", "linux"])
def native_payload(request, tmp_path_factory):
    platform = request.param
    root = tmp_path_factory / "installation"
    root.mkdir()
    home = tmp_path_factory / "user"
    home.mkdir()
    manifest = render_manifest(ROOT / "packaging/product.json", platform,
        {"macos": "arm64", "windows": "x86_64", "linux": "x86_64"}[platform])
    (root / "product.json").write_text(json.dumps(manifest), encoding="utf-8")
    shutil.copytree(ROOT / "assets/brand/generated", root / "assets/brand")
    (root / ("gitgo.exe" if platform == "windows" else "gitgo")).write_text("fixture-only")
    if platform == "macos":
        # A format prefix is enough to test the bundle template. This is
        # deliberately not evidence of a compiled, signed or runnable Mach-O.
        launcher = tmp_path_factory / "fixture-mach-o"
        launcher.write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture-only")
        stage_entry(root, launcher)
    else:
        stage_entry(root)
    if platform == "macos" and os.name == "nt":
        pytest.skip("macOS ownership links need POSIX symlink support; bundle template tested separately")
    environment = {"LOCALAPPDATA": str(home / "AppData/Local"), "APPDATA": str(home / "AppData/Roaming"),
                   "XDG_DATA_HOME": str(home / "data")}
    def shortcut(path, target, arguments, icon):
        path.write_text(json.dumps({"target":str(target),"arguments":arguments,"icon":str(icon)}))
    return root, home, environment, shortcut


def install(fixture):
    root, home, environment, shortcut = fixture
    return entry.install_entries(root, home=home, environment=environment, shortcut_writer=shortcut)


def test_native_entry_install_is_idempotent_and_stays_bound_to_stable_resources_after_payload_replacement(native_payload):
    root, home, environment, _ = native_payload
    result = install(native_payload)
    assert result["state"] == "ready" and result["entries"]
    record_path = root / entry.AREA / "owner.json"
    before = record_path.read_bytes()
    for name in result["entries"]:
        assert Path(name).is_relative_to(home)
        assert Path(name).exists()
    shutil.rmtree(root / "assets")
    assert install(native_payload) == result
    assert record_path.read_bytes() == before
    record = json.loads(before)
    assert record["source_sha256"] == entry.LOCKED_SOURCE
    if record["platform"] == "linux":
        text = Path(result["entries"][0]).read_text()
        values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        assert values["Terminal"] == "true"
        assert shlex.split(values["Exec"].replace("\\\\", "\\")) == [str(root / "gitgo")]
        assert Path(values["Icon"].replace("\\\\", "\\")).is_file()
    elif record["platform"] == "windows":
        profile = json.loads(Path(result["entries"][0]).read_text())
        assert str(root / "gitgo.exe") in profile["profiles"][0]["commandline"]
        assert Path(profile["profiles"][0]["icon"]).is_file()
        assert not (home / "AppData/Local/Microsoft/Windows Terminal/settings.json").exists()
    else:
        bundle = root / entry.AREA / "resources/Gitgo.app/Contents"
        info = plistlib.loads((bundle / "Info.plist").read_bytes())
        assert info["CFBundleIconFile"] == "gitgo.icns" and (bundle / "Resources/gitgo.icns").is_file()
        assert "exec" in (bundle.parent.parent / "gitgo").read_text()
    assert entry.remove_entries(root)["state"] == "removed"
    assert not record_path.exists()
    assert all(not Path(name).exists() for name in result["entries"])


def test_unknown_matching_filename_is_not_adopted_or_removed(native_payload):
    root, _, _, _ = native_payload
    result = install(native_payload)
    path = Path(result["entries"][0])
    link_target = path.readlink() if path.is_symlink() else None
    data = path.read_bytes() if link_target is None else None
    entry.remove_entries(root)
    if link_target is not None:
        path.symlink_to(link_target, target_is_directory=True)
    else:
        path.write_bytes(data)
    with pytest.raises(entry.NativeEntryError, match="UNOWNED_CONFLICT"):
        install(native_payload)
    assert path.is_symlink() if link_target else path.read_bytes() == data
    assert entry.remove_entries(root)["state"] == "absent"
    assert path.is_symlink() if link_target else path.read_bytes() == data


def test_desktop_shortcut_is_optional_owned_and_never_replaces_user_file(native_payload):
    root, home, environment, shortcut = native_payload
    if entry._context(root)[2] != "windows":
        pytest.skip("Windows shortcut option")
    desktop = home / "Desktop"
    desktop.mkdir()
    link = desktop / "Gitgo.lnk"
    assert len(install(native_payload)["entries"]) == 2
    assert not link.exists()
    link.write_bytes(b"existing shortcut")
    options = dict(home=home, environment=environment, shortcut_writer=shortcut, desktop_folder=desktop)
    assert entry.install_entries(root, **options)["desktop_shortcut"]["state"] == "conflict"
    assert link.read_bytes() == b"existing shortcut"
    link.unlink()
    result = entry.install_entries(root, **options)
    assert result["desktop_shortcut"] == {"state": "ready", "path": str(link)}
    assert link.is_file() and entry.install_entries(root, **options) == result
    assert entry.remove_entries(root)["state"] == "removed" and not link.exists()


def test_user_changes_block_cleanup_before_other_owned_files_are_deleted(native_payload):
    root, _, _, _ = native_payload
    result = install(native_payload)
    first = Path(result["entries"][0])
    if first.is_symlink():
        first.unlink(); first.write_text("user-owned replacement")
    else:
        first.write_text("user-owned replacement")
    with pytest.raises(entry.NativeEntryError, match="USER_FILE_CHANGED"):
        entry.remove_entries(root)
    assert first.read_text() == "user-owned replacement"
    assert all(Path(path).exists() for path in result["entries"])
    assert (root / entry.AREA / "owner.json").is_file()


def test_interrupted_publication_resumes_the_same_owned_entry_set(native_payload, monkeypatch):
    root, _, _, _ = native_payload
    publish = entry._publish
    calls = []
    def interrupted(resources, item):
        calls.append(item["path"])
        if len(calls) == 1:
            publish(resources, item)
            raise RuntimeError("simulated response loss after first publication")
        publish(resources, item)
    monkeypatch.setattr(entry, "_publish", interrupted)
    with pytest.raises(RuntimeError, match="response loss"):
        install(native_payload)
    record = json.loads((root / entry.AREA / "owner.json").read_text())
    assert record["state"] == "pending" and Path(calls[0]).exists()
    first = Path(calls[0]).lstat().st_mtime_ns
    monkeypatch.setattr(entry, "_publish", publish)
    result = install(native_payload)
    assert result["state"] == "ready" and Path(calls[0]).lstat().st_mtime_ns == first
    assert len(result["entries"]) == len(set(result["entries"]))


def test_linux_exec_uses_desktop_quoting_without_shell_expansion():
    encoded = entry._desktop_exec('/home/用户/space $ (x) "quote" \\ %f/gitgo')
    assert encoded.startswith('"') and encoded.endswith('"')
    assert "%%f" in encoded and "\\\\$" in encoded and '\\\\"quote\\\\"' in encoded


def test_installer_owned_windows_shortcut_does_not_create_a_second_start_menu_entry(native_payload):
    root, home, environment, shortcut = native_payload
    if json.loads((root / "product.json").read_text())["platform"] != "windows":
        pytest.skip("Windows Inno shortcut ownership")
    result = entry.install_entries(root, home=home, environment=environment,
                                   shortcut_writer=shortcut, shortcut_owner="installer")
    assert len(result["entries"]) == 1 and result["entries"][0].endswith(".json")
    assert not (root / entry.AREA / "resources/Gitgo.lnk").exists()
    assert entry.remove_entries(root)["state"] == "removed"


def test_macos_bundle_template_declares_the_locked_icon_and_uses_the_existing_sibling_cli(tmp_path_factory):
    stage = tmp_path_factory / "stage"
    stage.mkdir()
    product = render_manifest(ROOT / "packaging/product.json", "macos", "arm64")
    (stage / "product.json").write_text(json.dumps(product))
    shutil.copytree(ROOT / "assets/brand/generated", stage / "assets/brand")
    executable = tmp_path_factory / "fixture-mach-o"
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"template-test-only")
    stage_entry(stage, executable)
    bundle = stage / "Gitgo.app/Contents"
    info = plistlib.loads((bundle / "Info.plist").read_bytes())
    assert info["CFBundleIconFile"] == "gitgo.icns" and info["CFBundleExecutable"] == "Gitgo"
    assert (bundle / "Resources/gitgo.icns").read_bytes() == (stage / "assets/brand/gitgo.icns").read_bytes()
    assert 'exec "$HERE/gitgo" "$@"' in (bundle / "Resources/launch.command").read_text()


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell Link COM required")
def test_real_windows_shortcut_preserves_unicode_target_arguments_and_icon(tmp_path_factory):
    directory = tmp_path_factory / "图标 entry"
    directory.mkdir()
    shortcut = directory / "Gitgo.lnk"
    target = directory / "test e\u0301 𠮷 target.exe"
    target.write_bytes(b"fixture-only-not-executed")
    icon = ROOT / "assets/brand/generated/gitgo.ico"
    arguments = '--fixture "中文 e\u0301"'
    entry._shortcut(shortcut, target, arguments, icon)
    # Read the documented Unicode StringData independently. WScript's getter
    # also converts arguments through the ANSI code page and is not evidence
    # that the persisted Unicode shortcut lost characters.
    link_bytes = shortcut.read_bytes()
    flags = struct.unpack_from("<I", link_bytes, 20)[0]
    assert flags & 0x80
    offset = 76
    if flags & 1:
        offset += 2 + struct.unpack_from("<H", link_bytes, offset)[0]
    if flags & 2:
        info_size, header_size = struct.unpack_from("<II", link_bytes, offset)
        assert header_size >= 36
        local_unicode = struct.unpack_from("<I", link_bytes, offset + 28)[0]
        tail = link_bytes[offset + local_unicode:offset + info_size]
        observed_target = tail[:len(tail)//2*2].decode("utf-16-le").split("\0")[0]
        assert Path(observed_target) == target
        offset += info_size
    strings = {}
    for bit, name in ((4,"name"), (8,"relative"), (16,"working"), (32,"arguments"), (64,"icon")):
        if flags & bit:
            length = struct.unpack_from("<H", link_bytes, offset)[0]
            offset += 2
            strings[name] = link_bytes[offset:offset + length * 2].decode("utf-16-le")
            offset += length * 2
    assert strings["arguments"] == arguments
    assert strings["icon"] == str(icon)
