"""Shared byte replacement primitive, not an authorization or business service."""
import os
import tempfile
from pathlib import Path


def atomic_replace_bytes(path: Path, data: bytes) -> None:
    """Caller supplies its validated resource; preserve existing file semantics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd,"wb") as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,str(path))
    except Exception:
        try:os.unlink(temporary)
        except OSError:pass
        raise
