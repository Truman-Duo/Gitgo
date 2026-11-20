"""Host-selected index staging for a registered macOS linked worktree.

All Git object production uses the existing low-UID producer. The Host reads
selected files through Rust, writes its own canonical index, and never runs
repository commands or changes HEAD/refs. Interrupted staging is not replayed.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import uuid

from backend.core import macos_worktree as life,macos_broker_client as broker
from backend.core.macos_git_checkout import object_oid,validate_paths

MAX_CHANGES=16
MAX_DIRECTORIES=16


def directories(entries):
    return sorted({'/'.join(name.split('/')[:i]) for name,_,_ in entries for i in range(1,len(name.split('/')))})


def validate_entries(entries):
    if len(entries)>128:life.fail('index exceeds 128 files')
    # Reuse both transport path syntax and macOS alias checks. Contents here
    # are empty because this validates names/modes, not arbitrary file bytes.
    from backend.core.macos_broker_protocol import validate_image
    files,dirs=validate_image(dict(files=[[n,'',m] for n,_,m in entries],directories=directories(entries)))
    validate_paths(files,dirs)
    if len(dirs)>MAX_DIRECTORIES:life.fail('index exceeds directory budget')
    if any(type(oid) is not str or re.fullmatch('[0-9a-f]{40}',oid) is None or m not in (0o644,0o755) for _,oid,m in entries):
        life.fail('invalid index object/mode')


def encode(entries):
    validate_entries(entries)
    data=bytearray(struct.pack('!4sII',b'DIRC',2,len(entries)))
    for name,oid,mode in sorted(entries,key=lambda x:x[0].encode('utf8')):
        path=name.encode('utf8')
        item=(struct.pack('!10I',0,0,0,0,0,0,0o100755 if mode==0o755 else 0o100644,0,0,0)
              +bytes.fromhex(oid)+struct.pack('!H',len(path))+path+b'\0')
        data.extend(item+b'\0'*(-len(item)%8))
    return bytes(data)+hashlib.sha1(data).digest()


def decode(raw):
    if len(raw)<32 or hashlib.sha1(raw[:-20]).digest()!=raw[-20:]:life.fail('invalid index checksum')
    magic,version,count=struct.unpack('!4sII',raw[:12])
    if magic!=b'DIRC' or version!=2 or count>128:life.fail('unsupported index format')
    entries=[];at=12
    try:
        for _ in range(count):
            size=struct.unpack('!H',raw[at+60:at+62])[0]
            mode=struct.unpack('!I',raw[at+24:at+28])[0]
            name=raw[at+62:at+62+size].decode('utf8')
            if mode not in (0o100644,0o100755):life.fail('unsupported index mode')
            entries.append((name,raw[at+40:at+60].hex(),mode&0o777))
            at+=(62+size+1+7)//8*8
    except (ValueError,struct.error,UnicodeError) as exc:life.fail('malformed index: '+str(exc))
    if encode(entries)!=raw:life.fail('index is not the canonical Host representation')
    return entries


def tree_plan(entries):
    validate_entries(entries);tree={}
    for name,oid,mode in entries:
        node=tree;parts=name.split('/')
        for part in parts[:-1]:
            node=node.setdefault(part,{})
            if not isinstance(node,dict):life.fail('index file/directory conflict')
        if parts[-1] in node:life.fail('duplicate index member')
        node[parts[-1]]=(oid,mode)
    plan=[]
    def visit(node):
        items=[]
        for name,value in node.items():
            directory=isinstance(value,dict)
            oid,mode=(visit(value),'40000') if directory else (value[0],'100755' if value[1]==0o755 else '100644')
            n=name.encode('utf8');kind=b'tree' if directory else b'blob'
            items.append((n+(b'/' if directory else b'\0'),mode.encode()+b' '+n+b'\0'+bytes.fromhex(oid),
                          mode.encode()+b' '+kind+b' '+oid.encode()+b'\t'+n+b'\0'))
        ordered=sorted(items);oid=object_oid('tree',b''.join(x[1] for x in ordered))
        plan.append((oid,b''.join(x[2] for x in ordered)));return oid
    root=visit(tree)
    return root,plan


def selected_file(root,work,name):
    """Rust-pinned content plus same-object mode/owner observations."""
    parts=name.split('/');fd=os.dup(work)
    try:
        for part in parts[:-1]:
            child=os.open(part,life.FLAGS,dir_fd=fd);os.close(fd);fd=child
        child=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=fd)
        try:
            info=os.fstat(child)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
                    or info.st_mode&0o022):life.fail('selected index member is not a private ordinary file')
            with root.open(name) as opened:
                if (opened.identity.volume,opened.identity.file_id)!=(info.st_dev,info.st_ino):life.fail('selected file replaced')
                data=opened.read_bytes(max_bytes=1024*1024)
            after=os.fstat(child)
            if (info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_mode):
                life.fail('selected file changed during capture')
            return data,0o755 if info.st_mode&0o111 else 0o644
        finally:os.close(child)
    finally:os.close(fd)


def stage(manager,lease,paths,*,cancellation=None,_empty_tree=False):
    """Stage explicit additions/modifications/deletions; never infer all paths."""
    from backend.core.loop.native_invocation_scope import _SCOPE
    from backend.core.storage.native_invocations import pending_native_invocations
    from backend.core.sandbox_read import ReadRoot
    if _SCOPE.get() is not None:life.fail('index staging is a Host lifecycle operation, not a nested tool command')
    if type(paths) not in (list,tuple) or not (0 if _empty_tree else 1)<=len(paths)<=MAX_CHANGES or any(type(n) is not str for n in paths) or len(set(paths))!=len(paths):
        life.fail('select 1 to 16 distinct literal index paths')
    validate_entries([(name,'0'*40,0o644) for name in paths])
    if lease.mode!='write' or lease.state!='leased':life.fail('index staging requires an active write lease')
    if pending_native_invocations(manager.storage,include_active=True):life.fail('native recovery debt blocks index staging')
    with life.context(manager,lease) as (parent,metadata_parent,pins,guard):
        b=lease.macos_binding
        if b.get('result_update') or b.get('result'):life.fail('published result cannot be restaged')
        if b.get('runtime_update'):life.fail('runtime merge requires reconciliation')
        if b.get('index_update'):life.fail('interrupted index staging requires recovery')
        work=life.open_bound(parent,life.target_name(manager,lease),b['work_identity'])
        meta=None
        try:
            meta=life.open_bound(metadata_parent,b['metadata'],b['meta_identity'])
            life.check_files(manager,lease,work,meta)
            try:os.stat('index.lock',dir_fd=meta,follow_symlinks=False)
            except FileNotFoundError:pass
            else:life.fail('existing Git index lock; do not adopt or remove it')
            old=life.read_file(meta,'index');entries={n:(oid,mode) for n,oid,mode in decode(old)};updates=[]
            if _empty_tree and (paths or entries):life.fail('empty-tree staging requires an empty index and no paths')
            def checked():
                guard()
                if cancellation is not None and cancellation.is_set():life.fail('index staging cancelled')
            checked()
            with ReadRoot(Path(lease.path)) as root:
                if (root.identity.volume,root.identity.file_id)!=tuple(b['work_identity'][:2]):life.fail('worktree root replaced')
                for name in paths:
                    checked()
                    try:data,mode=selected_file(root,work,name)
                    except FileNotFoundError:
                        entries.pop(name,None);continue
                    from backend.core.macos_worktree_result import scan_files
                    scan_files(manager,[(name,data,mode)])
                    oid=object_oid('blob',data);entries[name]=(oid,mode);updates.append((oid,data))
            if sum(len(data) for _,data in updates)>4*1024*1024:life.fail('selected index bytes exceed aggregate budget')
            final=sorted((n,oid,mode) for n,(oid,mode) in entries.items());new=encode(final);tree,plan=tree_plan(final)
            intent=dict(id=uuid.uuid4().hex,phase='producing',before_sha256=hashlib.sha256(old).hexdigest(),
                        after_sha256=hashlib.sha256(new).hexdigest(),tree=tree,paths=list(paths))
            life.save(manager,lease,'published',index_update=intent)
            with ReadRoot(manager.repo_root/'.git/objects') as root:
                for oid,data in updates:
                    checked();result=broker.produce_git_objects(manager.repo_root,root,['hash-object','-w','--stdin'],data,None,cancellation=cancellation)
                    if result.stdout!=oid+'\n':life.fail('staged blob content mismatch')
                for oid,data in plan:
                    checked();result=broker.produce_git_objects(manager.repo_root,root,['mktree','-z'],data,None,cancellation=cancellation)
                    if result.stdout!=oid+'\n':life.fail('staged tree differs from captured index')
            checked()
            if life.read_file(meta,'index')!=old:life.fail('index changed before publication')
            # Git-compatible exclusion, with no adoption/removal of an existing lock.
            life.write_file(meta,'index.lock',new);os.fsync(meta)
            life.save(manager,lease,'published',index_update={**intent,'phase':'prepared'})
            checked()
            if life.read_file(meta,'index')!=old:life.fail('index changed at publication')
            os.replace('index.lock','index',src_dir_fd=meta,dst_dir_fd=meta);os.fsync(meta)
            checked()
            if life.read_file(meta,'index')!=new:life.fail('published index changed')
            life.save(manager,lease,'published',index_sha256=hashlib.sha256(new).hexdigest(),staged_tree=tree,index_update={})
            return dict(tree=tree,index_sha256=hashlib.sha256(new).hexdigest(),paths=list(paths),files=len(final))
        finally:
            if meta is not None:os.close(meta)
            os.close(work)
