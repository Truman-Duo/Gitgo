"""Bounded object-only Git read prototype; NOT the production Git backend.

The Host supplies an already authorized Rust ReadRoot for the object directory.
The explicit macos_broker_client adapter carries this bounded snapshot through
the existing project grant and invocation journal; formal Git remains gated.
Only object bytes enter a disposable repository. No source config, refs, index,
hooks, alternates, credentials or worktree paths reach Git. Commands use full
SHA-1 object IDs; this is not a coherent snapshot of a live repository or an
authorization to publish changes. The caller must launch through the existing
UID/Seatbelt execution owner and withhold results until confirmed retirement.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import base64
import os
from pathlib import Path
import re

from backend.core.loop.git_execution import GitBoundaryError
from backend.core.sandbox_read import MAX_READ_BYTES

MAX_FILES = 2048
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 4096
_OID = re.compile(r'[0-9a-f]{40}')


def refused(message):
    raise GitBoundaryError('GIT_OBJECT_VIEW_UNSUPPORTED', message)


def _matches(fd, identity):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino) == (identity.volume, identity.file_id)


def object_name(name):
    return type(name) is str and re.fullmatch(
        r'(?:[0-9a-f]{2}/[0-9a-f]{38}|pack/pack-[0-9a-f]{40}\.(?:pack|idx|rev|bitmap))', name) is not None


def object_digest(files):
    digest = hashlib.sha256(b'gitgo-object-view-v1\0')
    for name, raw in files:
        encoded = name.encode('ascii')
        digest.update(len(encoded).to_bytes(4, 'big') + encoded)
        digest.update(len(raw).to_bytes(8, 'big') + raw)
    return digest.hexdigest()


@dataclass(frozen=True)
class ObjectView:
    files: tuple[tuple[str, bytes], ...]
    sha256: str

    def __post_init__(self):
        if (type(self.files) is not tuple or len(self.files) > MAX_FILES or
                any(type(item) is not tuple or len(item) != 2 or not object_name(item[0]) or
                    type(item[1]) is not bytes for item in self.files)):
            refused('Invalid object view')
        names = [name for name, _ in self.files]
        if (names != sorted(set(names)) or sum(len(raw) for _, raw in self.files) >= MAX_BYTES or
                any(len(raw) > MAX_READ_BYTES for _, raw in self.files) or
                self.sha256 != object_digest(self.files)):
            refused('Invalid object view bounds or digest')

    def materialize(self, destination):
        """Fresh private destination, before the task UID is allowed to run."""
        destination = Path(destination)
        # Parent ownership/separation belongs to the existing execution trial.
        # Never overwrite an existing directory, including an empty one/link.
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        (destination / 'objects').mkdir(mode=0o755)
        (destination / 'refs').mkdir(mode=0o755)
        (destination / 'objects').chmod(0o755)
        (destination / 'refs').chmod(0o755)
        (destination / 'HEAD').write_bytes(b'ref: refs/heads/gitgo-unused\n')
        (destination / 'config').write_bytes(b'[core]\nrepositoryformatversion = 0\nbare = true\n')
        for name, raw in self.files:
            target = destination / 'objects' / name
            target.parent.mkdir(mode=0o755, exist_ok=True)
            target.parent.chmod(0o755)
            with target.open('xb') as stream:
                stream.write(raw)
            target.chmod(0o444)
        (destination / 'HEAD').chmod(0o444)
        (destination / 'config').chmod(0o444)


def capture_objects(root, directory):
    """Copy a bounded object store through live Rust file owners.

    Descriptor enumeration supplies *names*, never content authority. Every
    byte comes from the Rust root's bounded reader (no symlinks/hardlinks/mounts).
    Bucket replacement can change which names are proposed, but cannot change
    the content root. This intentionally makes no atomic multi-file claim.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    # open_directory rejects links in ancestors too; no resolve() of user paths.
    from backend.core.macos_broker_protocol import open_directory
    fd = open_directory(Path(directory))
    files, used, entries = [], 0, 0
    try:
        if not _matches(fd, root.identity):
            refused('Object enumeration and the authorized native root differ')

        def names(parent):
            nonlocal entries
            with os.scandir(parent) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        refused('Object directory exceeds the entry budget')
                    yield entry.name

        for bucket in names(fd):
            if bucket not in {'pack', 'info'} and re.fullmatch('[0-9a-f]{2}', bucket) is None:
                refused('Only SHA-1 loose objects and ordinary packs are supported')
            child = os.open(bucket, flags, dir_fd=fd)
            try:
                if os.fstat(child).st_dev != root.identity.volume:
                    refused('Object enumeration cannot cross a volume')
                for leaf in names(child):
                    name = bucket + '/' + leaf
                    valid = (re.fullmatch(r'pack-[0-9a-f]{40}\.(pack|idx|rev|bitmap)', leaf) if bucket == 'pack'
                             else re.fullmatch('[0-9a-f]{38}', leaf) if bucket != 'info' else None)
                    if valid is None:
                        refused('Alternates, promisor stores and auxiliary object metadata are unsupported')
                    if len(files) >= MAX_FILES:
                        refused('Object view exceeds the file budget')
                    with root.open(name) as opened:
                        raw = opened.read_bytes(max_bytes=min(MAX_READ_BYTES, MAX_BYTES - used))
                    used += len(raw)
                    if used >= MAX_BYTES:
                        refused('Object view exceeds the byte budget')
                    files.append((name, raw))
            finally:
                os.close(child)
    finally:
        os.close(fd)
    files.sort()
    return ObjectView(tuple(files), object_digest(files))


