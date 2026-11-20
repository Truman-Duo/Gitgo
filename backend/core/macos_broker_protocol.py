"""Bounded local broker protocol. Credentials come from the kernel, never JSON.

The broker handles snapshots, never opens/chowns a caller-supplied pathname.
See Apple's getpeereid(3); this authenticates an OS user, not a signed app.
"""
from __future__ import annotations
import array
import base64
import ctypes
import json
import math
import os
from pathlib import Path
import select
import socket
import stat
import struct
import time

MAX_FRAME = 16 * 1024 * 1024
MAX_CONTENT = 4 * 1024 * 1024
MAX_FILES = 128


def peer_uid(connection):
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    fn = lib.getpeereid
    fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    fn.restype = ctypes.c_int
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    if fn(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
        raise OSError(ctypes.get_errno(), 'Peer credentials unavailable')
    return uid.value


def exact(connection, count):
    result = bytearray()
    while len(result) < count:
        block = connection.recv(count-len(result))
        if not block:
            raise EOFError('Broker disconnected; never retry an unknown invocation')
        result.extend(block)
    return bytes(result)


def unique_pairs(pairs):
    result = {}
    for k,v in pairs:
        if k in result: raise ValueError('Duplicate protocol field')
        result[k] = v
    return result


def send(connection, value):
    raw = json.dumps(value,ensure_ascii=True,allow_nan=False,separators=(',',':')).encode()
    if len(raw)>MAX_FRAME: raise ValueError('Broker frame too large')
    connection.sendall(struct.pack('!I',len(raw))+raw)


def receive(connection):
    size = struct.unpack('!I',exact(connection,4))[0]
    if not 0<size<=MAX_FRAME: raise ValueError('Broker frame outside bounds')
    value=json.loads(exact(connection,size),object_pairs_hook=unique_pairs,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite number')))
    if type(value) is not dict: raise ValueError('Expected protocol object')
    return value


class ReceiveCancelled(InterruptedError):
    """A metadata exchange was cancelled before a functional request."""


class ReceiveDeadlineExceeded(TimeoutError):
    """The entire metadata exchange exceeded its absolute deadline."""


def receive_until(connection, deadline, *, cancellation=None):
    """Read one bounded frame under a single cancellable absolute deadline.

    Used only during capability negotiation. The existing functional and
    recovery protocol keeps ``receive`` unchanged. A fragmented header or body
    cannot reset this deadline by sending another byte before a socket timeout.
    This does not change socket flags or the caller's socket timeout.
    """
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError('Finite broker receive deadline required')

    def remaining():
        if cancellation is not None and cancellation.is_set():
            raise ReceiveCancelled('Broker capability negotiation cancelled')
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ReceiveDeadlineExceeded('Broker capability negotiation timed out')
        return seconds

    def bounded_exact(count):
        result = bytearray()
        while len(result) < count:
            wait = min(remaining(), .05)
            if not select.select([connection], [], [], wait)[0]:
                continue
            remaining()
            block = connection.recv(min(count - len(result), 65536))
            if not block:
                raise EOFError('Broker disconnected; never retry an unknown invocation')
            result.extend(block)
        remaining()
        return bytes(result)

    size = struct.unpack('!I', bounded_exact(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError('Broker frame outside bounds')
    value = json.loads(bounded_exact(size), object_pairs_hook=unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite number')))
    if type(value) is not dict:
        raise ValueError('Expected protocol object')
    remaining()
    return value


def send_directory(connection, fd):
    connection.sendmsg([b'D'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])


def receive_directory(connection):
    fds=[]
    try:
        message,ancillary,flags,_=connection.recvmsg(1,socket.CMSG_SPACE(8*array.array('i').itemsize))
        unexpected=False
        for level,kind,data in ancillary:
            if level!=socket.SOL_SOCKET or kind!=socket.SCM_RIGHTS:
                unexpected=True;continue
            values=array.array('i');values.frombytes(data);fds.extend(values)
        for fd in fds: os.set_inheritable(fd,False)
        if unexpected or message!=b'D' or flags & socket.MSG_CTRUNC or len(fds)!=1:
            raise ValueError('Exactly one project directory descriptor required')
        fd=fds[0]
        directory_identity(fd)
        fds=[]
        return fd
    finally:
        for fd in fds: os.close(fd)


def open_directory(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts: raise ValueError('Canonical absolute project required')
    # Identity binding needs traversal/fstat, not directory enumeration. In
    # particular, a root-owned 0711 ancestor is intentionally searchable but
    # not readable by the ordinary Host. Darwin O_SEARCH preserves that DAC
    # boundary without chmod or granting listing access.
    flags=getattr(os,'O_SEARCH',os.O_RDONLY)|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
    fd=os.open('/',flags)
    try:
        for part in path.parts[1:]:
            new=os.open(part,flags,dir_fd=fd)
            os.close(fd);fd=new
        # SCM_RIGHTS/F_SETFD need an ordinary transferable directory handle on
        # Darwin. Reopen the pinned object, not its pathname; only the project
        # itself (which the Host snapshots) requires read permission.
        result=os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
        return result
    finally:
        if fd>=0: os.close(fd)


def directory_identity(fd):
    s=os.fstat(fd)
    if not stat.S_ISDIR(s.st_mode): raise ValueError('Project handle is not a directory')
    return [s.st_dev,s.st_ino,s.st_uid]


def validate_image(value):
    if type(value) is not dict or set(value)!={'files','directories'}:
        raise ValueError('Invalid workspace image')
    files,dirs=value['files'],value['directories']
    if type(files) is not list or type(dirs) is not list or len(files)>MAX_FILES or len(dirs)>512:
        raise ValueError('Workspace image exceeds bounds')
    def name(raw):
        if type(raw) is not str or len(raw)>1024 or '\0' in raw or '\\' in raw:
            raise ValueError('Invalid member name')
        parts=raw.split('/')
        if any(p in ('','.','..') or p.casefold() in ('.git','.gitgo') for p in parts):
            raise ValueError('Protected or escaping image member')
        return raw
    paths=set();directory_names=[];decoded=[];used=0
    for raw in dirs:
        n=name(raw)
        if n.casefold() in paths: raise ValueError('Duplicate image member')
        paths.add(n.casefold());directory_names.append(n)
    file_names=set()
    for member in files:
        if type(member) is not list or len(member)!=3: raise ValueError('Invalid file member')
        n,encoded,mode=member;n=name(n)
        if type(mode) is not int or mode not in (0o600,0o644,0o700,0o755): raise ValueError('Unsafe image mode')
        if n.casefold() in paths or type(encoded) is not str: raise ValueError('Duplicate image member')
        paths.add(n.casefold());file_names.add(n.casefold())
        raw=base64.b64decode(encoded,validate=True);used+=len(raw)
        if used>MAX_CONTENT: raise ValueError('Workspace content exceeds trial limit')
        decoded.append((n,raw,mode))
    for n in paths:
        if any('/'.join(n.split('/')[:i]) in file_names for i in range(1,len(n.split('/')))):
            raise ValueError('File used as image directory')
    return decoded,directory_names


def image_wire(image):
    value={'files':[[n,base64.b64encode(raw).decode('ascii'),mode] for n,raw,mode in image.files],
           'directories':list(image.directories)}
    validate_image(value)
    return value


def materialize(value, target, *, uid=None):
    # Only an empty, service-created directory. Task has not started yet.
    files,dirs=validate_image(value)
    if any(target.iterdir()): raise ValueError('Image target must be empty')
    for name in dirs: (target/name).mkdir(parents=True,exist_ok=True)
    for name,raw,mode in files:
        path=target/name;path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
        with os.fdopen(fd,'wb') as stream: stream.write(raw)
        path.chmod(mode)
    if uid is not None:
        for directory,dirs,files in os.walk(target,followlinks=False):
            os.chown(directory,uid,uid)
            for name in files: os.chown(Path(directory)/name,uid,uid,follow_symlinks=False)
