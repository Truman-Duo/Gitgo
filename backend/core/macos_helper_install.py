"""Sealed frozen helper installation, separate from the user-managed app.

Internal acceptance pins an independently confirmed complete inventory digest.
Legacy ad-hoc acceptance remains explicit for existing experiments. Neither is
publisher authentication or an enablement of the production capability.
Nothing here grants a project, changes PATH, or replaces a user's Gitgo.
"""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import re

if os.name == 'posix':
    import fcntl

from backend.core.macos_file_authority import require_no_extended_acl

BASE=Path('/Library/PrivilegedHelperTools/org.gitgo.sandbox-v1')
MAX_BUNDLE_BYTES=1024**3
MAX_BUNDLE_ENTRIES=20000
MAX_BUNDLE_DEPTH=64


def _stamp(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _check_owner(info,sealed):
    if sealed and (info.st_uid!=0 or info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode)):
        raise ValueError('Installed helper is writable outside root')


def _check_links(entries):
    """Resolve framework links in the captured inventory, never via live paths."""
    nodes={entry[0]:entry for entry in entries}
    for path,entry in nodes.items():
        if entry[1]!='link': continue
        pending=path.split('/');resolved=[];followed=0
        while pending:
            part=pending.pop(0)
            if part in ('','.'): continue
            if part=='..':
                if not resolved: raise ValueError('Helper contains an external symlink')
                resolved.pop();continue
            key='/'.join([*resolved,part]);node=nodes.get(key)
            if node is None: raise ValueError('Helper contains a dangling symlink')
            if node[1]=='link':
                target=node[2];followed+=1
                if not target or target.startswith('/'):
                    raise ValueError('Helper contains an external symlink')
                if followed>40: raise ValueError('Helper contains a cyclic symlink')
                pending=target.split('/')+pending
            else:
                if pending and node[1]!='directory':
                    raise ValueError('Helper symlink traverses a non-directory')
                resolved.append(part)


def inventory(root, *, sealed=False):
    """Bounded descriptor-relative scan; callers still need immutable staging.

    The source may be operator-writable while it is being copied. Refuse
    replacement, growth, unreadable directories and in-place mutations instead
    of following swapped ancestors or silently producing a partial inventory.
    This does not promise an atomic snapshot of a concurrently writable tree.
    """
    root=Path(root).absolute()
    entries=[];total=0
    flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC

    def visit(directory,prefix,depth):
        nonlocal total
        if depth>MAX_BUNDLE_DEPTH: raise ValueError('Helper bundle exceeds depth budget')
        before=os.fstat(directory);_check_owner(before,sealed)
        if sealed: require_no_extended_acl(directory)
        # scandir errors must propagate; os.walk's default ignores them.
        with os.scandir(directory) as scan:
            names=[]
            for item in scan:
                names.append(item.name)
                if len(names)+len(entries)>MAX_BUNDLE_ENTRIES:
                    raise ValueError('Helper bundle exceeds entry budget')
        for name in sorted(names):
            if len(entries)>=MAX_BUNDLE_ENTRIES: raise ValueError('Helper bundle exceeds entry budget')
            relative=prefix+name
            info=os.stat(name,dir_fd=directory,follow_symlinks=False)
            _check_owner(info,sealed)
            if stat.S_ISLNK(info.st_mode):
                target=os.readlink(name,dir_fd=directory)
                entries.append([relative,'link',target])
            elif stat.S_ISDIR(info.st_mode):
                child=os.open(name,flags|os.O_DIRECTORY,dir_fd=directory)
                try:
                    if _stamp(os.fstat(child))!=_stamp(info): raise ValueError('Bundle changed')
                    entries.append([relative,'directory'])
                    visit(child,relative+'/',depth+1)
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                total+=info.st_size
                if total>MAX_BUNDLE_BYTES: raise ValueError('Helper bundle exceeds size budget')
                fd=os.open(name,flags,dir_fd=directory)
                try:
                    if _stamp(os.fstat(fd))!=_stamp(info): raise ValueError('Bundle changed')
                    if sealed: require_no_extended_acl(fd)
                    digest=hashlib.sha256();remaining=info.st_size
                    while remaining:
                        chunk=os.read(fd,min(remaining,1024*1024))
                        if not chunk: raise ValueError('Bundle changed')
                        digest.update(chunk);remaining-=len(chunk)
                    # Read at most the declared size plus one, even under growth.
                    if os.read(fd,1) or _stamp(os.fstat(fd))!=_stamp(info):
                        raise ValueError('Bundle changed')
                finally: os.close(fd)
                entries.append([relative,'file',bool(info.st_mode&0o111),info.st_size,digest.hexdigest()])
            else: raise ValueError('Helper contains a special file or hardlink')
            if _stamp(os.stat(name,dir_fd=directory,follow_symlinks=False))!=_stamp(info):
                raise ValueError('Bundle changed')
        if _stamp(os.fstat(directory))!=_stamp(before): raise ValueError('Bundle changed')

    fd=os.open(root,flags|os.O_DIRECTORY)
    try:
        before=os.fstat(fd);visit(fd,'',0)
        if _stamp(root.lstat())!=_stamp(before): raise ValueError('Bundle changed')
    finally: os.close(fd)
    host=next((entry for entry in entries if entry[0]=='gitgo-host'),None)
    if host is None or host[1]!='file' or not host[2]: raise ValueError('Expected an executable frozen Host bundle')
    _check_links(entries)
    return sorted(entries)


