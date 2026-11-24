"""Publisher identity checks, deliberately not a production installation gate.

No policy is shipped or enabled by this module. A caller may load the fixed
root-controlled policy, or parse bytes whose digest is already pinned by an
authenticated release manifest. A digest supplied by the candidate itself,
an environment variable, a tool request or a CLI flag is NOT a trust anchor.

This checks one executable's Developer ID signature, not the authenticity of
all files in a PyInstaller onedir. Production installation still needs the
complete inventory bound to an authenticated release, a trusted administrative
entry point, and real Developer ID acceptance. It remains disabled here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from backend.core.macos_file_authority import FileAuthorityError, require_no_extended_acl


PRODUCTION_ENABLED = False
_ROOT_POLICY_PATH = Path(
    '/Library/PrivilegedHelperTools/org.gitgo.sandbox-v1/publisher-policy.json'
)
_MAX_POLICY_BYTES = 4096
_MAX_METADATA_BYTES = 64 * 1024
_MAX_EXECUTABLE_BYTES = 1024 ** 3
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_TEAM = re.compile(r'[A-Z0-9]{10}\Z')
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,254}\Z')


class PublisherTrustError(ValueError):
    """A publisher claim could not be established; never fall back to ad hoc."""


@dataclass(frozen=True)
class PublisherPolicy:
    team_identifier: str
    signing_identifier: str
    policy_sha256: str

    def __post_init__(self):
        if not isinstance(self.team_identifier, str) or not _TEAM.fullmatch(self.team_identifier):
            raise PublisherTrustError('Publisher policy has an invalid TeamIdentifier')
        if not isinstance(self.signing_identifier, str) or not _IDENTIFIER.fullmatch(self.signing_identifier):
            raise PublisherTrustError('Publisher policy has an invalid signing Identifier')
        if not isinstance(self.policy_sha256, str) or not _SHA256.fullmatch(self.policy_sha256):
            raise PublisherTrustError('Publisher policy has an invalid SHA-256')


@dataclass(frozen=True)
class PublisherIdentity:
    """Evidence for this executable only; not a reusable installation permit."""

    team_identifier: str
    signing_identifier: str
    policy_sha256: str
    executable_sha256: str

    def __post_init__(self):
        PublisherPolicy(self.team_identifier, self.signing_identifier, self.policy_sha256)
        if not isinstance(self.executable_sha256, str) or not _SHA256.fullmatch(self.executable_sha256):
            raise PublisherTrustError('Publisher evidence has an invalid executable SHA-256')

    @property
    def production_enabled(self):
        return False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublisherTrustError('Publisher policy contains a duplicate field')
        result[key] = value
    return result


def _parse_policy(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_POLICY_BYTES:
        raise PublisherTrustError('Publisher policy is empty or exceeds its size bound')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as exc:
        raise PublisherTrustError('Publisher policy is not strict UTF-8 JSON') from exc
    required = {'schema', 'product_id', 'kind', 'team_identifier', 'signing_identifier'}
    if not isinstance(value, dict) or set(value) != required:
        raise PublisherTrustError('Publisher policy has an unexpected schema')
    if (type(value['schema']) is not int or value['schema'] != 1
            or value['product_id'] != 'gitgo-terminal'
            or value['kind'] != 'developer-id-application'):
        raise PublisherTrustError('Publisher policy has an unsupported identity kind')
    return PublisherPolicy(value['team_identifier'], value['signing_identifier'],
                           hashlib.sha256(raw).hexdigest())


def policy_from_authenticated_release(raw: bytes, *, expected_sha256: str) -> PublisherPolicy:
    """Parse policy bytes pinned by an independently authenticated release.

    This function does not authenticate the supplied digest. Only trusted
    release-verification code may supply it; never use candidate metadata or
    user-provided values as ``expected_sha256``. The root loader below is the
    alternative when no authenticated release-policy binding is available.
    """
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise PublisherTrustError('An authenticated policy digest is required')
    if not isinstance(raw, bytes) or len(raw) > _MAX_POLICY_BYTES:
        raise PublisherTrustError('Publisher policy exceeds its size bound')
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PublisherTrustError('Publisher policy differs from the authenticated release')
    return _parse_policy(raw)


def _check_root_owned(info, *, directory=False):
    correct_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_kind or info.st_uid != 0 or info.st_mode & 0o022:
        raise PublisherTrustError('Publisher policy must be controlled exclusively by root')
    if not directory and (info.st_nlink != 1 or not 0 < info.st_size <= _MAX_POLICY_BYTES):
        raise PublisherTrustError('Publisher policy must be a bounded file with one link')


def _file_stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def load_root_policy() -> PublisherPolicy:
    """Read the fixed policy with no path, Team or identifier override.

    Open each ancestor relative to the previous descriptor, never following
    symlinks. A missing policy fails closed; it does not enable local builds.
    """
    path = _ROOT_POLICY_PATH
    if not path.is_absolute() or '..' in path.parts:
        raise PublisherTrustError('Publisher policy location is invalid')
    directory_fd = file_fd = None
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        directory_fd = os.open('/', flags | os.O_DIRECTORY)
        _check_root_owned(os.fstat(directory_fd), directory=True)
        require_no_extended_acl(directory_fd)
        for part in path.parts[1:-1]:
            child_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
            _check_root_owned(os.fstat(directory_fd), directory=True)
            require_no_extended_acl(directory_fd)
        file_fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        _check_root_owned(before)
        require_no_extended_acl(file_fd)
        chunks = []
        remaining = _MAX_POLICY_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) != before.st_size or _file_stamp(before) != _file_stamp(os.fstat(file_fd)):
            raise PublisherTrustError('Publisher policy changed while being read')
        return _parse_policy(raw)
    except (OSError, FileAuthorityError) as exc:
        raise PublisherTrustError('Root publisher policy is unavailable or unsafe') from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def developer_id_requirement(policy: PublisherPolicy) -> str:
    """Require Developer ID Application, excluding App Store/development IDs.

    Apple TN3127 documents the issuer/leaf OIDs. The Apple anchor is essential:
    another CA can copy those OIDs. Unlike Xcode's cross-distribution DR, this
    requirement has no Mac App Store alternative. It is an external admission
    requirement, not a replacement for the candidate's designated requirement.
    https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements
    """
    if type(policy) is not PublisherPolicy:
        raise PublisherTrustError('An authenticated publisher policy is required')
    # __post_init__ excludes quotes, wildcards, escapes and requirement syntax.
    policy.__post_init__()
    return (
        'anchor apple generic'
        f' and identifier "{policy.signing_identifier}"'
        ' and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
        ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
        f' and certificate leaf[subject.OU] = "{policy.team_identifier}"'
    )


def _metadata_identity(raw, policy):
    if not isinstance(raw, bytes) or len(raw) > _MAX_METADATA_BYTES:
        raise PublisherTrustError('Code signature metadata exceeds its size bound')
    try:
        text = raw.decode('utf-8')
    except UnicodeError as exc:
        raise PublisherTrustError('Code signature metadata is not UTF-8') from exc
    if any(ord(char) < 32 and char != '\n' for char in text):
        raise PublisherTrustError('Code signature metadata contains control characters')
    lines = text.splitlines()

    def one(prefix):
        values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
        if len(values) != 1:
            raise PublisherTrustError('Code signature metadata is missing or ambiguous')
        return values[0]

    if (one('Identifier=') != policy.signing_identifier
            or one('TeamIdentifier=') != policy.team_identifier):
        raise PublisherTrustError('Code signature publisher does not match the pinned policy')
    directory = one('CodeDirectory ')
    flags = re.search(r'\bflags=0x([0-9a-fA-F]+)\(([^)]+)\)(?:\s|$)', directory)
    if flags is None or int(flags.group(1), 16) & 0x2 or 'adhoc' in flags.group(2).split(','):
        raise PublisherTrustError('Code signature is ad hoc or has unsupported flags metadata')
    if any(line.startswith('Signature=') for line in lines):
        raise PublisherTrustError('Code signature has no authenticated signing certificate')
    size = one('Signature size=')
    if not re.fullmatch(r'[1-9][0-9]{0,8}', size):
        raise PublisherTrustError('Code signature has no bounded CMS signature')


def _run_codesign(arguments):
    try:
        result = subprocess.run(
            ['/usr/bin/codesign', *arguments], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=30, check=False,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LANG': 'C'},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublisherTrustError('Publisher signature verification could not complete') from exc
    if result.returncode != 0:
        # Do not copy tool output or candidate-controlled text into an audit log.
        raise PublisherTrustError('Code integrity or pinned Developer ID requirement was rejected')
    if (len(result.stdout) > _MAX_METADATA_BYTES or len(result.stderr) > _MAX_METADATA_BYTES
            or len(result.stdout) + len(result.stderr) > _MAX_METADATA_BYTES):
        raise PublisherTrustError('Code signature metadata exceeds its size bound')
    return result


def _executable_snapshot(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or not 0 < before.st_size <= _MAX_EXECUTABLE_BYTES):
                raise PublisherTrustError('Publisher verification needs one bounded executable file')
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_EXECUTABLE_BYTES:
                    raise PublisherTrustError('Executable exceeds its size bound')
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if total != before.st_size or _file_stamp(before) != _file_stamp(after):
                raise PublisherTrustError('Executable changed during publisher verification')
            return _file_stamp(before), digest.hexdigest()
    except OSError as exc:
        raise PublisherTrustError('Executable is unavailable or unsafe') from exc


def verify_publisher(executable, policy: PublisherPolicy) -> PublisherIdentity:
    """Verify a fixed candidate; do not execute it or turn production on.

    The caller must hold the candidate in its own immutable staging area while
    verifying and installing. Before/after hashing detects ordinary mutation;
    it is not a substitute for root-owned staging and complete bundle sealing.
    All Mach-O architectures must pass the fixed external requirement. The
    display metadata is an additional exact identity/ad-hoc consistency check,
    not the source of certificate trust. This does not establish Gatekeeper,
    notarization or online certificate-revocation acceptance.
    """
    if sys.platform != 'darwin':
        raise PublisherTrustError('Developer ID verification requires macOS')
    requirement = developer_id_requirement(policy)
    path = Path(executable)
    if not path.is_absolute() or '..' in path.parts:
        raise PublisherTrustError('Publisher verification requires an absolute candidate path')
    before = _executable_snapshot(path)
    _run_codesign(['--verify', '--deep', '--strict', '--all-architectures',
                   '-R', '=' + requirement, str(path)])
    result = _run_codesign(['--display', '--verbose=4', str(path)])
    _metadata_identity(result.stdout + b'\n' + result.stderr, policy)
    if _executable_snapshot(path) != before:
        raise PublisherTrustError('Executable changed during publisher verification')
    return PublisherIdentity(policy.team_identifier, policy.signing_identifier,
                             policy.policy_sha256, before[1])


def require_same_publisher(previous: PublisherIdentity, candidate: PublisherIdentity) -> None:
    """Prevent implicit publisher migration on either upgrade or rollback.

    Both identities must come from fresh verification under the currently
    authenticated policy; deserializing an old receipt is not verification.
    Content/version compatibility and rollback policy are the caller's job.
    """
    if type(previous) is not PublisherIdentity or type(candidate) is not PublisherIdentity:
        raise PublisherTrustError('Freshly verified publisher identities are required')
    if ((previous.team_identifier, previous.signing_identifier)
            != (candidate.team_identifier, candidate.signing_identifier)):
        raise PublisherTrustError('Changing the helper publisher requires a separate trust migration')
