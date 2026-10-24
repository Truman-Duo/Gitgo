"""Bounded object descriptions for the trusted native read protocol.

These values do not grant access. The Host must retain the corresponding pins;
the child must still satisfy its native OS policy before reading any bytes.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import sys

from backend.core.sandbox_read import ObjectIdentity, ReadBoundaryError, ReadRoot, relative_parts

_EXPECTED = ContextVar('approved_read', default=None)


@dataclass(frozen=True)
class ReadPlan:
    workspace: str
    root: str
    relative: str
    workspace_identity: ObjectIdentity
    root_identity: ObjectIdentity
    file_identity: ObjectIdentity

    def to_dict(self):
        def identity(value):
            return [value.platform, value.volume, value.file_id]
        return {'version': 1, 'workspace': self.workspace, 'root': self.root,
                'relative': self.relative, 'workspace_identity': identity(self.workspace_identity),
                'root_identity': identity(self.root_identity), 'file_identity': identity(self.file_identity)}

    @classmethod
    def from_dict(cls, value):
        fields = {'version', 'workspace', 'root', 'relative', 'workspace_identity',
                  'root_identity', 'file_identity'}
        if type(value) is not dict or set(value) != fields or type(value['version']) is not int or value['version'] != 1:
            raise ReadBoundaryError('Invalid approved read schema')
        for key in ('workspace', 'root', 'relative'):
            if type(value[key]) is not str or not 0 < len(value[key]) <= 32768 or '\0' in value[key]:
                raise ReadBoundaryError('Invalid approved read path')
        for key in ('workspace', 'root'):
            path = Path(value[key])
            if not path.is_absolute() or '..' in path.parts or path == Path(path.anchor):
                raise ReadBoundaryError('Invalid approved read root')
        relative_parts(value['relative'])
        def identity(key):
            raw = value[key]
            if (type(raw) is not list or len(raw) != 3 or raw[0] != sys.platform
                    or sys.platform not in {'linux', 'win32', 'darwin'}
                    or any(type(v) is not int or not 0 <= v < 2**128 for v in raw[1:])):
                raise ReadBoundaryError('Invalid approved object identity')
            return ObjectIdentity(*raw)
        return cls(value['workspace'], value['root'], value['relative'],
                   identity('workspace_identity'), identity('root_identity'), identity('file_identity'))

    def check_arguments(self, args):
        from backend.core.tools.workspace_tools import _read_target
        workspace, _, root, relative = _read_target(args)
        if (workspace, root, Path(relative)) != (Path(self.workspace), Path(self.root), Path(self.relative)):
            raise ReadBoundaryError('Approved file selection changed')


@contextmanager
def approved_read_scope(payload, args):
    plan = ReadPlan.from_dict(payload) if payload is not None else None
    if plan is None:
        # Explicitly clear nested scopes; tool arguments cannot provide plans.
        token = _EXPECTED.set(None)
        try:
            yield
        finally:
            _EXPECTED.reset(token)
        return
    plan.check_arguments(args)
    with ReadRoot(Path(plan.workspace)) as workspace:
        if workspace.identity != plan.workspace_identity:
            raise ReadBoundaryError('Approved workspace was replaced')
        token = _EXPECTED.set(plan)
        try:
            yield
        finally:
            _EXPECTED.reset(token)


@contextmanager
def open_planned_read(root, relative):
    with ReadRoot(root) as acquired, acquired.open(relative) as opened:
        plan = _EXPECTED.get()
        if plan is not None and (Path(root) != Path(plan.root)
                or Path(relative) != Path(plan.relative)
                or acquired.identity != plan.root_identity or opened.identity != plan.file_identity):
            raise ReadBoundaryError('Approved file object was replaced')
        yield opened
