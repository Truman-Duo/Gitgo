"""Bounded tree/name comparisons from verified immutable commit images.

No repository command, revision/pathspec expansion, worktree read, or ref write.
Both images use the existing low-UID checkout and original invocation journal.
"""
import re
import time

from backend.core import macos_git_checkout as checkout,macos_broker_protocol as wire
from backend.core.loop.git_execution import GitBoundaryError


def refuse(message):
    raise GitBoundaryError('GIT_COMMAND_UNSUPPORTED',message)


def parse(args):
    if not args or args[0] not in ('ls-tree','diff'):return None
    if any(type(a) is not str for a in args):refuse('Expected exact string arguments')
    args=list(args)
    if len(args)>=4 and args[:2]==['ls-tree','-rz'] and args[3]=='--':
        oid=args[2];checkout.validate_commit(oid);paths=args[4:]
        if len(paths)>128 or len(set(paths))!=len(paths):refuse('Too many or duplicate tree selectors')
        for name in paths:
            # Select literal files or directory prefixes, never Git pathspecs.
            if name.startswith('-') or any(c in name for c in ':*?[]'):refuse('Tree selectors must be literal paths')
            dirs=['/'.join(name.split('/')[:i]) for i in range(1,len(name.split('/')))]
            files,dirs=wire.validate_image(dict(files=[[name,'',0o644]],directories=dirs))
            checkout.validate_paths(files,dirs)
        return 'tree',(oid,),tuple(paths)
    if args[:4]==['diff','--no-renames','--name-only','-z'] and args[-1:] == ['--']:
        revisions=args[4:-1]
        if len(revisions)==1 and re.fullmatch('[0-9a-f]{40}\\.\\.[0-9a-f]{40}',revisions[0]):
            revisions=revisions[0].split('..')
        if len(revisions)==2:
            for oid in revisions:checkout.validate_commit(oid)
            return 'names',tuple(revisions),()
    refuse('Only exact commit ls-tree -rz and diff --no-renames --name-only -z are supported')


def query(project,root,spec,*,timeout,cancellation,guard):
    from backend.core import macos_broker_client as broker
    from backend.core.macos_git_read import QueryResult
    kind,commits,paths=spec;images={};deadline=time.monotonic()+timeout
    def checked():
        guard()
        if cancellation is not None and cancellation.is_set():
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Commit comparison cancelled')
        if time.monotonic()>=deadline:
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Commit comparison deadline exceeded')
    for oid in dict.fromkeys(commits):
        checked();seconds=min(60,deadline-time.monotonic())
        if seconds<1:raise GitBoundaryError('GIT_READ_UNCONFIRMED','Insufficient time for another commit image')
        image=broker.checkout_git_commit(project,root,oid,timeout=seconds,cancellation=cancellation)
        checked()
        if image.commit!=oid:raise GitBoundaryError('GIT_READ_UNCONFIRMED','Commit image identity differs')
        images[oid]={name:(checkout.object_oid('blob',data),mode) for name,data,mode in image.files}
    if kind=='tree':
        entries=images[commits[0]]
        names=sorted((n for n in entries if not paths or any(n==p or n.startswith(p+'/') for p in paths)),key=lambda n:n.encode('utf8'))
        output=''.join(('100755' if entries[n][1]==0o755 else '100644')+' blob '+entries[n][0]+'\t'+n+'\0' for n in names)
    else:
        before,after=(images[oid] for oid in commits)
        names=sorted((n for n in before.keys()|after.keys() if before.get(n)!=after.get(n)),key=lambda n:n.encode('utf8'))
        output=''.join(n+'\0' for n in names)
    checked()
    return QueryResult(output,0)
