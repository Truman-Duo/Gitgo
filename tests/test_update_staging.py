"""Signed whole-package preparation, malicious archives and protected-data sentinels."""

from __future__ import annotations

from dataclasses import replace
from contextlib import nullcontext
from datetime import timedelta
import gzip
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile

import httpx
import pytest

from backend.core.update import ExtractionLimits, HttpsTransport, UpdateError, UpdatePolicy, prepare_update
from tests.test_update_discovery import CURRENT, NOW, POLICY
from tests.test_update_manifest import manifest_wire, signed, signing
from tests.test_update_transport import Stream


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Private staging requires the POSIX ownership adapter; Windows is fail-closed",
)


def tar_bytes(items=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, kind, data in items or [("bundle/gitgo", "file", b"executable-placeholder")]:
            member = tarfile.TarInfo(name)
            member.mode = 0o6755
            if kind == "file":
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            else:
                member.type = {"directory": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE,
                               "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE,
                               "device": tarfile.CHRTYPE}[kind]
                member.linkname = data
                archive.addfile(member)
    return gzip.compress(output.getvalue())


def zip_bytes(items=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode in items or [("gitgo.exe", b"executable-placeholder", stat.S_IFREG | 0o755)]:
            member = zipfile.ZipInfo(name)
            member.external_attr = mode << 16
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, data)
    return output.getvalue()


@pytest.fixture
def locations(tmp_path_factory):
    # This repository deliberately provides a Path under this fixture name.
    root = tmp_path_factory.resolve()
    parent = root / "staging"
    parent.mkdir(mode=0o700)
    protected = []
    for name in ("installed", "project", "config", "runtime"):
        path = root / name
        path.mkdir()
        protected.append(path)
        for file in ("sentinel", "sentinel-wal", "sentinel-shm"):
            (path / file).write_bytes((name + file).encode())
    return parent, tuple(protected)


def snapshot(paths):
    return {p: p.read_bytes() for root in paths for p in root.rglob("*") if p.is_file()}


def pipeline(signing, locations, body=None, *, wire_changes=None, delivered=None,
             current=CURRENT, policy=POLICY, handler=None, **options):
    body = tar_bytes() if body is None else body
    wire = manifest_wire(bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), **(wire_changes or {}))
    raw, signature = signed(wire, signing)
    calls = []

    def serve(request):
        url = str(request.url)
        calls.append(url)
        if url == policy.manifest_url:
            return httpx.Response(200, stream=Stream([raw]))
        if url == policy.signature_url:
            return httpx.Response(200, stream=Stream([signature]))
        if handler is not None:
            return handler(request)
        return httpx.Response(200, stream=Stream([delivered if delivered is not None else body]))

    before = snapshot(locations[1])
    try:
        result = prepare_update(policy, current, signing[1], staging_parent=locations[0],
                                protected_paths=locations[1], transport=HttpsTransport(
                                    policy.allowed_hosts, transport=httpx.MockTransport(serve)),
                                clock=options.pop("clock", lambda: NOW), **options)
        return result, calls
    finally:
        assert snapshot(locations[1]) == before


@pytest.mark.parametrize("platform,architecture,kind", [("macos", "arm64", "tar.gz"),
                                                       ("linux", "x86_64", "tar.gz"),
                                                       ("windows", "x86_64", "zip")])
def test_each_metadata_target_stages_without_executing_or_installing(signing, locations, platform, architecture, kind):
    body = zip_bytes() if kind == "zip" else tar_bytes()
    result, calls = pipeline(signing, locations, body, wire_changes={"platform": platform,
                             "architecture": architecture, "archive_format": kind},
                             current=replace(CURRENT, platform=platform, architecture=architecture))
    assert result.status == "staged"
    assert result.bundle_directory == result.staging_directory / "payload" / ("" if kind == "zip" else "bundle")
    assert (result.staging_directory / "archive.verified").read_bytes() == body
    assert not (result.staging_directory / "archive.part").exists()
    assert not (result.staging_directory / "expanded.tar").exists()
    assert len(calls) == 3
    executable = result.bundle_directory / ("gitgo.exe" if kind == "zip" else "gitgo")
    assert executable.read_bytes() == b"executable-placeholder"
    assert executable.stat().st_mode & 0o7777 == 0o700


