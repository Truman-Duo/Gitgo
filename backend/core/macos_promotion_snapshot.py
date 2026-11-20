"""Read-only promotion precondition against raw, descriptor-bound file bytes.

No repository Git command, filter, hook or worktree write is permitted here.
The actual promotion still needs the existing candidate/approval/write/CAS path.
"""
import hashlib
import os
import re
from backend.core import macos_broker_client as broker,macos_broker_protocol as wire
from backend.core import macos_worktree_index as index
from backend.core.sandbox_read import ReadRoot


def matches(manager,commit,paths,*,versions=None,side=None):
    from backend.core.macos_git_checkout import validate_commit
    validate_commit(commit)
    from itertools import islice
    from backend.core.loop.worktree import WorktreeError
    paths=list(islice(iter(paths),129))
    if len(paths)>128 or any(type(p) is not str for p in paths) or len(set(paths))!=len(paths):return False
    try:
        index.validate_entries([(name,'0'*40,0o644) for name in paths])
        # A persisted prepared effect binds its recorded paths exactly once.
        # Newly guarded paths in an incremental publication still match the commit.
        # Validate before launching a checkout, including the unused side: an
        # incomplete record must never be mistaken for proof of an effect.
        digests=None
        if versions is not None:
            if side not in ('before','after') or type(versions) is not list or len(versions)>len(paths):return False
            digests={}
            for row in versions:
                if type(row) is not dict or set(row)!={'path','before_sha256','after_sha256'}:return False
                name=row['path']
                if type(name) is not str or name not in paths or name in digests:return False
                for key in ('before_sha256','after_sha256'):
                    value=row[key]
                    if type(value) is not str or (value and re.fullmatch(r'[0-9a-f]{64}',value) is None):return False
                digests[name]=row[side+'_sha256']
        if not paths:return True
        binding=broker.project_binding(manager.repo_root)
        from backend.core.loop.native_invocation_scope import current_native_cancellation
        with ReadRoot(manager.repo_root/'.git/objects') as objects:
            image=broker.checkout_git_commit(manager.repo_root,objects,commit,cancellation=current_native_cancellation(manager.repo_root))
        if image.commit!=commit:return False
        expected={name:(data,mode) for name,data,mode in image.files}
        fd=wire.open_directory(manager.repo_root)
        try:
            if wire.directory_identity(fd)!=binding['identity']:return False
            with ReadRoot(manager.repo_root) as root:
                if (root.identity.volume,root.identity.file_id)!=tuple(binding['identity'][:2]):return False
                used=0
                for name in paths:
                    try:actual=index.selected_file(root,fd,name)
                    except FileNotFoundError:actual=None
                    if actual!=expected.get(name):return False
                    if digests is not None and name in digests:
                        digest=hashlib.sha256(actual[0]).hexdigest() if actual is not None else ''
                        if digest!=digests[name]:return False
                    if actual is not None:used+=len(actual[0])
                    if used>4*1024*1024:return False
                return broker.project_binding(manager.repo_root)['identity']==binding['identity']
        finally:os.close(fd)
    except (OSError,ValueError,WorktreeError):return False


def matches_record(manager,commit,paths,record,side):
    """Verify durable raw versions and commit bytes in the same bound read.

    Missing versions retain legacy commit-only semantics. Explicit null or
    malformed versions are corrupt evidence, never a request for that fallback.
    """
    if type(record) is not dict or side not in ('before','after'):return False
    if 'file_versions' not in record:return matches(manager,commit,paths)
    if type(record['file_versions']) is not list:return False
    from itertools import islice
    paths=list(islice(iter(paths),129))
    covered=record.get('paths',paths)
    if (type(covered) is not list or len(covered)>128 or any(type(p) is not str for p in covered)
            or len(set(covered))!=len(covered) or any(p not in paths for p in covered)):return False
    rows=record['file_versions']
    if (len(rows)!=len(covered) or any(type(row) is not dict or type(row.get('path')) is not str for row in rows)
            or {row['path'] for row in rows}!=set(covered)):return False
    return matches(manager,commit,paths,versions=record['file_versions'],side=side)
