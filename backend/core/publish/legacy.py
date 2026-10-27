"""Explicit legacy handoff and a shared writer boundary for old/new paths."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .state import digest, PublishError, identity, read_ledger

_HELD = ContextVar("publish_legacy_writer", default=frozenset())


def record(row):
    get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
    return {"prefix": get("prefix"), "number": get("number"), "message": get("message"),
        "created_at": get("created_at", ""), "source_indices": sorted(get("source_indices", [])),
        **{field: get(field, False) for field in ("synced", "pushed", "is_incoming", "sources_cleared")}}


def key(row):
    value = record(row)
    return digest({field: value[field] for field in ("prefix", "number", "created_at")})


@contextmanager
def writer(workspace):
    name = identity(workspace)
    if name in _HELD.get():
        yield
        return
    from backend.core.storage import get_storage
    from backend.core.loop.publish_recovery import item_lease
    with item_lease(get_storage(workspace), "legacy-handoff"):
        token = _HELD.set(_HELD.get() | {name})
        try:
            yield
        finally:
            _HELD.reset(token)


def validate_saved_state(workspace, data):
    ledger, _ = read_ledger(workspace)
    mappings = (ledger or {}).get("legacy_migrations", {})
    if not mappings:
        return
    values = {key(row): digest(record(row)) for row in data.get("formal_commits", [])}
    for old, mapping in mappings.items():
        if values.get(old) != mapping["record_digest"]:
            raise PublishError("PUBLISH_LEGACY_MIGRATED")


def legacy_mutation(method):
    """Prevent old commands from affecting a handed-off record, before Git."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with writer(self.workspace_path):
            ledger, _ = read_ledger(self.workspace_path)
            mappings = (ledger or {}).get("legacy_migrations", {})
            if method.__name__ == "step_push":
                rows = [row for row in self.formal_commits if row.synced and not row.pushed]
            elif method.__name__ == "step_sync":
                index = args[0] if args else kwargs.get("formal_index")
                rows = self.formal_commits[index:index + 1] if type(index) is int else [row for row in self.formal_commits if not row.synced][:1]
            else:
                index = args[0] if args else kwargs.get("index", -1)
                rows = self.formal_commits[index:index + 1] if type(index) is int and index >= 0 else []
            if any(key(row) in mappings for row in rows):
                raise PublishError("PUBLISH_LEGACY_MIGRATED")
            return method(self, *args, **kwargs)
    return guarded
