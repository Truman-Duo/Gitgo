"""Unreferenced Git object production in the existing low-UID private view.

This is a prerequisite for protected worktrees, not permission to update refs,
index or checked-out files. Root handles only bounded data. The ordinary Host
may append verified content-addressed objects; it never overwrites an object.
"""
from dataclasses import dataclass
import base64
import hashlib
import os
from pathlib import Path
import re
import stat
import zlib

from backend.core.loop.git_execution import GitBoundaryError, commit_author_environment
from backend.core import macos_git_read as read
from backend.core import macos_broker_protocol as wire

OID=re.compile('[0-9a-f]{40}')
MAX_INPUT=1024*1024
MAX_EXPANDED=wire.MAX_CONTENT


def refuse(message):
    raise GitBoundaryError('GIT_OBJECT_VIEW_UNSUPPORTED',message)


def validate(query, data, author):
    if type(query) not in (list,tuple) or any(type(x) is not str for x in query):refuse('Expected fixed object producer')
    query=list(query)
    if type(data) is not bytes or len(data)>MAX_INPUT:refuse('Object input exceeds bound')
    env={}
    if query==['hash-object','-w','--stdin']:
        if author is not None:refuse('Blob production does not accept an author')
    elif query==['mktree','-z']:
        if author is not None:refuse('Tree production does not accept an author')
        # No missing entries, path traversal, duplicate names or gitlinks.
        names=set()
        if data and not data.endswith(b'\0'):refuse('Expected NUL-delimited tree input')
        for entry in data.split(b'\0')[:-1]:
            match=re.fullmatch(rb'(100644|100755|040000|40000) (blob|tree) ([0-9a-f]{40})\t([^\0/]+)',entry)
            if not match:refuse('Unsupported tree entry')
            mode,kind,oid,name=match.groups()
            if (mode in (b'040000',b'40000'))!=(kind==b'tree'):refuse('Tree mode and type differ')
            try:key=name.decode('utf8').casefold()
            except UnicodeError:refuse('Tree names must be UTF-8')
            if name in (b'.',b'..') or name.lower() in (b'.git',b'.gitgo') or b'\\' in name:refuse('Protected tree name')
            if key in names:refuse('Duplicate or case-colliding tree entry')
            names.add(key)
    elif query and query[0]=='commit-tree':
        options=query[1:];parents=options[1:-2]
        if (not 3<=len(options)<=21 or not OID.fullmatch(options[0]) or options[-2]!='-m'
                or len(parents)%2 or any(parents[i]!='-p' or not OID.fullmatch(parents[i+1]) for i in range(0,len(parents),2))
                or not 1<=len(options[-1].encode('utf8'))<=65536 or '\0' in options[-1] or data):
            refuse('Expected fixed tree, parents and bounded commit message')
        env=commit_author_environment(query,author)
        env.update({k.replace('AUTHOR','COMMITTER'):v for k,v in list(env.items())})
    else:refuse('Only blob, tree and commit object production is admitted')
    return env


def command(destination,query,data,author):
    env=validate(query,data,author)
    # Reuse the fixed executable and safety overrides, never repository config.
    prefix=read.read_command(destination,['cat-file','-t','0'*40])[:-3]
    return [*prefix,'-c','commit.gpgSign=false',*query],env


def identity_environment(argv, source, workspace):
    """Revalidate the broker's fixed commit identity at the execution boundary.

    This does not whitelist inherited Git variables for general task commands.
    Only the exact private commit-tree command may carry these six data fields.
    """
    prefix=read.read_command(Path(workspace)/'object-view',['cat-file','-t','0'*40])[:-3]
    prefix=[*prefix,'-c','commit.gpgSign=false']
    if list(argv[:len(prefix)])!=prefix:refuse('Commit identity requires the fixed private Git command')
    query=list(argv[len(prefix):])
    author={key:source.get('GIT_AUTHOR_'+key.upper()) for key in ('name','email','date')}
    expected=validate(query,b'',author)
    supplied={key:value for key,value in source.items() if key.startswith(('GIT_AUTHOR_','GIT_COMMITTER_'))}
    if supplied!=expected:refuse('Incomplete or inconsistent commit identity')
    return expected