def identity(entries):
    return hashlib.sha256(json.dumps(entries,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


def trusted_directory(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Helper directory must use an absolute non-traversing path')
    # The ordinary Host checks root-owned 0711 service ancestors too. Search
    # handles allow fstat/ACL verification without requiring directory listing.
    flags=getattr(os,'O_SEARCH',os.O_RDONLY)|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC
    fd=os.open('/',flags)
    try:
        for name in (None,*path.parts[1:]):
            if name is not None:
                child=os.open(name,flags,dir_fd=fd)
                os.close(fd);fd=child
            info=os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
                raise ValueError('Helper directory is not root-controlled: '+str(path))
            require_no_extended_acl(fd)
    finally: os.close(fd)


def verify_installed_runtime():
    if sys.platform!='darwin' or not getattr(sys,'frozen',False) or os.getuid()!=0 or os.geteuid()!=0:
        raise PermissionError('Privileged helper requires the installed frozen runtime as root')
    return verify_bundle_path(sys.executable)


def verify_bundle_path(executable):
    executable=Path(executable).absolute();version=executable.parent.parent
    if executable.name!='gitgo-host' or executable.parent.name!='runtime' or version.parent!=BASE:
        raise ValueError('Helper must use its sealed installation path')
    trusted_directory(executable.parent)
    read_receipt(version)
    entries=inventory(executable.parent,sealed=True)
    if identity(entries)!=version.name:
        raise ValueError('Installed helper bundle differs from its operator-approved snapshot')
    return version


def _pin(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('Expected an independently confirmed helper SHA-256')
    return value


def read_receipt(version):
    """Root receipt is authority for an exact snapshot, never publisher identity."""
    from backend.core.macos_identity import bounded_read
    from backend.core.macos_broker_protocol import unique_pairs
    version=Path(version)
    trusted_directory(version)
    identifier=_pin(version.name)
    receipt=json.loads(bounded_read(version/'install.json',0,True),object_pairs_hook=unique_pairs)
    legacy=dict(schema=1,bundle_id=identifier,production_enabled=False)
    pinned=dict(schema=2,bundle_id=identifier,production_enabled=False,
                trust=dict(kind='operator-pinned-sha256',sha256=identifier))
    if (type(receipt) is not dict or type(receipt.get('schema')) is not int
            or receipt.get('production_enabled') is not False or receipt not in (legacy,pinned)):
        raise ValueError('Invalid helper installation trust receipt')
    return receipt


def require_transition_trust(previous,target):
    """A pinned service cannot be upgraded through the legacy unpinned path.

    Explicit administrator pins may select any exact local build, including a
    known older one. This is not remote update admission or a sequence watermark.
    Recovery retains only the predecessor already journaled by lifecycle code.
    """
    if previous and read_receipt(Path(previous).parent.parent)['schema']==2:
        if read_receipt(Path(target).parent.parent)['schema']!=2:
            raise ValueError('Pinned helper service requires an independently confirmed candidate SHA-256')


@contextmanager
def _install_lock():
    """Serialize publication and receipt promotion across different services."""
    fd=os.open(BASE/'.install.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o077):
            raise ValueError('Unsafe helper installation lock')
        require_no_extended_acl(fd)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def install(bundle, *, allow_local_adhoc=False, expected_bundle_sha256=None):
    if sys.platform!='darwin' or os.getuid()!=0 or os.geteuid()!=0:
        raise PermissionError('Helper installation requires macOS administrator authority')
    if expected_bundle_sha256 is not None:
        _pin(expected_bundle_sha256)
        if allow_local_adhoc:
            raise ValueError('Select either an explicit digest pin or legacy local acceptance')
    elif not allow_local_adhoc:
        raise ValueError('Publisher trust policy is not enabled; local acceptance requires explicit --allow-local-adhoc')
    bundle=Path(bundle).resolve(strict=True)
    before=inventory(bundle)
    identifier=identity(before)
    if expected_bundle_sha256 is not None and identifier!=expected_bundle_sha256:
        raise ValueError('Helper bundle differs from the independently confirmed SHA-256')
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(bundle/'gitgo-host')],check=True,capture_output=True,timeout=30)
    # Validate every existing ancestor before creating the fixed install roots.
    for path in (BASE.parent,BASE):
        trusted_directory(path.parent)
        try: path.mkdir(mode=0o755)
        except FileExistsError: pass
        trusted_directory(path)
    with _install_lock():
        return _publish(bundle,before,identifier,expected_bundle_sha256 is not None)


def _publish(bundle,before,identifier,pinned):
    from backend.core.macos_identity import atomic_json
    destination=BASE/identifier
    receipt=dict(schema=1,bundle_id=identifier,production_enabled=False)
    if pinned:
        receipt.update(schema=2,trust=dict(kind='operator-pinned-sha256',sha256=identifier))
    if destination.exists():
        trusted_directory(destination)
        saved=read_receipt(destination)
        if inventory(destination/'runtime',sealed=True)!=before:
            raise ValueError('Existing helper installation does not match')
        # An explicit independently obtained pin may adopt an existing exact
        # snapshot; legacy acceptance can never erase an existing pin.
        if pinned and saved['schema']==1:
            atomic_json(destination/'install.json',receipt)
        return destination/'runtime/gitgo-host'
    staging=Path(tempfile.mkdtemp(prefix='.install-',dir=BASE))
    try:
        shutil.copytree(bundle,staging/'runtime',symlinks=True)
        if inventory(staging/'runtime')!=before or inventory(bundle)!=before: raise ValueError('Build changed during installation')
        for directory,dirs,files in os.walk(staging,followlinks=False):
            Path(directory).chmod(0o755)
            for name in dirs+files:
                path=Path(directory)/name;info=path.lstat()
                os.chown(path,0,0,follow_symlinks=False)
                if not stat.S_ISLNK(info.st_mode): path.chmod(0o755 if stat.S_ISDIR(info.st_mode) or info.st_mode&0o111 else 0o644)
        # copytree can preserve source ACLs; chmod does not remove them. Reject
        # inherited/copied authority before publishing the immutable image.
        trusted_directory(staging)
        if inventory(staging/'runtime',sealed=True)!=before:
            raise ValueError('Sealed helper differs from its approved source')
        atomic_json(staging/'install.json',receipt)
        subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(staging/'runtime/gitgo-host')],check=True,capture_output=True,timeout=30)
        os.rename(staging,destination)
        directory=os.open(BASE,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
        return destination/'runtime/gitgo-host'
    finally:
        if staging.exists(): shutil.rmtree(staging)
