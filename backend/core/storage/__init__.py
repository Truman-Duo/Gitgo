"""Durable storage foundation for Gitgo.

All SQLite access is deliberately kept behind :class:`StorageRuntime`.  Other
modules must use this facade instead of opening database connections directly.
"""

from .models import (
    StorageBlocked,
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
    release_storage,
    unbind_storage,
)
from .scope import RepositoryScope, assess_repository_scope

__all__ = [
    "RepositoryScope",
    "StorageBlocked",
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
    "release_storage",
    "unbind_storage",
    "assess_repository_scope",
    "resolve_storage_paths",
    "resolve_existing_storage_paths",
    "read_project_list_status",
]
