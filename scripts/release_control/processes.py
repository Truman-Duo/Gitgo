"""Use Gitgo's existing owned process lifetime for release commands too."""
from contextlib import contextmanager

from backend.core.process_control import spawn_managed, finish_scope, terminate_tree, creation_flags


@contextmanager
def owned(argv, **options):
    options.setdefault('creationflags', creation_flags())
    proc = spawn_managed(argv, **options)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            terminate_tree(proc)
        else:
            finish_scope(proc)
        if not getattr(proc, '_gitgo_cleanup', {}).get('complete'):
            raise RuntimeError('Release command cleanup is unverified; refusing to advance')
