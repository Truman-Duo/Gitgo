"""Identity of trusted installed execution code, never an authority grant.

Runtime upgrades deliberately retire old approvals. Enumerate the installed
backend rather than maintaining a tool-specific list of transitive imports.
Tool definitions and optional native artifacts are bound separately by their
registration. Changing a mechanism *instance* does not change installed code.
"""
from dataclasses import dataclass
import hashlib
import importlib.util
import marshal
import os
from pathlib import Path
import pkgutil
import stat
import sys
_frozen_digest = None


def _source_digest(path, before=None):
    # Windows directory-entry metadata can omit file identity (st_ino=0).
    before = path.stat() if before is None or not before.st_ino else before
    signature = (before.st_dev, before.st_ino, before.st_size,
                 before.st_mtime_ns, before.st_ctime_ns)
    body = path.read_bytes()
    after = path.stat()
    if signature != (after.st_dev, after.st_ino, after.st_size,
                     after.st_mtime_ns, after.st_ctime_ns):
        raise RuntimeError("Installed execution code changed while identifying it")
    return hashlib.sha256(body).hexdigest()


def _source_files(root):
    """Use directory-entry metadata; never traverse a reparse point."""
    pending = [root]
    result = []
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.name == "__pycache__":
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise RuntimeError("Installed backend contains a reparse point")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif entry.name.endswith(".py"):
                    if not info.st_ino:
                        info = Path(entry.path).stat()
                    if info.st_nlink != 1:
                        raise RuntimeError("Installed backend source is multiply linked")
                    result.append((Path(entry.path), info))
    return sorted(result)


def runtime_implementation_digest():
    """Fail closed if installed modules cannot be identified, source or frozen.

No environment variable or model-selected manifest supplies module names.
    Source bytes are reread: timestamps alone cannot prove unchanged code.
    Catalog comparisons share one observation. Frozen code is read from
the actual import loader, not from a source-version label.
"""
    global _frozen_digest
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(b"gitgo.runtime-implementation.v2\0")
    if not getattr(sys, "frozen", False):
        files = _source_files(root)
        if not files:
            raise RuntimeError("Installed backend source is unavailable")
        for path, info in files:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(_source_digest(path, info).encode())
    else:
        if _frozen_digest is not None:
            return _frozen_digest
        import backend
        names = {"backend"}
        def unavailable(name):
            raise RuntimeError("Cannot enumerate installed execution package: " + name)
        names.update(item.name for item in pkgutil.walk_packages(
            backend.__path__, "backend.", onerror=unavailable))
        if len(names) < 2:
            raise RuntimeError("Installed backend module catalog is unavailable")
        for name in sorted(names):
            spec = importlib.util.find_spec(name)
            if spec is None or spec.loader is None:
                raise RuntimeError("Cannot identify installed execution module: " + name)
            code = spec.loader.get_code(name) if hasattr(spec.loader, "get_code") else None
            digest.update(name.encode())
            if code is not None:
                digest.update(hashlib.sha256(marshal.dumps(code)).digest())
            elif spec.origin and Path(spec.origin).is_file():
                digest.update(_source_digest(Path(spec.origin)).encode())
            else:
                raise RuntimeError("Cannot identify installed execution module: " + name)
    if sys.platform in {'win32', 'linux', 'darwin'}:
        from backend.core.native_sandbox import implementation_identity
        import json
        digest.update(json.dumps(implementation_identity(), sort_keys=True,
                                 separators=(',', ':')).encode('utf8'))
    value = digest.hexdigest()
    if getattr(sys, "frozen", False):
        _frozen_digest = value
    return value


@dataclass(frozen=True)
class ImplementationArtifact:
    """Trusted registration of a native helper/library, including its ABI.

This binds approvals to a binary; it is not a launcher. A native adapter must
also pin and verify the artifact actually launched and retain its ownership.
There is deliberately no constructor from model JSON or discovery metadata.
"""
    name: str
    path: Path
    abi_version: int = 1

    def __post_init__(self):
        if not self.name or type(self.abi_version) is not int or self.abi_version < 1:
            raise ValueError("Invalid native implementation identity")
        path = Path(self.path)
        if not path.is_absolute():
            raise ValueError("Native implementation path must be absolute")
        object.__setattr__(self, "path", path)

    def identity(self):
        from backend.core.sandbox_read import ReadRoot
        with ReadRoot(self.path.parent) as root:
            with root.open(self.path.name) as opened:
                value, size = opened.sha256()
        return {"name": self.name, "path": str(self.path), "abi_version": self.abi_version,
                "sha256": value, "size": size}
