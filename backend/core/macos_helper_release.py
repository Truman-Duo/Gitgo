"""Read-only local release evidence for a complete frozen macOS helper.

This does not install, write a receipt, run candidate code, or enable production.
The fixed root policy uses the updater's existing release-trust schema. Nothing
here provisions that policy: initial administrative/bootstrap authentication is
still a release-integration decision. A candidate's policy, public key, hash or
an already-constructed UpdateManifest is never accepted as authority.

The returned dictionary is diagnostic evidence, NOT an installation capability.
A future trusted installer must repeat this verification under its own locks and
bind the resulting root-owned snapshot to a receipt before executing anything.
Freshness is checked for this inspection, but there is deliberately no synthetic
CurrentInstallation, replay watermark, upgrade or retained-rollback decision.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile

from backend.core.macos_file_authority import require_no_extended_acl
from backend.core.macos_helper_install import identity, inventory as helper_inventory
from backend.core.update.archive import ExtractionLimits, _extract_archive
from backend.core.update.contracts import verify_bundle_contract
from backend.core.update.release_policy import MAX_POLICY_BYTES, parse_release_policy
from backend.core.update.staging import _verify_file
from backend.core.update.tree import inventory as package_inventory
from backend.core.update.validation import https_url


PRODUCTION_ENABLED = False
_ROOT_POLICY = Path('/Library/PrivilegedHelperTools/org.gitgo.sandbox-v1/release-trust.json')
_WORK_ROOT = Path('/private/tmp')
_HELPER = 'internal/gitgo-host'
_CHUNK = 1024 * 1024


class HelperReleaseError(ValueError):
    """Release evidence failed; no alternative trust mode is selected."""


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _root_owned(info, *, directory):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise HelperReleaseError('Release policy must be controlled exclusively by root')
    if not directory and (info.st_nlink != 1 or not 0 < info.st_size <= MAX_POLICY_BYTES):
        raise HelperReleaseError('Release policy must be a bounded file with one link')


def _open_file(path, *, policy=False):
    """Keep every lookup relative to a no-follow directory descriptor."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.name in ('', '.'):
        raise HelperReleaseError('An absolute, non-traversing file path is required')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open('/', flags | os.O_DIRECTORY)
    try:
        if policy:
            _root_owned(os.fstat(directory), directory=True)
            require_no_extended_acl(directory)
        for part in path.parts[1:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
            if policy:
                _root_owned(os.fstat(directory), directory=True)
                require_no_extended_acl(directory)
        return os.open(path.name, flags | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def _read_root_policy():
    # No path/key parameter and no environment or candidate policy fallback.
    try:
        fd = _open_file(_ROOT_POLICY, policy=True)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _root_owned(before, directory=False)
            require_no_extended_acl(stream.fileno())
            raw = stream.read(MAX_POLICY_BYTES + 1)
            if len(raw) != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise HelperReleaseError('Release policy changed while being read')
            return raw
    except OSError:
        raise HelperReleaseError('Fixed root release policy is missing or unsafe') from None


def _clock():
    return datetime.now(timezone.utc)


def _fresh(manifest):
    now = _clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise HelperReleaseError('An aware local clock is required')
    if not manifest.published_at <= now < manifest.expires_at:
        raise HelperReleaseError('Release metadata is expired or not yet valid')


def _require_local_admin(allow_local_adhoc):
    if (sys.platform != 'darwin' or platform.machine() != 'arm64'
            or os.getuid() != 0 or os.geteuid() != 0):
        raise PermissionError('Helper release inspection requires an Apple Silicon macOS administrator')
    if allow_local_adhoc is not True:
        raise HelperReleaseError('Production is disabled; explicit local acceptance is required')


def _temporary_work():
    # A fixed sticky root-owned parent, not inherited TMPDIR or candidate input.
    fd = os.open(_WORK_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise HelperReleaseError('Temporary parent is not root-controlled or sticky')
        require_no_extended_acl(fd)
        work = Path(tempfile.mkdtemp(prefix='gitgo-helper-release-', dir=_WORK_ROOT))
        try:
            opened = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(opened)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or _stamp(work.lstat()) != _stamp(info)):
                    raise HelperReleaseError('Temporary inspection directory is not private')
                require_no_extended_acl(opened)
            finally:
                os.close(opened)
        except BaseException:
            shutil.rmtree(work)
            raise
        return work
    finally:
        os.close(fd)


def _copy_verified_archive(source, destination, manifest):
    before = os.fstat(source.fileno())
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size != manifest.bytes):
        raise HelperReleaseError('Archive must be one regular file with the signed size')
    remaining = manifest.bytes
    while remaining:
        chunk = source.read(min(_CHUNK, remaining))
        if not chunk:
            raise HelperReleaseError('Archive changed during copying')
        destination.write(chunk)
        remaining -= len(chunk)
    if source.read(1) or _stamp(os.fstat(source.fileno())) != _stamp(before):
        raise HelperReleaseError('Archive changed during copying')
    destination.flush()
    os.fsync(destination.fileno())
    _verify_file(destination, manifest, lambda: False)


def _expected_helper(records):
    if next((r['kind'] for r in records if r['path'] == _HELPER), None) != 'directory':
        raise HelperReleaseError('Package helper must be a real directory')
    expected = []
    prefix = _HELPER + '/'
    for record in records:
        if not record['path'].startswith(prefix):
            continue
        path = record['path'][len(prefix):]
        if record['kind'] == 'directory':
            expected.append([path, 'directory'])
        elif record['kind'] == 'symlink':
            expected.append([path, 'link', record['link']])
        else:
            expected.append([path, 'file', bool(record['mode'] & 0o111),
                             record['bytes'], record['sha256']])
    return sorted(expected)


def inspect_release(archive, raw_manifest, detached_signature, *, channel, allow_local_adhoc=False):
    """Verify exact archive bytes and all package files; return diagnostics only.

    No root configuration/service paths are written. The sole temporary tree is
    removed even on failure. The caller must not treat returned hashes as proof
    for a different directory, future installation, or a rollback selection.
    """
    _require_local_admin(allow_local_adhoc)
    raw_policy = _read_root_policy()
    policy = parse_release_policy(raw_policy, 'macos', 'arm64')
    selected = policy.channel(channel)
    manifest = policy.verifier.verify(raw_manifest, detached_signature)
    if manifest.scope != ('gitgo-terminal', channel, 'macos', 'arm64'):
        raise HelperReleaseError('Release product, channel or target does not match')
    https_url(manifest.url, selected.allowed_hosts)
    _fresh(manifest)
    work = None
    try:
        # Authentication precedes opening any archive path or making temporary files.
        fd = _open_file(archive)
        with os.fdopen(fd, 'rb') as source:
            work = _temporary_work()
            with (work / 'archive').open('x+b') as copy:
                _copy_verified_archive(source, copy, manifest)
                _fresh(manifest)
                package = _extract_archive(copy, manifest.archive_format, work / 'payload', work,
                                           ExtractionLimits(), lambda: False)
        verify_bundle_contract(package, manifest)
        records = package_inventory(package)
        actual = helper_inventory(package / _HELPER, sealed=True)
        if actual != _expected_helper(records):
            raise HelperReleaseError('Helper inventory differs from authenticated package evidence')
        # A trusted local owner could still edit between checks. Refuse changes,
        # including changes outside the helper, before reporting a coherent view.
        verify_bundle_contract(package, manifest)
        if package_inventory(package) != records or helper_inventory(package / _HELPER, sealed=True) != actual:
            raise HelperReleaseError('Package changed during inspection')
        if _read_root_policy() != raw_policy:
            raise HelperReleaseError('Release authority changed during inspection')
        _fresh(manifest)
        return {
            'schema': 1, 'purpose': 'local-release-inspection', 'production_enabled': False,
            'policy_sha256': hashlib.sha256(raw_policy).hexdigest(),
            'manifest_sha256': manifest.manifest_sha256, 'key_id': manifest.key_id,
            'archive_sha256': manifest.sha256, 'archive_bytes': manifest.bytes,
            'product_id': manifest.product_id, 'channel': manifest.channel,
            'platform': manifest.platform, 'architecture': manifest.architecture,
            'version': manifest.version, 'source_commit': manifest.source_commit,
            'source_tree': manifest.source_tree, 'release_id': manifest.release_id,
            'sequence': manifest.sequence, 'helper_relative_path': _HELPER,
            'helper_inventory': actual, 'helper_inventory_sha256': identity(actual),
        }
    finally:
        if work is not None:
            # Never remove a supplied package, archive, or service directory.
            shutil.rmtree(work)
