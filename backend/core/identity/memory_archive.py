"""Immutable tool-memory archives on the configured backup node's existing store.

Archives are data, never instructions or executable modules. Legacy snapshot
files remain read-only import sources; this module does not replace directories.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import stat
import uuid
from datetime import datetime
from pathlib import Path

from backend.core.sandbox_read import ReadRoot, ReadBoundaryError, relative_parts
from backend.core.storage import get_storage, peek_storage, resolve_existing_storage_paths
from backend.core.storage.maintenance import StorageLease

SOURCES = (".claude", ".codex", ".codebuddy")
MAX_SNAPSHOTS_PER_SOURCE = 5
MAX_FILES = 128
MAX_BYTES = 16 * 1024 * 1024
_NAMESPACE = "identity.memory_archives.v2"


class MemoryArchiveError(RuntimeError):
    code = "MEMORY_ARCHIVE_UNAVAILABLE"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")


def workspace_key(workspace):
    return hashlib.sha256(os.path.normcase(str(Path(workspace).resolve())).encode("utf8")).hexdigest()


def _ordinary(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ReadBoundaryError("Tool-memory archives do not follow links or reparse points")
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise ReadBoundaryError("Tool-memory archives require ordinary files and directories")
    return info


def capture_sources(workspace, *, source_paths=None):
    """Bounded descriptor reads; no shallow size/mtime equality shortcut."""
    workspace = Path(workspace).absolute()
    source_paths = source_paths or {source: source for source in SOURCES}
    if any(source not in SOURCES for source in source_paths):
        raise MemoryArchiveError("Unknown tool-memory source")
    captured = {}; total_bytes = 0; total_files = 0; visited = 0
    with ReadRoot(workspace) as acquired:
        for source, source_path in source_paths.items():
            relative_parts(source_path)
            path = workspace / source_path
            try:
                info = _ordinary(path)
            except FileNotFoundError:
                continue
            directory = stat.S_ISDIR(info.st_mode)
            names = []
            if directory:
                pending = [path]
                while pending:
                    parent = pending.pop(); _ordinary(parent)
                    visited += 1
                    if visited > MAX_FILES * 2 or len(parent.relative_to(workspace).parts) > 32:
                        raise MemoryArchiveError("Tool-memory directory inventory exceeds its bound")
                    # Stream entries rather than materializing an unbounded
                    # os.walk directory listing before applying the limit.
                    with os.scandir(parent) as entries:
                        for entry in entries:
                            candidate = Path(entry.path); kind = _ordinary(candidate)
                            if stat.S_ISDIR(kind.st_mode):
                                pending.append(candidate)
                                if len(pending) + visited > MAX_FILES * 2:
                                    raise MemoryArchiveError("Tool-memory directory inventory exceeds its bound")
                            else:
                                names.append(candidate.relative_to(workspace).as_posix())
                                if len(names) + total_files > MAX_FILES:
                                    raise MemoryArchiveError("Tool-memory file inventory exceeds its bound")
            else:
                names = [source_path]
            content = {}
            for name in sorted(names):
                with acquired.open(name) as opened:
                    raw = opened.read_bytes(max_bytes=MAX_BYTES - total_bytes)
                total_bytes += len(raw); total_files += 1
                if total_files > MAX_FILES or total_bytes > MAX_BYTES:
                    raise MemoryArchiveError("Tool-memory content exceeds its bound")
                inner = Path(name).relative_to(Path(source_path))
                from backend.core.tools.file_operations import protected_metadata_part
                if any(protected_metadata_part(part) for part in inner.parts):
                    raise MemoryArchiveError("Tool-memory archives cannot import nested Gitgo or Git metadata")
                target = (Path(source) / inner).as_posix()
                content[target] = base64.b64encode(raw).decode("ascii")
            captured[source] = {"version": 1, "workspace": str(workspace.resolve()),
                                "source": source, "is_dir": directory, "files": content}
    return captured


def _store_if_present(backup):
    store = peek_storage(backup)
    if store is not None:
        return store
    paths = resolve_existing_storage_paths(backup)
    return get_storage(backup) if paths and paths.state_db.is_file() else None


def _namespace(backup):
    return _NAMESPACE + ":" + workspace_key(backup)


def _rows(store, namespace, prefix):
    after = ""; rows = []
    while True:
        page = store.list_state_refs(namespace, prefix=prefix, after=after, limit=100)
        rows.extend(page)
        if len(rows) > 2000:
            raise MemoryArchiveError("Tool-memory index exceeds its bounded listing")
        if len(page) < 100: return rows
        after = page[-1]["key"]


def snapshot(workspace, backup):
    """Publish immutable archives; the backup node owns retention and quotas."""
    workspace = Path(workspace).resolve(); backup = Path(backup).resolve()
    if not backup.is_dir() or any(backup.is_relative_to(workspace/source) for source in SOURCES):
        raise MemoryArchiveError("A local backup directory outside tool-memory sources is required")
    captured = capture_sources(workspace)
    if not captured:
        return {"snapped": [], "timestamp": "", "effect_state": "not_committed",
                "execution_state": "completed", "status": "no_sources"}
    store = get_storage(backup); owner = workspace_key(workspace)
    namespace = _namespace(backup)
    lock = store.paths.project_root / "memory-archive-lock"; lock.mkdir(exist_ok=True)
    with StorageLease(lock, exclusive=True):
        # Sequence is derived from retained heads. Unique timestamps prevent
        # reuse after a partial publish; sequence orders same-clock snapshots.
        rows = _rows(store, namespace, "snapshot:" + owner + ":")
        sequence = max((int(row["key"].split(":")[3]) for row in rows), default=0) + 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:12]
        published = []
        try:
            for source, archive in captured.items():
                archive = {**archive, "backup": str(backup)}
                ref = store.put_blob(encoded(archive), media_type="application/vnd.gitgo.tool-memory+json")
                key = f"snapshot:{owner}:{source}:{sequence:020d}:{timestamp}:" + ("d" if archive["is_dir"] else "f")
                if not store.compare_state_ref(namespace, key, ref, expected_ref=None):
                    raise MemoryArchiveError("Tool-memory snapshot identity already exists")
                published.append(source)
        except Exception as exc:
            from backend.core.storage import StorageCommitUnknown
            if isinstance(exc, StorageCommitUnknown): raise
            if published:
                return {"error": "MEMORY_ARCHIVE_UNAVAILABLE", "snapped": published,
                        "timestamp": timestamp, "effect_state": "partial", "execution_state": "completed"}
            raise
        warnings = []
        try:
            for source in SOURCES:
                entries = _rows(store, namespace, f"snapshot:{owner}:{source}:")
                for row in sorted(entries, key=lambda item:item["key"], reverse=True)[MAX_SNAPSHOTS_PER_SOURCE:]:
                    store.release_state_ref(namespace,row["key"],expected_ref=row["value_ref"])
        except Exception as exc:
            warnings.append({"code": "MEMORY_RETENTION_PENDING", "error_type": type(exc).__name__,
                             "message": "Snapshot committed; retention needs inspection. Do not repeat snapshot creation."})
        return {"snapped": published, "timestamp": timestamp, "dest": str(store.paths.project_root),
                "effect_state": "committed", "execution_state": "completed", "warnings": warnings,
                "consistency": "bounded_per_file_reads"}


def list_archives(backup, workspace=None):
    store = _store_if_present(backup)
    if store is None: return []
    prefix = "snapshot:" + (workspace_key(workspace) + ":" if workspace is not None else "")
    result = []
    for row in _rows(store, _namespace(backup), prefix):
        _, owner, source, sequence, timestamp, kind = row["key"].split(":",5)
        if source not in SOURCES or not sequence.isdigit() or kind not in {"d", "f"}:
            raise MemoryArchiveError("Invalid tool-memory index entry")
        result.append({"source":source,"timestamp":timestamp,"sequence":int(sequence),
                       "snapshot_ref":row["value_ref"],"path":row["value_ref"],
                       "is_dir":kind == "d","format":"cas-v2","workspace_key":owner})
    return sorted(result,key=lambda item:(item["sequence"],item["timestamp"]),reverse=True)


def read_archive(backup, row, workspace):
    store = _store_if_present(backup)
    if store is None: raise MemoryArchiveError("Tool-memory archive store is unavailable")
    value = json.loads(store.read_blob(row["snapshot_ref"],repair=False))
    if (not isinstance(value,dict) or value.get("version") != 1 or value.get("workspace") != str(Path(workspace).resolve())
            or value.get("backup") != str(Path(backup).resolve())
            or value.get("source") != row["source"] or type(value.get("is_dir")) is not bool
            or not isinstance(value.get("files"),dict) or len(value["files"]) > MAX_FILES):
        raise MemoryArchiveError("Tool-memory archive binding or format is invalid")
    total = 0
    for name, content in value["files"].items():
        parts = relative_parts(name)
        if parts[0] != row["source"]:
            raise MemoryArchiveError("Archive file is outside its tool-memory source")
        raw = base64.b64decode(content,validate=True);total += len(raw)
        if total > MAX_BYTES: raise MemoryArchiveError("Archive content exceeds its bound")
    return value
