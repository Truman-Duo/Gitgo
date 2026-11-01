"""Fixed product-metadata file I/O; this module grants no model authority.

User-editable YAML remains a config source, not a second session database.
Business effects still require the file service. Locks coordinate participants;
this is not an atomic filesystem transaction against arbitrary external writers.
"""
import hashlib
import errno
import time
from contextlib import ExitStack
from pathlib import Path
from backend.core.filesystem import atomic_replace_bytes
from backend.core.sandbox_read import ReadRoot,relative_parts
from .maintenance import StorageLease, StorageMaintenanceActive

MAX_METADATA_BYTES=2*1024*1024


class MetadataFileChanged(RuntimeError):
    code="METADATA_FILE_CHANGED"


class MetadataCommitUnknown(RuntimeError):
    code="METADATA_COMMIT_UNKNOWN"


class MetadataFileBusy(RuntimeError):
    code="METADATA_FILE_BUSY"


def _write_lease(lock, filename, timeout):
    """Wait only for an actual file-lock conflict, before any write begins."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return StorageLease(lock, exclusive=True)
        except StorageMaintenanceActive as exc:
            cause = exc.__cause__
            contention = (getattr(cause, "winerror", None) == 33
                          or (getattr(cause, "winerror", None) is None
                              and getattr(cause, "errno", None) in {errno.EAGAIN, errno.EWOULDBLOCK}))
            if not contention:
                raise cause if cause is not None else exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MetadataFileBusy(f"Metadata file is busy: {filename}; retry this metadata operation later") from exc
            time.sleep(min(0.02, remaining))


def _name(filename):
    if len(relative_parts(filename))!=1:raise ValueError("A fixed metadata filename is required")
    return filename


def read_metadata_bytes(workspace,filename,*,max_bytes=MAX_METADATA_BYTES):
    filename=_name(filename)
    with ReadRoot(Path(workspace).absolute()) as root:
        try:
            with root.open(".gitgo/"+filename) as opened:return opened.read_bytes(max_bytes=max_bytes)
        except FileNotFoundError:return None


def write_metadata_bytes(workspace,filename,content,*,expected_sha256=None,max_bytes=MAX_METADATA_BYTES,lock_timeout=0):
    filename=_name(filename);root=Path(workspace).absolute()
    if not isinstance(lock_timeout, (int, float)) or not 0 <= lock_timeout <= 5:
        raise ValueError("Metadata lock timeout must be between 0 and 5 seconds")
    if len(content)>max_bytes:raise ValueError("Product metadata exceeds its bounded size")
    with ExitStack() as owners:
        owners.enter_context(ReadRoot(root))
        metadata=root/".gitgo";metadata.mkdir(exist_ok=True)
        acquired=owners.enter_context(ReadRoot(metadata))
        lock_root = metadata / "metadata-locks"
        lock_root.mkdir(exist_ok=True)
        owners.enter_context(ReadRoot(lock_root))
        lock=lock_root/hashlib.sha256(filename.casefold().encode()).hexdigest()
        lock.mkdir(exist_ok=True)
        owners.enter_context(ReadRoot(lock))
        owners.enter_context(_write_lease(lock, filename, lock_timeout))
        try:
            with acquired.open(filename) as opened:previous=opened.read_bytes(max_bytes=max_bytes)
        except FileNotFoundError:previous=None
        actual=hashlib.sha256(previous).hexdigest() if previous is not None else ""
        if expected_sha256 is not None and expected_sha256!=actual:
            raise MetadataFileChanged("Product metadata changed; reload its current version")
        try:atomic_replace_bytes(metadata/filename,content)
        except OSError as exc:
            raise MetadataCommitUnknown("Metadata replacement outcome needs inspection before any retry") from exc
    return hashlib.sha256(content).hexdigest()
