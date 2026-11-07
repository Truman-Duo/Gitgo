"""Use Gitgo's existing owned process lifetime for release commands too."""
from contextlib import contextmanager
import sys

from backend.core.process_control import spawn_managed, finish_scope, terminate_tree, creation_flags


@contextmanager
def owned(argv, *, single_process=False, **options):
    if sys.platform == 'darwin':
        if not single_process:
            raise RuntimeError('macOS general release-command descendant retirement is unavailable; use a Windows/Linux controller. Only explicitly single-process checks can use the existing forkless scope.')
        options['macos_forkless'] = True
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