def test_disabled_preparation_has_no_clock_network_or_filesystem_access():
    def forbidden():
        pytest.fail("disabled preparation touched a dependency")
    assert prepare_update(UpdatePolicy(), None, None, staging_parent=None, protected_paths=None,
                          clock=forbidden, cancelled=forbidden).status == "disabled"
    assert prepare_update(POLICY, None, None, staging_parent=None, protected_paths=None,
                          automatic=True, clock=forbidden).status == "disabled"


def test_automatic_checks_do_not_authorize_automatic_preparation(signing, locations):
    with pytest.raises(UpdateError, match="UPDATE_PREPARATION_MANUAL"):
        pipeline(signing, locations, policy=replace(POLICY, automatic_check=True), automatic=True)
    assert not list(locations[0].iterdir())


def test_up_to_date_does_not_download_or_create_staging(signing, locations):
    result, calls = pipeline(signing, locations, wire_changes={"version": CURRENT.version})
    assert result.status == "up_to_date"
    assert len(calls) == 2
    assert not list(locations[0].iterdir())


@pytest.mark.parametrize("delivered", [b"short", tar_bytes() + b"extra", b"x" * len(tar_bytes())])
def test_size_and_hash_damage_never_leave_usable_staging(signing, locations, delivered):
    with pytest.raises(UpdateError):
        pipeline(signing, locations, delivered=delivered)
    assert not list(locations[0].iterdir())


def test_stream_interruption_discards_partial_download(signing, locations):
    def failed(_):
        return httpx.Response(200, stream=Stream([b"prefix"], httpx.ReadError("offline")))
    with pytest.raises(UpdateError, match="UPDATE_TRANSFER_FAILED"):
        pipeline(signing, locations, handler=failed)
    assert not list(locations[0].iterdir())


