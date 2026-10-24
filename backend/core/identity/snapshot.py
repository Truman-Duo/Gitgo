"""Tool-memory snapshot compatibility facade; archives are never instructions.

New snapshots use the backup node's canonical storage. Legacy directory snapshots
remain read-only import sources. Restoration is delegated to the file service.
"""
from pathlib import Path
from .memory_archive import (SOURCES, snapshot, list_archives, capture_sources,
                             _ordinary, MemoryArchiveError, MAX_SNAPSHOTS_PER_SOURCE)

MEMORY_SOURCES = list(SOURCES)
_MAX_SNAPSHOTS = MAX_SNAPSHOTS_PER_SOURCE


def snapshot_tool_memories(workspace_path, backup_path, project) -> dict:
    return snapshot(workspace_path, backup_path)


def legacy_snapshots(backup_path):
    root = Path(backup_path).absolute() / ".gitgo" / "memories"
    try:
        _ordinary(root)
    except FileNotFoundError:
        return []
    from backend.core.sandbox_read import ReadRoot
    result = []
    with ReadRoot(root):
        for index, item in enumerate(root.iterdir()):
            if index >= 512: raise MemoryArchiveError("Legacy memory index exceeds its bound")
            for source in SOURCES:
                if item.name.startswith(source + "_"):
                    info = _ordinary(item)
                    import stat
                    result.append({"source": source, "timestamp": item.name[len(source)+1:],
                        "path":str(item), "is_dir":stat.S_ISDIR(info.st_mode),
                        "format":"legacy", "sequence":0})
    return sorted(result,key=lambda row:row["timestamp"],reverse=True)


def list_memory_snapshots(backup_path, workspace_path=None) -> list[dict]:
    return list_archives(backup_path, workspace_path) + legacy_snapshots(backup_path)


def read_legacy_archive(backup, row, workspace):
    root = Path(backup).absolute() / ".gitgo" / "memories"
    # A listing row is not a capability: derive the literal source path again.
    name = row["source"] + "_" + row["timestamp"]
    from backend.core.sandbox_read import relative_parts
    if len(relative_parts(name)) != 1: raise MemoryArchiveError("Invalid legacy snapshot name")
    captured = capture_sources(root, source_paths={row["source"]:name})
    if row["source"] not in captured: raise MemoryArchiveError("Legacy snapshot disappeared")
    return captured[row["source"]]


def restore_tool_memories(backup_path, workspace_path, snapshot_timestamp=None, *,
                          service_args=None, project_name="") -> dict:
    from backend.core.tools.memory_restore_service import restore_memory_files
    return restore_memory_files(backup_path, workspace_path, snapshot_timestamp,
                                service_args=service_args, project_name=project_name)