def view_to_wire(view):
    """Use the existing, smaller broker budget (128 files / 4 MiB)."""
    value = dict(files=[[name, base64.b64encode(raw).decode('ascii'), 0o644]
                        for name, raw in view.files], directories=[])
    view_from_wire(value, view.sha256)
    return value


def view_from_wire(value, digest):
    from backend.core.macos_broker_protocol import validate_image
    files, dirs = validate_image(value)
    if dirs or any(mode != 0o644 for _, _, mode in files):
        refused('Object transport accepts only ordinary object files')
    return ObjectView(tuple((name, raw) for name, raw, _ in files), digest)


def valid_exit_code(query, code):
    """Exit 1 is an ancestry answer only for the exact predicate command."""
    read_command('/private/tmp/gitgo-query-validation', query)
    return type(code) is int and (code == 0 or
        code == 1 and list(query[:2]) == ['merge-base', '--is-ancestor'])


@dataclass(frozen=True)
class QueryResult:
    stdout: str
    returncode: int


def validate_output(query, output, exit_code=0):
    read_command('/private/tmp/gitgo-query-validation', query)
    query = list(query)
    if not valid_exit_code(query, exit_code):
        refused('Git process failure is not an object query answer')
    pattern = (r'' if query[:2] == ['merge-base', '--is-ancestor'] else
               r'(?:blob|tree|commit|tag)\n' if query[:2] == ['cat-file', '-t'] else
               r'(?:0|[1-9][0-9]{0,19})\n' if query[:2] == ['cat-file', '-s'] else
               r'[0-9a-f]{40}\n')
    if type(output) is not str or re.fullmatch(pattern, output) is None:
        refused('Malformed or excessive object query output')
    return output


def read_command(destination, query):
    """No free-form revisions, pathspecs, stdin, options or executable override.

    Initial text-only plumbing: object type/size and two-commit merge base.
    Exact ancestry predicates also preserve their 0/1 answer. Blob/batch output,
    history, worktrees, writes and transports are not enabled.
    """
    if type(query) not in (tuple, list) or any(type(p) is not str for p in query):
        refused('Expected a fixed object query')
    valid = (len(query) == 3 and
             ((query[0] == 'cat-file' and query[1] in {'-t', '-s'} and _OID.fullmatch(query[2]))
              or (query[0] == 'merge-base' and all(_OID.fullmatch(p) for p in query[1:]))))
    valid = valid or (len(query) == 4 and list(query[:2]) == ['merge-base', '--is-ancestor']
                      and all(_OID.fullmatch(p) for p in query[2:]))
    if not valid:
        refused('Only exact SHA-1 object type/size and merge-base queries are supported')
    destination = Path(destination)
    if not destination.is_absolute() or '..' in destination.parts:
        refused('Expected an absolute private object view')
    return ['/usr/bin/git', '--no-pager', '--no-lazy-fetch', '--no-replace-objects',
            '--git-dir=' + str(destination),
            '-c', 'safe.directory=' + str(destination),
            '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'protocol.allow=never', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
            *query]


def accepted_output(proc, stdout):
    """Never turn a successful leader exit into successful unretired Git work."""
    if proc.returncode != 0 or getattr(proc, '_gitgo_cleanup', {}).get('complete') is not True:
        raise GitBoundaryError('GIT_READ_UNCONFIRMED', 'Git result requires success and confirmed UID cleanup')
    return stdout
