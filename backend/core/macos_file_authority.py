"""Descriptor-only macOS ACL checks for fixed administrative trust files.

Mode bits alone cannot establish exclusive root control on macOS. This check
rejects every nonempty extended ACL, including deny-only/inherited entries;
it neither interprets principals nor edits permissions. Callers still verify
owner, mode, object type and every parent, and retain their own mutation checks.

Darwin ABI: sys/acl.h (macOS SDK), acl_get_fd(3), acl_get_entry(3).
Apple Libc posix1e/acl_file.c obtains FILESEC_ACL from fstatx_np; gen/filesec.c
returns ENOENT when that property is absent. A valid allocated ACL's first
entry returns 0 when present, or -1/EINVAL when empty (not Linux's 1/0 ABI).
https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_file.c
https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_entry.c
https://github.com/apple-oss-distributions/Libc/blob/main/gen/filesec.c
"""
from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys


_ACL_TYPE_EXTENDED = 0x100
_ACL_FIRST_ENTRY = 0


class FileAuthorityError(ValueError):
    """An ACL-free authority boundary could not be established."""


def _acl_functions():
    try:
        library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        library.acl_get_fd_np.restype = ctypes.c_void_p
        library.acl_valid.argtypes = [ctypes.c_void_p]
        library.acl_valid.restype = ctypes.c_int
        library.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_void_p)]
        library.acl_get_entry.restype = ctypes.c_int
        library.acl_free.argtypes = [ctypes.c_void_p]
        library.acl_free.restype = ctypes.c_int
        return library
    except (AttributeError, OSError) as exc:
        raise FileAuthorityError('Native macOS ACL inspection is unavailable') from exc


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def require_no_extended_acl(fd):
    """Reject ACL entries and unsupported inspection of the already-open fd.

    The caller owns the descriptor. No path is resolved, no subprocess runs,
    and no ACL or descriptor is modified. Results are not cached.
    """
    if sys.platform != 'darwin' or type(fd) is not int or fd < 0:
        raise FileAuthorityError('macOS ACL inspection requires an open descriptor')
    try:
        before = os.fstat(fd)
        if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
            raise FileAuthorityError('Authority ACL inspection requires a file or directory')
        functions = _acl_functions()
        ctypes.set_errno(0)
        acl = functions.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
        result_errno = ctypes.get_errno()
        if not acl:
            # The descriptor was just validated. Darwin's FILESEC_ACL absence
            # is ENOENT; EINVAL/EOPNOTSUPP/EACCES are never treated as absence.
            if result_errno != errno.ENOENT:
                raise FileAuthorityError('Extended ACL could not be inspected')
        else:
            try:
                if functions.acl_valid(acl) != 0:
                    raise FileAuthorityError('Extended ACL is invalid')
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                status = functions.acl_get_entry(acl, _ACL_FIRST_ENTRY, ctypes.byref(entry))
                result_errno = ctypes.get_errno()
                if status == 0:
                    raise FileAuthorityError('Extended ACL entries are unsupported for administrative authority')
                if status != -1 or result_errno != errno.EINVAL or entry.value is not None:
                    raise FileAuthorityError('Extended ACL entries could not be inspected')
            finally:
                if functions.acl_free(acl) != 0:
                    raise FileAuthorityError('Extended ACL inspection could not release native state')
        if _stamp(os.fstat(fd)) != _stamp(before):
            raise FileAuthorityError('Authority object changed during ACL inspection')
    except OSError as exc:
        raise FileAuthorityError('Authority descriptor is unavailable for ACL inspection') from exc
