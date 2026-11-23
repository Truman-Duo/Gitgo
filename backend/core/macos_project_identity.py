"""Descriptor-bound persistent project identity, independent of mount dev_t.

Darwin fgetattrlist supplies volume UUID, file ID, creation time and owner.
Unsupported or incomplete attributes fail closed; paths are never re-resolved
to obtain the persistent identity. Runtime dev/inode checks remain separate.
"""
from __future__ import annotations
import ctypes
import os
import stat
import struct
import sys

RETURNED=0x80000000
VOLUME_UUID=0x00040000
VOLUME_INFO=0x80000000
OBJECT=0x02008200  # ATTR_CMN_CRTIME | ATTR_CMN_OWNERID | ATTR_CMN_FILEID


class AttrList(ctypes.Structure):
    _fields_=[('count',ctypes.c_uint16),('reserved',ctypes.c_uint16),
              ('common',ctypes.c_uint32),('volume',ctypes.c_uint32),
              ('directory',ctypes.c_uint32),('file',ctypes.c_uint32),('fork',ctypes.c_uint32)]


def attributes(fd,common,volume,size):
    if sys.platform!='darwin':raise ValueError('Persistent project identity requires Darwin')
    query=AttrList(5,0,common|RETURNED,volume,0,0,0)
    result=ctypes.create_string_buffer(size)
    call=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True).fgetattrlist
    call.argtypes=[ctypes.c_int,ctypes.POINTER(AttrList),ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong]
    call.restype=ctypes.c_int
    if call(fd,ctypes.byref(query),result,size,0):
        raise OSError(ctypes.get_errno(),'Persistent project attributes unavailable')
    head=struct.unpack_from('=6I',result.raw)
    # ATTR_VOL_INFO selects volume queries; it is not a returned value bit.
    if head!=(size,common|RETURNED,volume & ~VOLUME_INFO,0,0,0):
        raise ValueError('Filesystem did not return all required identity attributes')
    return result.raw[24:]


def persistent_identity(fd):
    before=os.fstat(fd)
    if not stat.S_ISDIR(before.st_mode):raise ValueError('Expected project directory')
    volume=attributes(fd,0,VOLUME_INFO|VOLUME_UUID,40).hex()
    sec,nsec,uid,file_id=struct.unpack('=qqIQ',attributes(fd,OBJECT,0,52))
    after=os.fstat(fd)
    if (volume=='0'*32 or file_id<=0 or sec<=0 or not 0<=nsec<1000000000
            or (before.st_dev,before.st_ino,before.st_uid)!=(after.st_dev,after.st_ino,after.st_uid)
            or (file_id,uid)!=(after.st_ino,after.st_uid)):
        raise ValueError('Persistent project identity changed or is unsupported')
    return dict(schema=1,volume_uuid=volume,file_id=file_id,birth=[sec,nsec],owner_uid=uid)


def check_persistent(fd,binding):
    expected=binding.get('project_object')
    if expected is None or persistent_identity(fd)!=expected:
        raise ValueError('Persistent project object is missing or changed')


def check_directory(fd,binding):
    from backend.core.macos_broker_protocol import directory_identity
    if directory_identity(fd)!=binding['identity']:raise ValueError('Authorized project directory changed')
    if 'project_object' in binding:check_persistent(fd,binding)
