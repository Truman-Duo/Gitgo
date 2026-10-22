"""Opt-in preparation: discover, download, hash and isolate; NEVER install/run.

Staging paths and protected roots must come from local installation authority,
not the release manifest. Durable watermark/ownership adapters remain separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Callable

from .archive import ExtractionLimits, _extract_archive
from .checker import CurrentInstallation, ReplayState, UpdatePolicy, check_for_update
from .manifest import ManifestVerifier, UpdateManifest
from .transport import CHUNK_BYTES, HttpsTransport
from .validation import UpdateError, reject


@dataclass(frozen=True)
class PreparationResult:
    status: str
    candidate: UpdateManifest | None = None
    watermark: ReplayState | None = None
    staging_directory: Path | None = None
    bundle_directory: Path | None = None


def _staging_parent(parent: Path, protected_paths: tuple[Path, ...]) -> Path:
    windows = False
    if os.name == "nt" and hasattr(os, "open"):
        from .windows_storage import supported
        windows = supported()
    if os.name != "posix" and not windows:
        reject("UPDATE_STAGING_PLATFORM", "Private staging requires a validated platform ownership adapter")
    if (not isinstance(parent, Path) or not parent.is_absolute()
            or parent != parent.resolve(strict=True) or not parent.is_dir()):
        reject("UPDATE_STAGING_INVALID", "Staging requires an existing canonical directory")
    # Reject symlink ancestors even if they happen to resolve within the parent.
    for component in (() if windows else (parent, *parent.parents)):
        if component.is_symlink():
            reject("UPDATE_STAGING_INVALID", "Staging ancestors must not be symlinks")
        info = component.stat()
        if (info.st_uid not in (0, os.getuid())
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            reject("UPDATE_STAGING_INVALID", "Staging ancestors allow untrusted directory replacement")
    if windows:
        from .windows_storage import validate_staging
        validate_staging(parent)
    else:
        info = parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            reject("UPDATE_STAGING_INVALID", "Staging parent must be private and owned by the current user")
    if type(protected_paths) is not tuple or not 1 <= len(protected_paths) <= 256:
        reject("UPDATE_PROTECTED_PATHS", "Explicit local protected paths are required")
    for path in protected_paths:
        if not isinstance(path, Path) or not path.is_absolute():
            reject("UPDATE_PROTECTED_PATHS", "Protected paths must be absolute local paths")
        protected = path.resolve()
        if parent == protected or parent.is_relative_to(protected) or protected.is_relative_to(parent):
            reject("UPDATE_PROTECTED_PATHS", "Staging must not overlap installations, projects or state")
    return parent


def _verify_file(handle, manifest, cancelled):
    handle.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(CHUNK_BYTES):
        if cancelled():
            reject("UPDATE_CANCELLED", "Update preparation was cancelled")
        size += len(chunk)
        if size > manifest.bytes:
            reject("UPDATE_DOWNLOAD_SIZE", "Downloaded archive exceeds its signed size")
        digest.update(chunk)
    if size != manifest.bytes:
        reject("UPDATE_DOWNLOAD_SIZE", "Downloaded archive differs from its signed size")
    if digest.hexdigest() != manifest.sha256:
        reject("UPDATE_HASH_MISMATCH", "Downloaded archive differs from its signed digest")
    handle.seek(0)


def prepare_update(policy: UpdatePolicy, current: CurrentInstallation, verifier: ManifestVerifier, *,
                   staging_parent: Path, protected_paths: tuple[Path, ...],
                   transport: HttpsTransport | None = None,
                   previous: ReplayState | None = None, automatic: bool = False,
                   clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                   cancelled: Callable[[], bool] = lambda: False,
                   limits: ExtractionLimits = ExtractionLimits()) -> PreparationResult:
    # Do not construct transport, touch disk/clock or inspect local data when off.
    if not policy.enabled or (automatic is True and not policy.automatic_check):
        return PreparationResult("disabled")
    if automatic is not False:
        reject("UPDATE_PREPARATION_MANUAL", "Automatic preparation is not supported in this version")
    if transport is None:
        transport = HttpsTransport(policy.allowed_hosts)
    if transport.allowed_hosts != policy.allowed_hosts:
        reject("UPDATE_TRANSPORT_INVALID", "Transport must use the local policy's exact host list")
    if not isinstance(limits, ExtractionLimits):
        reject("UPDATE_ARCHIVE_LIMIT", "Local extraction limits are required")
    metadata = {}

    def fetch(url, maximum):
        if cancelled():
            reject("UPDATE_CANCELLED", "Update preparation was cancelled")
        value = transport.fetch_bytes(url, maximum, cancelled=cancelled)
        metadata[url] = value
        return value

    result = check_for_update(policy, current, verifier, fetch, now=clock(), previous=previous)
    if result.status != "available":
        return PreparationResult(result.status, watermark=result.watermark)
    manifest = result.candidate
    work = None
    complete = False
    try:
        parent = _staging_parent(staging_parent, protected_paths)
        work = Path(tempfile.mkdtemp(prefix="gitgo-update-", dir=parent))
        archive_path = work / "archive.part"
        # Keep the same exclusive descriptor through hash validation and parsing.
        with archive_path.open("x+b") as handle:
            transport.receive(manifest.url, manifest.bytes, handle, expected_size=manifest.bytes,
                              cancelled=cancelled)
            handle.flush()
            os.fsync(handle.fileno())
            _verify_file(handle, manifest, cancelled)
            # A long transfer must not turn expired metadata into usable staging.
            check_for_update(policy, current, verifier, lambda url, _: metadata[url],
                             now=clock(), previous=previous)
            bundle = _extract_archive(handle, manifest.archive_format, work / "payload", work,
                                      limits, cancelled)
            check_for_update(policy, current, verifier, lambda url, _: metadata[url],
                             now=clock(), previous=previous)
        archive_path.rename(work / "archive.verified")
        if os.name == "posix": (work / "archive.verified").chmod(0o400)
        # Windows integrity uses the private DACL, held handles and signed
        # revalidation. READONLY is not an ownership/lease mechanism and would
        # prevent flushing this freshly written cache or cleaning failed work.
        complete = True
        return PreparationResult("staged", manifest, result.watermark, work, bundle)
    except UpdateError:
        raise
    except Exception:
        raise UpdateError("UPDATE_STAGING_FAILED", "Update preparation failed; current installation unchanged") from None
    finally:
        # Only our unpredictable, freshly-created directory is a cleanup target.
        # Hard kill/power loss can leave it behind: it is NEVER an active install.
        if work is not None and not complete:
            try:
                shutil.rmtree(work)
            except OSError:
                raise UpdateError("UPDATE_CLEANUP_FAILED", "Incomplete private staging remains; current installation unchanged") from None
