"""Fixed product-metadata file I/O; this module grants no model authority.

User-editable YAML remains a config source, not a second session database.
Business effects still require the file service. Locks coordinate participants;
this is not an atomic filesystem transaction against arbitrary external writers.
"""
import hashlib
from contextlib import ExitStack
from pathlib import Path
from backend.core.filesystem import atomic_replace_bytes
from backend.core.sandbox_read import ReadRoot,relative_parts
from .maintenance import StorageLease

MAX_METADATA_BYTES=2*1024*1024


class MetadataFileChanged(RuntimeError):
    code="METADATA_FILE_CHANGED"


class MetadataCommitUnknown(RuntimeError):
    code="METADATA_COMMIT_UNKNOWN"


def _name(filename):
    if len(relative_parts(filename))!=1:raise ValueError("A fixed metadata filename is required")
    return filename


def read_metadata_bytes(workspace,filename,*,max_bytes=MAX_METADATA_BYTES):
    filename=_name(filename)
    with ReadRoot(Path(workspace).absolute()) as root:
        try:
            with root.open(".gitgo/"+filename) as opened:return opened.read_bytes(max_bytes=max_bytes)
        except FileNotFoundError:return None


def write_metadata_bytes(workspace,filename,content,*,expected_sha256=None,max_bytes=MAX_METADATA_BYTES):
    filename=_name(filename);root=Path(workspace).absolute()
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
        owners.enter_context(StorageLease(lock,exclusive=True))
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
