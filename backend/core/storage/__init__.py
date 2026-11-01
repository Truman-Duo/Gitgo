"""Durable storage foundation for Gitgo.

All SQLite access is deliberately kept behind :class:`StorageRuntime`.  Other
modules must use this facade instead of opening database connections directly.
"""

# Callers classify database failures through the storage facade, without
# importing or opening the underlying SQLite implementation themselves.
from sqlite3 import Error as StorageDatabaseError

from .models import (
    StorageBlocked,
    StorageCommitUnknown,
    StorageCorruptionDetected,
    StorageReferenceMissing,
    StorageRuntimeUnsupported,
    StorageHealth,
    StorageHealthLevel,
    StoragePolicy,
)
from .paths import StoragePaths, resolve_existing_storage_paths, resolve_storage_paths
from .runtime import StorageRuntime, read_project_list_status
from .bindings import (
    bind_storage,
    close_owned_storage,
    get_storage,
    peek_storage,
    release_storage,
    unbind_storage,
)
from .scope import RepositoryScope, assess_repository_scope

__all__ = [
    "RepositoryScope",
    "StorageBlocked",
    "StorageDatabaseError",
    "StorageCommitUnknown",
    "StorageCorruptionDetected",
    "StorageReferenceMissing",
    "StorageRuntimeUnsupported",
    "StorageHealth",
    "StorageHealthLevel",
    "StoragePaths",
    "StoragePolicy",
    "StorageRuntime",
    "bind_storage",
    "close_owned_storage",
    "get_storage",
    "peek_storage",
    "release_storage",
    "unbind_storage",
    "assess_repository_scope",
    "resolve_storage_paths",
    "resolve_existing_storage_paths",
    "read_project_list_status",
]