@pytest.mark.parametrize("path", ["/outside", "../outside", "bundle/../../outside", "C:/outside",
                                 "bundle\\outside", "bundle//outside", "bundle/./outside",
                                 "bundle/con.txt", "bundle/aux", "bundle/file:stream",
                                 "bundle/end.", "bundle/end ", "bundle/\nname",
                                 "bundle/" + "x" * 256, "bundle/" + "x/" * 33 + "end"])
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_unsafe_paths_in_both_archive_formats(signing, locations, path, archive_format):
    body = tar_bytes([(path, "file", b"bad")]) if archive_format == "tar.gz" else zip_bytes([(path, b"bad", stat.S_IFREG | 0o600)])
    kwargs = {} if archive_format == "tar.gz" else {"wire_changes": {"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                                                   "current": replace(CURRENT, platform="windows", architecture="x86_64")}
    with pytest.raises(UpdateError):
        pipeline(signing, locations, body, **kwargs)
    assert not list(locations[0].iterdir())


@pytest.mark.parametrize("names", [["bundle/a", "bundle/a"], ["bundle/a", "bundle/A"],
                                  ["bundle/é", "bundle/e\u0301"], ["bundle/Data/a", "bundle/data/b"],
                                  ["bundle/a", "bundle/a/child"]])
@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_duplicates_collisions_and_bad_layout_are_rejected(signing, locations, names, archive_format):
    if archive_format == "tar.gz":
        body = tar_bytes([(name, "file", b"data") for name in names])
        kwargs = {}
    else:
        with pytest.warns(UserWarning) if len(names) > 1 and names[0] == names[1] else nullcontext():
            body = zip_bytes([(name, b"data", stat.S_IFREG | 0o600) for name in names])
        kwargs = {"wire_changes": {"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                  "current": replace(CURRENT, platform="windows", architecture="x86_64")}
    with pytest.raises(UpdateError):
        pipeline(signing, locations, body, **kwargs)
    assert not list(locations[0].iterdir())


@pytest.mark.parametrize("names", [["one/a", "two/b"], ["rootfile"]])
def test_unix_archives_require_one_directory_wrapper(signing, locations, names):
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_LAYOUT"):
        pipeline(signing, locations, tar_bytes([(name, "file", b"data") for name in names]))
    assert not list(locations[0].iterdir())


def test_windows_existing_flat_archive_layout_is_supported(signing, locations):
    body = zip_bytes([("gitgo.exe", b"placeholder", stat.S_IFREG | 0o755),
                      ("product.json", b"{}", stat.S_IFREG | 0o644),
                      ("internal/gitgo-host/gitgo-host.exe", b"host", stat.S_IFREG | 0o755)])
    result, _ = pipeline(signing, locations, body,
                         wire_changes={"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                         current=replace(CURRENT, platform="windows", architecture="x86_64"))
    assert (result.bundle_directory / "gitgo.exe").read_bytes() == b"placeholder"
    assert (result.bundle_directory / "internal/gitgo-host/gitgo-host.exe").read_bytes() == b"host"
    # Safe staging does not claim these placeholder product contents are valid
    # install components. Package-contract verification is a later apply gate.


def test_valid_mac_framework_links_can_point_to_later_entries_and_chains(signing, locations):
    body = tar_bytes([("bundle/Python.framework/Versions/Current", "symlink", "A"),
                      ("bundle/Python.framework/Python", "symlink", "Versions/Current/Python"),
                      ("bundle/Python.framework/Versions/A/Python", "file", b"framework-data"),
                      ("bundle/Python.framework/Versions/A/Resources/info", "file", b"info"),
                      ("bundle/Python.framework/Resources", "symlink", "Versions/Current/Resources"),
                      ("bundle/nested/alias", "symlink", "../Python.framework/Python")])
    result, _ = pipeline(signing, locations, body)
    assert (result.bundle_directory / "Python.framework/Python").read_bytes() == b"framework-data"
    assert (result.bundle_directory / "Python.framework/Resources/info").read_bytes() == b"info"
    assert (result.bundle_directory / "nested/alias").read_bytes() == b"framework-data"


@pytest.mark.parametrize("items", [
    [("bundle/a", "symlink", "/outside")],
    [("bundle/a", "symlink", "../outside")],
    [("bundle/a", "symlink", "missing")],
    [("bundle/a", "symlink", "a")],
    [("bundle/a", "symlink", "b"), ("bundle/b", "symlink", "a")],
    [("bundle/a", "symlink", "file/../file")],
    [("bundle/a", "symlink", "../../outside"), ("bundle/a/child", "file", b"bad")],
    [("bundle/a", "hardlink", "bundle/file")],
    [("bundle/a", "fifo", "")], [("bundle/a", "device", "")],
])
def test_bad_link_graph_hardlinks_devices_and_link_parents(signing, locations, items):
    with pytest.raises(UpdateError):
        pipeline(signing, locations, tar_bytes(items + [("bundle/file", "file", b"original")]))
    assert not list(locations[0].iterdir())


def test_zip_symlinks_and_special_entries_rejected(signing, locations):
    for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
        with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_TYPE"):
            pipeline(signing, locations, zip_bytes([("bundle/a", b"target", mode | 0o600)]),
                     wire_changes={"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                     current=replace(CURRENT, platform="windows", architecture="x86_64"))
    assert not list(locations[0].iterdir())


@pytest.mark.parametrize("limits", [ExtractionLimits(entries=1), ExtractionLimits(total_bytes=5),
                                   ExtractionLimits(file_bytes=5), ExtractionLimits(expansion_ratio=1)])
def test_archive_resource_limits_are_enforced(signing, locations, limits):
    with pytest.raises(UpdateError):
        pipeline(signing, locations, tar_bytes([("bundle/first", "file", b"123456"),
                                                ("bundle/second", "file", b"abcdef")]), limits=limits)
    assert not list(locations[0].iterdir())


@pytest.mark.parametrize("broken", [b"not an archive", tar_bytes()[:-3]])
def test_even_correctly_signed_broken_archive_is_not_accepted(signing, locations, broken):
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_INVALID"):
        pipeline(signing, locations, broken)
    assert not list(locations[0].iterdir())


def test_zip_crc_damage_detected_after_download_digest_matches(signing, locations):
    body = bytearray(zip_bytes())
    at = body.index(b"PK\x01\x02") + 16  # central-directory CRC, not signed digest
    body[at] ^= 1
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_INVALID"):
        pipeline(signing, locations, bytes(body),
                 wire_changes={"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                 current=replace(CURRENT, platform="windows", architecture="x86_64"))
    assert not list(locations[0].iterdir())


def test_central_directory_rejected_before_zipfile_eager_allocation(signing, locations, monkeypatch):
    from backend.core.update import archive
    body = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 10000000, 0, 0)
    monkeypatch.setattr(archive.zipfile, "ZipFile", lambda *_: pytest.fail("unbounded central directory parsed"))
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_LIMIT"):
        pipeline(signing, locations, body,
                 wire_changes={"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                 current=replace(CURRENT, platform="windows", architecture="x86_64"))


@pytest.mark.parametrize("when", [1, 2])
def test_expiry_during_download_or_extraction_discards_stage(signing, locations, when):
    times = iter([NOW] * when + [NOW + timedelta(days=3)])
    with pytest.raises(UpdateError, match="UPDATE_FRESHNESS_INVALID"):
        pipeline(signing, locations, clock=lambda: next(times))
    assert not list(locations[0].iterdir())


def test_staging_cannot_overlap_protected_data_in_either_direction(signing, locations):
    for parent, roots in [(locations[1][0], locations[1]), (locations[0], (locations[0] / "child",)),
                          (locations[0], (locations[0].parent,))]:
        parent.chmod(0o700)
        with pytest.raises(UpdateError, match="UPDATE_PROTECTED_PATHS"):
            pipeline(signing, (parent, roots))
    assert not list(locations[0].iterdir())


def test_staging_symlink_and_nonprivate_parent_rejected(signing, locations):
    link = locations[0].parent / "alias"
    link.symlink_to(locations[0], target_is_directory=True)
    with pytest.raises(UpdateError, match="UPDATE_STAGING_INVALID"):
        pipeline(signing, (link, locations[1]))
    locations[0].chmod(0o777)
    with pytest.raises(UpdateError, match="UPDATE_STAGING_INVALID"):
        pipeline(signing, locations)
    assert not list(locations[0].iterdir())


def test_keyboard_interrupt_does_not_leave_partial_stage(signing, locations, monkeypatch):
    def interrupted(self, url, maximum, output, **kwargs):
        output.write(b"partial")
        raise KeyboardInterrupt
    monkeypatch.setattr(HttpsTransport, "receive", interrupted)
    # Metadata uses receive too; use its bounded fetch separately for this test.
    raw, signature = signed(manifest_wire(), signing)
    monkeypatch.setattr(HttpsTransport, "fetch_bytes", lambda self, url, _, **kwargs: raw if url == POLICY.manifest_url else signature)
    with pytest.raises(KeyboardInterrupt):
        prepare_update(POLICY, CURRENT, signing[1], staging_parent=locations[0],
                       protected_paths=locations[1], clock=lambda: NOW)
    assert not list(locations[0].iterdir())


def test_hard_process_exit_leaves_only_inert_private_staging(locations):
    before = snapshot(locations[1])
    script = '''
import os, sys
from pathlib import Path
from tests.test_update_manifest import manifest_wire, signed, signing
from tests.test_update_discovery import CURRENT, NOW, POLICY
from backend.core.update import prepare_update, HttpsTransport
keys = signing.__wrapped__()
raw, signature = signed(manifest_wire(), keys)
HttpsTransport.fetch_bytes = lambda self, url, _, **kwargs: raw if url == POLICY.manifest_url else signature
def crash(self, url, maximum, output, **kwargs):
    output.write(b"interrupted")
    output.flush()
    os._exit(73)
HttpsTransport.receive = crash
prepare_update(POLICY, CURRENT, keys[1], staging_parent=Path(sys.argv[1]),
               protected_paths=tuple(Path(p) for p in sys.argv[2:]), clock=lambda: NOW)
'''
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(locations[0]),
                                *(str(p) for p in locations[1])], capture_output=True)
    assert completed.returncode == 73, completed.stderr.decode()
    leftovers = list(locations[0].iterdir())
    assert len(leftovers) == 1
    assert (leftovers[0] / "archive.part").read_bytes() == b"interrupted"
    assert not (leftovers[0] / "archive.verified").exists()
    assert snapshot(locations[1]) == before
    # The test's temporary-directory fixture owns cleanup; updater never adopts
    # arbitrary leftovers or treats them as active installations.


def test_pax_metadata_read_is_bounded_before_allocating_big_body(signing, locations):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("bundle/file")
        member.size = 1
        member.pax_headers = {"comment": "x" * (1024**2 + 1)}
        archive.addfile(member, io.BytesIO(b"a"))
    # Uncompressed gzip avoids the separate ratio limit masking metadata checks.
    body = gzip.compress(output.getvalue(), compresslevel=0)
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_LIMIT"):
        pipeline(signing, locations, body)
    assert not list(locations[0].iterdir())


def test_total_tar_metadata_limit_covers_hidden_global_headers(signing, locations):
    output = io.BytesIO()
    # Global PAX records are not yielded as files; the ordinary entry count
    # alone would not account for this allocation path.
    for index in range(20):
        output.write(tarfile.TarInfo.create_pax_global_header({f"field{index}": "x" * 1000}))
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("bundle/file")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"a"))
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_LIMIT"):
        pipeline(signing, locations, gzip.compress(output.getvalue(), compresslevel=0),
                 limits=ExtractionLimits(metadata_bytes=4096))
    assert not list(locations[0].iterdir())


def test_zip_directory_cannot_lie_about_entry_count(signing, locations):
    body = bytearray(zip_bytes([("one", b"1", stat.S_IFREG), ("two", b"2", stat.S_IFREG)]))
    end = body.index(b"PK\x05\x06")
    struct.pack_into("<HH", body, end + 8, 1, 1)
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_INVALID"):
        pipeline(signing, locations, bytes(body),
                 wire_changes={"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                 current=replace(CURRENT, platform="windows", architecture="x86_64"))
    assert not list(locations[0].iterdir())


def test_deflated_zip_bomb_hits_ratio_limit(signing, locations):
    body = zip_bytes([("large", b"0" * 1024**2, stat.S_IFREG)])
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_LIMIT"):
        pipeline(signing, locations, body,
                 wire_changes={"platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                 current=replace(CURRENT, platform="windows", architecture="x86_64"))
    assert not list(locations[0].iterdir())


def test_disk_failure_is_cleaned_without_touching_protected_data(signing, locations, monkeypatch):
    from backend.core.update import staging
    def failed(_):
        raise OSError("disk full")
    monkeypatch.setattr(staging.os, "fsync", failed)
    with pytest.raises(UpdateError, match="UPDATE_STAGING_FAILED"):
        pipeline(signing, locations)
    assert not list(locations[0].iterdir())


def test_cancellation_during_archive_expansion_discards_stage(signing, locations, monkeypatch):
    from backend.core.update import staging
    original = staging._extract_archive
    cancelled = [False]
    def cancel(*args):
        cancelled[0] = True
        return original(*args)
    monkeypatch.setattr(staging, "_extract_archive", cancel)
    with pytest.raises(UpdateError, match="UPDATE_CANCELLED"):
        pipeline(signing, locations, cancelled=lambda: cancelled[0])
    assert not list(locations[0].iterdir())


def test_archive_deadline_is_enforced(signing, locations, monkeypatch):
    from backend.core.update import staging
    original = staging._extract_archive
    def delayed(*args):
        ticks = iter([0, 0, 2])
        monkeypatch.setattr("backend.core.update.archive.time.monotonic", lambda: next(ticks))
        return original(*args)
    monkeypatch.setattr(staging, "_extract_archive", delayed)
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_TIMEOUT"):
        pipeline(signing, locations, limits=ExtractionLimits(seconds=1))
    assert not list(locations[0].iterdir())


def test_untrusted_signature_stops_before_any_asset_or_disk_access(signing, locations):
    from tests.test_update_manifest import sign_raw
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw, _ = signed(manifest_wire(), signing)
    wrong = sign_raw(raw, Ed25519PrivateKey.generate())
    calls = []
    def handler(request):
        url = str(request.url)
        calls.append(url)
        assert url in (POLICY.manifest_url, POLICY.signature_url)
        return httpx.Response(200, stream=Stream([raw if url == POLICY.manifest_url else wrong]))
    with pytest.raises(UpdateError, match="UPDATE_SIGNATURE_INVALID"):
        prepare_update(POLICY, CURRENT, signing[1], staging_parent=locations[0],
                       protected_paths=locations[1], clock=lambda: NOW,
                       transport=HttpsTransport(POLICY.allowed_hosts, transport=httpx.MockTransport(handler)))
    assert len(calls) == 2
    assert not list(locations[0].iterdir())


def test_unvalidated_windows_acl_adapter_fails_closed(signing, locations, monkeypatch):
    from types import SimpleNamespace
    from backend.core.update import staging
    # Do not monkeypatch the shared os module: pathlib/test fixtures also use it.
    monkeypatch.setattr(staging, "os", SimpleNamespace(name="nt"))
    with pytest.raises(UpdateError, match="UPDATE_STAGING_PLATFORM"):
        pipeline(signing, locations)
    assert not list(locations[0].iterdir())


def test_nonsticky_writable_ancestor_cannot_swap_private_parent(signing, locations):
    unsafe = locations[0] / "unsafe-parent"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    private = unsafe / "private-staging"
    private.mkdir(mode=0o700)
    with pytest.raises(UpdateError, match="UPDATE_STAGING_INVALID"):
        pipeline(signing, (private, locations[1]))
    assert not list(private.iterdir())


@pytest.mark.parametrize("suffix", [b"extra", gzip.compress(b"another member")])
def test_concatenated_gzip_or_trailing_bytes_are_not_hidden(signing, locations, suffix):
    with pytest.raises(UpdateError, match="UPDATE_ARCHIVE_INVALID"):
        pipeline(signing, locations, tar_bytes() + suffix)
    assert not list(locations[0].iterdir())


def test_existing_windows_packager_zip_layout_not_an_invented_test_format(signing, locations, monkeypatch):
    import json
    from backend.version import __version__
    from scripts import release_artifacts
    from scripts.render_product_manifest import render_manifest
    root = locations[0].parent / "build-source"
    (root / "packaging").mkdir(parents=True)
    (root / "cli/dashboard").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "packaging/product.json"
    (root / "packaging/product.json").write_bytes(source.read_bytes())
    product = render_manifest(source, "windows", "x86_64")
    (root / "cli/dashboard/package.json").write_text(json.dumps({"version": __version__}))
    stage = root / "payload"
    (stage / "internal/gitgo-host").mkdir(parents=True)
    (stage / "product.json").write_text(json.dumps(product))
    (stage / "gitgo.exe").write_bytes(b"placeholder")
    (stage / "internal/gitgo-host/gitgo-host.exe").write_bytes(b"host-placeholder")
    for name in ("uninstall.exe", "dashboard/dist/cli.js", "dashboard/dist/trace.js",
                 "dashboard/scripts/launch.mjs", "dashboard/scripts/runtime.mjs"):
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder-not-a-runnable-product")
    monkeypatch.setattr(release_artifacts, "source_identity", lambda _: {"commit": "b" * 40,
                                                                        "tree": "c" * 40, "dirty": False})
    packaged = release_artifacts.package(root, stage, root / "output")
    result, _ = pipeline(signing, locations, Path(packaged["archive"]).read_bytes(),
                         wire_changes={"version": __version__, "platform": "windows", "architecture": "x86_64", "archive_format": "zip"},
                         current=replace(CURRENT, version="1.0.0-alpha.1", platform="windows", architecture="x86_64"))
    assert (result.bundle_directory / "build-manifest.json").is_file()
    assert (result.bundle_directory / "gitgo.exe").read_bytes() == b"placeholder"
    # The existing packager is used, but placeholders are NOT runnable Windows
    # binaries and product/source/data-contract acceptance is still outstanding.


@pytest.mark.skipif(os.name != "posix" or shutil.which("tar") is None, reason="native tar unavailable")
def test_native_tar_directory_and_symlinks_match_unix_packager_command(signing, locations):
    source = locations[0].parent / "tar-source"
    root = source / "macos-arm64"
    (root / "internal").mkdir(parents=True)
    (root / "gitgo").write_bytes(b"placeholder")
    (root / "internal/library").write_bytes(b"library")
    (root / "alias").symlink_to("internal/library")
    archive = locations[0].parent / "native.tar.gz"
    env = dict(os.environ, COPYFILE_DISABLE="1")
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "macos-arm64"],
                   check=True, capture_output=True, env=env)
    result, _ = pipeline(signing, locations, archive.read_bytes())
    assert result.bundle_directory.name == "macos-arm64"
    assert (result.bundle_directory / "alias").read_bytes() == b"library"


def test_bounded_tar_xattrs_and_acls_are_not_restored(signing, locations):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("bundle/file")
        member.size, member.mode = 1, 0o666
        member.pax_headers = {"SCHILY.xattr.user.gitgo_test": "untrusted",
                              "SCHILY.acl.access": "untrusted", "SCHILY.fflags": "untrusted"}
        archive.addfile(member, io.BytesIO(b"a"))
    result, _ = pipeline(signing, locations, gzip.compress(output.getvalue()))
    path = result.bundle_directory / "file"
    assert path.read_bytes() == b"a"
    assert path.stat().st_mode & 0o7777 == 0o600
    if hasattr(os, "listxattr"):
        assert "user.gitgo_test" not in os.listxattr(path)