def decode_input(value):
    if type(value) is not str or len(value)>4*((MAX_INPUT+2)//3):refuse('Invalid encoded producer input')
    try:return base64.b64decode(value,validate=True)
    except ValueError as exc:raise GitBoundaryError('GIT_OBJECT_VIEW_UNSUPPORTED','Invalid producer input') from exc


def loose_oid(name,raw):
    if re.fullmatch('[0-9a-f]{2}/[0-9a-f]{38}',name) is None:refuse('New objects must be loose SHA-1 objects')
    stream=zlib.decompressobj()
    try:expanded=stream.decompress(raw,MAX_EXPANDED+1)
    except zlib.error as exc:raise GitBoundaryError('GIT_OBJECT_VIEW_UNSUPPORTED','Malformed loose object') from exc
    if len(expanded)>MAX_EXPANDED or not stream.eof or stream.unused_data or stream.unconsumed_tail:refuse('Invalid compressed object boundary')
    header,sep,body=expanded.partition(b'\0')
    match=re.fullmatch(rb'(blob|tree|commit) (0|[1-9][0-9]{0,9})',header)
    if not sep or not match or int(match[2])!=len(body):refuse('Invalid object header or length')
    oid=hashlib.sha1(expanded).hexdigest()
    if oid!=name.replace('/',''):refuse('Object content hash does not match its name')
    return oid


@dataclass(frozen=True)
class ProducedObjects:
    oid: str
    additions: tuple[tuple[str,bytes],...]
    before_sha256: str


def verify_result(before,after,query,data,author,stdout):
    validate(query,data,author)
    if type(stdout) is not str or re.fullmatch('[0-9a-f]{40}\n',stdout) is None:refuse('Expected exactly one produced object ID')
    old=dict(before.files);new=dict(after.files)
    if any(new.get(n)!=raw for n,raw in old.items()):refuse('Object producer changed or deleted its input objects')
    additions=tuple((n,raw) for n,raw in after.files if n not in old)
    for name,raw in additions:loose_oid(name,raw)
    oid=stdout.strip()
    # A preexisting packed object can be returned without a new loose object.
    if query==['hash-object','-w','--stdin']:
        expected=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        if oid!=expected:refuse('Blob output differs from the supplied bytes')
    if not additions and not before.files:refuse('Producer returned no object')
    if additions and oid not in {n.replace('/','') for n,_ in additions} and oid[:2]+'/'+oid[2:] not in old:
        refuse('Result object is absent from the returned object set')
    return ProducedObjects(oid,additions,before.sha256)


def append_objects(project, root, produced, *, guard):
    """Host-only append, pinned directories, no refs/index/config modification.

    A failure may leave unreachable validated objects. It never proves a branch
    was updated, and callers must not replay the producer on a lost result.
    """
    from backend.core.sandbox_read import ReadRoot
    if type(root) is not ReadRoot or type(produced) is not ProducedObjects:refuse('Explicit object-root authority required')
    for name,raw in produced.additions:loose_oid(name,raw)
    directory=Path(project)/'.git/objects';fd=wire.open_directory(directory)
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
    try:
        if not read._matches(fd,root.identity):refuse('Object root replaced before import')
        for name,raw in produced.additions:
            guard();bucket,leaf=name.split('/')
            try:os.mkdir(bucket,mode=0o755,dir_fd=fd)
            except FileExistsError:pass
            child=os.open(bucket,flags,dir_fd=fd)
            try:
                info=os.fstat(child)
                if info.st_dev!=root.identity.volume or info.st_uid!=os.getuid():refuse('Unowned object bucket')
                guard()
                # Stage under an unguessable name and publish with Darwin's
                # exclusive rename. No reader can observe a partial final object.
                import uuid
                temporary='tmp_gitgo_'+uuid.uuid4().hex
                out=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o444,dir_fd=child)
                try:
                    view=memoryview(raw)
                    while view:
                        written=os.write(out,view)
                        if not written:raise OSError('Short object write')
                        view=view[written:]
                    os.fsync(out);guard()
                    try:rename_exclusive(child,temporary,leaf)
                    except FileExistsError:
                        with root.open(name) as existing:current=existing.read_bytes(max_bytes=MAX_EXPANDED)
                        if current!=raw:refuse('Existing object differs; no overwrite permitted')
                finally:
                    os.close(out)
                    try:os.unlink(temporary,dir_fd=child)
                    except FileNotFoundError:pass
                os.fsync(child)
            finally:os.close(child)
        guard();os.fsync(fd)
    finally:os.close(fd)
    return produced.oid+'\n'


def rename_exclusive(directory, old, new):
    # Darwin stdio.h: RENAME_EXCL = 0x00000004. Unsupported volumes refuse.
    import ctypes
    import sys
    if sys.platform!='darwin':refuse('Exclusive object publication requires Darwin')
    fn=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True).renameatx_np
    fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    fn.restype=ctypes.c_int
    if fn(directory,os.fsencode(old),directory,os.fsencode(new),4):
        error=ctypes.get_errno();raise OSError(error,os.strerror(error))
