"""Windows single-file updates. The public executable is the replacement target."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import file_digest, sha256
import argparse
import ctypes as C
from ctypes import wintypes
import os
from pathlib import Path
import stat
import subprocess
import sys
from uuid import uuid4

from .checker import CurrentInstallation, ReplayState, check_for_update
from .data_transition import ENGINE_VERSION
from .health import isolated_environment
from .manifest import DataCompatibility
from .persistence import atomic_control, read_control
from .release_policy import parse_release_policy, read_policy_file
from .transport import HttpsTransport
from .validation import UpdateError, json_object, reject
from .version import Version
from . import windows_storage as ntfs

PORTABLE_FOOTER = b"GITGO-WINDOWS-PORTABLE-v1\0"


@dataclass(frozen=True)
class ExecutableVerifier:
    publisher: object

    @property
    def configured(self):
        return self.publisher.configured

    def verify(self, raw, signature):
        return self.publisher.verify_portable(raw, signature)


def release_settings(package):
    product = json_object((package / "product.json").read_bytes(), 65536)
    channel = "preview" if Version.parse(product["version"]).prerelease else "stable"
    trust = parse_release_policy(read_policy_file(package / "release-trust.json"), "windows", "x86_64")
    contract = json_object((package / "update-data-contract.json").read_bytes(), 256 * 1024)
    current = CurrentInstallation(product["product_id"], product["version"], "windows", "x86_64", ENGINE_VERSION,
                                  DataCompatibility.parse(contract["data_compatibility"]), managed=True)
    policy = trust.channel(channel)
    policy = replace(policy, manifest_url=policy.manifest_url.removesuffix(".manifest.json") + ".portable.manifest.json",
                     signature_url=policy.signature_url.removesuffix(".manifest.sig") + ".portable.manifest.sig")
    return current, policy, ExecutableVerifier(trust.verifier)


@contextmanager
def pinned_launcher(pid):
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, C.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    if pid != os.getppid():
        reject("UPDATE_HANDOFF_INVALID", "Portable update requires its original launcher")
    handle = kernel.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
        length = wintypes.DWORD(32768)
        value = C.create_unicode_buffer(length.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, value, C.byref(length)):
            raise C.WinError(C.get_last_error())
        target = Path(value.value)
        if target != target.resolve(strict=True) or target.suffix.lower() != ".exe":
            reject("UPDATE_HANDOFF_INVALID", "Portable executable path is not canonical")

        def wait():
            if kernel.WaitForSingleObject(handle, 30000) != 0:
                reject("UPDATE_BUSY", "The portable launcher has not exited")

        yield target, wait
    finally:
        kernel.CloseHandle(handle)


def executable_fact(path):
    with os.fdopen(ntfs.open_file(path, share=1), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 2**31:
            reject("UPDATE_EXECUTABLE_INVALID", "Expected a bounded, single-link executable")
        if stream.read(2) != b"MZ":
            reject("UPDATE_EXECUTABLE_INVALID", "Expected a Windows PE executable")
        if info.st_size < len(PORTABLE_FOOTER) + 2:
            reject("UPDATE_EXECUTABLE_INVALID", "Portable executable is truncated")
        stream.seek(-len(PORTABLE_FOOTER), os.SEEK_END)
        if stream.read() != PORTABLE_FOOTER:
            reject("UPDATE_EXECUTABLE_INVALID", "Expected a Gitgo portable executable")
        stream.seek(0)
        return file_digest(stream, "sha256").hexdigest(), info.st_size


@contextmanager
def operation_directory(target):
    name = ".gitgo-portable-" + sha256(os.path.normcase(str(target)).encode("utf-8")).hexdigest()[:20]
    state = target.parent / name
    with ntfs.private_namespace(target.parent):
        try:
            ntfs.create_private_directory(state)
        except FileExistsError:
            pass
        with ntfs.private_namespace(state):
            lock = state / "operation.lock"
            try:
                os.close(ntfs.open_file(lock, create=True, write=True))
            except FileExistsError:
                pass
            with ntfs.lock_file(lock):
                yield state


def ready_candidate(current, policy, verifier, transport, previous=None):
    result = check_for_update(policy, current, verifier, transport.fetch_bytes, previous=previous)
    if result.candidate and result.candidate.data_compatibility != current.data_compatibility:
        reject("UPDATE_PORTABLE_MIGRATION_REQUIRED", "This portable update changes the data compatibility contract")
    return result


def _health_check(candidate, profile, version):
    ntfs.create_private_directory(profile)
    try:
        result = subprocess.run([str(candidate), "--version"], cwd=profile, env=isolated_environment(profile),
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if result.returncode or result.stdout.strip() != f"Gitgo {version}":
            reject("UPDATE_HEALTH_FAILED", "Portable candidate did not start as the signed version")
    except (OSError, subprocess.TimeoutExpired):
        reject("UPDATE_HEALTH_FAILED", "Portable candidate failed the startup check")


def apply(target, state, original, current, policy, verifier, transport, *, health_check=_health_check):
    watermark_path = state / "watermark.json"
    previous = None
    if watermark_path.exists():
        wire = read_control(watermark_path)
        previous = ReplayState(**{**wire, "published_at": datetime.fromisoformat(wire["published_at"])})
    result = ready_candidate(current, policy, verifier, transport, previous)
    if result.candidate is None:
        return result.status, current.version
    candidate = result.candidate
    nonce = uuid4().hex
    staged = state / (nonce + ".exe")
    try:
        with os.fdopen(ntfs.open_file(staged, create=True, write=True), "wb") as stream:
            transport.receive(candidate.url, candidate.bytes, stream, expected_size=candidate.bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if executable_fact(staged) != (candidate.sha256, candidate.bytes):
            reject("UPDATE_PACKAGE_DIGEST", "Portable executable differs from the publisher's signed release")
        health_check(staged, state / ("health-" + nonce), candidate.version)
        if executable_fact(target) != original or executable_fact(staged) != (candidate.sha256, candidate.bytes):
            reject("UPDATE_SELECTION_STALE", "Portable target or verified candidate changed")
        old_sha, old_size = original
        backup = state / (old_sha + ".previous.exe")
        if not backup.exists():
            ntfs.durable_copy(target, backup)
        if executable_fact(backup) != original:
            reject("UPDATE_PACKAGE_DIGEST", "Could not verify retained portable predecessor")
        atomic_control(state / (candidate.sha256 + ".rollback.json"),
                       {"sha256": old_sha, "bytes": old_size, "version": current.version})
        wire = vars(result.watermark)
        atomic_control(watermark_path, {**wire, "published_at": wire["published_at"].isoformat()})
        ntfs.move(staged, target, replace=True)
        return "committed", candidate.version
    finally:
        if staged.exists():
            staged.unlink()


def rollback(target, state, original):
    record = state / (original[0] + ".rollback.json")
    if not record.is_file():
        reject("UPDATE_ROLLBACK_UNAVAILABLE", "There is no retained predecessor for this exact portable file")
    wire = read_control(record)
    from .validation import digest, fields, number, text
    fields(wire, {"sha256", "bytes", "version"})
    backup = state / (digest(wire["sha256"]) + ".previous.exe")
    version = text(wire["version"], r"[0-9A-Za-z.+-]+")
    if executable_fact(backup) != (wire["sha256"], number(wire["bytes"], maximum=2**31)):
        reject("UPDATE_PACKAGE_DIGEST", "Retained portable executable failed verification")
    if executable_fact(target) != original:
        reject("UPDATE_SELECTION_STALE", "Portable target changed before rollback")
    ntfs.move(backup, target, replace=True)
    return "rolled_back", version


def main(argv):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        reject("UPDATE_INSTALL_INVALID", "Portable updates require the packaged Windows runtime")
    package = Path(sys.executable).resolve(strict=True).parents[2]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("check", "apply", "rollback"))
    parser.add_argument("--parent", type=int)
    parser.add_argument("--ready", type=Path)
    args = parser.parse_args(argv)
    try:
        current, policy, verifier = release_settings(package)
        transport = HttpsTransport(policy.allowed_hosts)
        if args.operation == "check":
            result = ready_candidate(current, policy, verifier, transport)
            status, version = result.status, result.candidate.version if result.candidate else current.version
        else:
            if args.parent is None or args.ready != package.parent / "updater.ready":
                reject("UPDATE_HANDOFF_INVALID", "Portable handoff location is invalid")
            with pinned_launcher(args.parent) as (target, wait):
                original = executable_fact(target)
                with args.ready.open("x"):
                    pass
                wait()
                with operation_directory(target) as state:
                    if args.operation == "apply":
                        status, version = apply(target, state, original, current, policy, verifier, transport)
                    else:
                        status, version = rollback(target, state, original)
        print(f"Gitgo portable update: {status} ({version})", flush=True)
        return 0
    except UpdateError as exc:
        print(f"Gitgo portable update: {exc.code}: {exc}", file=sys.stderr, flush=True)
        return 2
    except OSError:
        print("Gitgo portable update: UPDATE_PORTABLE_IO: unable to safely replace the executable",
              file=sys.stderr, flush=True)
        return 2
