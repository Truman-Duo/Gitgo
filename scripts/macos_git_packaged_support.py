"""Harness-only selection and evidence for the immutable installed helper."""
from pathlib import Path
import importlib
import sys

HELPER=None
MODULES=('macos_broker_client','macos_broker_service','macos_git_read','macos_git_write','macos_git_checkout','macos_git_compare','macos_worktree','macos_worktree_index','macos_worktree_result','macos_worktree_seal','macos_worktree_runtime','macos_worktree_finish','macos_worktree_resume','macos_promotion_snapshot','macos_promotion_write','macos_promotion','loop.git_macos',
         'sandbox_macos','macos_execution_service','macos_identity','macos_coalition',
         'macos_resource_guard','storage.native_invocations')


def select_helper(value):
    global HELPER
    if value is None:
        HELPER=None
        return
    from backend.core.macos_helper_install import BASE
    path=Path(value).absolute()
    if path.parent.parent.parent!=BASE or path.parent.name!='runtime' or path.name!='gitgo-host' or not path.is_file():
        raise ValueError('Expected the installed sealed helper')
    HELPER=path


def command(script,*args):
    executable=str(HELPER) if HELPER else sys.executable
    prefix=[executable,'--gitgo-internal-role','python'] if HELPER or getattr(sys,'frozen',False) else [executable]
    return [*prefix,'-B','-I',str(script),*map(str,args)]


def evidence():
    frozen=bool(getattr(sys,'frozen',False))
    root=Path(sys._MEIPASS).resolve() if frozen else None
    files={name:str(Path(importlib.import_module('backend.core.'+name).__file__).resolve()) for name in MODULES}
    embedded=frozen and all(Path(path).is_relative_to(root) for path in files.values())
    if frozen and not embedded:raise RuntimeError('Frozen acceptance loaded source backend modules')
    return dict(frozen=frozen,executable=sys.executable,embedded_backend=embedded,modules=files)


def task_evidence(lease,state,helper):
    """Exact supervisor entry, not merely a frozen flag in the client."""
    import plistlib
    from backend.core.macos_identity import bounded_read
    job=plistlib.loads(bounded_read(Path(lease)/'job.plist',0,True))
    return job['ProgramArguments']==[str(helper),'--gitgo-internal-role','macos-execution-service',str(lease)]
