"""New-file-only, handle-relative package export and bounded package reads.

Callers must already hold resource authorization for the selected directory.
An interrupted temporary file is retained for inspection, never overwritten.
"""
from contextlib import contextmanager
import ctypes as C
import hashlib
import os
from pathlib import Path
import stat
import sys

from backend.core.sandbox_read import ReadRoot, relative_parts
from .package import MAX_PACKAGE
from .state import PublishError, identifier


def _filename(value):
    parts = relative_parts(value)
    if len(parts) != 1 or "/" in value or "\\" in value:
        raise PublishError("PUBLISH_EXPORT_INVALID")
    return value


def _directory(value):
    path = Path(value)
    if not path.is_absolute() or str(path) != str(path.absolute()):
        raise PublishError("PUBLISH_EXPORT_INVALID")
    return path


def read_file(directory, filename):
    with ReadRoot(_directory(directory)) as root, root.open(_filename(filename)) as opened:
        before = os.fstat(opened._fd)
        if before.st_size > MAX_PACKAGE:
            raise PublishError("PUBLISH_PACKAGE_LIMIT")
        output = bytearray()
        while chunk := os.read(opened._fd, min(65536, MAX_PACKAGE + 1 - len(output))):
            output.extend(chunk)
            if len(output) > MAX_PACKAGE:
                raise PublishError("PUBLISH_PACKAGE_LIMIT")
        after = os.fstat(opened._fd)
        if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise PublishError("PUBLISH_EXPORT_CHANGED")
        return bytes(output)


def observe(directory, filename, expected_sha256):
    try:
        raw = read_file(directory, filename)
    except FileNotFoundError:
        return "absent"
    return "matching" if hashlib.sha256(raw).hexdigest() == expected_sha256 else "conflict"


@contextmanager
def _new_file(root, name):
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes as W
        from backend.core.sandbox_read_windows import ObjectAttributes, UnicodeString, IoStatus
        buffer = C.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        string = UnicodeString(length, length + 2, C.cast(buffer, W.LPWSTR))
        attributes = ObjectAttributes(C.sizeof(ObjectAttributes), root._handle, C.pointer(string), 0x40 | 0x1000, None, None)
        status_block, handle = IoStatus(), W.HANDLE()
        # READ_DATA | WRITE_DATA | DELETE | READ_ATTRIBUTES | SYNCHRONIZE,
        # FILE_CREATE (never open/replace), no delete sharing or reparse lookup.
        status = root._windows._create(C.byref(handle), 0x110083, C.byref(attributes), C.byref(status_block),
            None, 0x80, 1, 2, 0x200000 | 0x20 | 0x40, None, 0)
        if status < 0:
            raise C.WinError(root._windows._error(status))
        try:
            root._windows.identity(handle.value, directory=False)
            fd = msvcrt.open_osfhandle(handle.value, os.O_BINARY | os.O_RDWR)
        except BaseException:
            root._windows.close(handle.value)
            raise
    elif sys.platform == "linux":
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root._handle)
    else:
        raise PublishError("PUBLISH_EXPORT_PLATFORM_UNSUPPORTED")
    try:
        yield fd
    finally:
        os.close(fd)


def _rename_new(root, fd, temporary, filename):
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes as W
        from backend.core.sandbox_read_windows import IoStatus
        class Rename(C.Structure):
            _fields_ = [("replace", W.BOOLEAN), ("root", W.HANDLE), ("length", W.DWORD), ("name", W.WCHAR * (len(filename) + 1))]
        record = Rename(False, root._handle, len(filename.encode("utf-16-le")), filename)
        call = root._windows._nt.NtSetInformationFile
        call.argtypes, call.restype = [W.HANDLE, C.POINTER(IoStatus), C.c_void_p, W.ULONG, W.ULONG], C.c_long
        status_block = IoStatus()
        status = call(msvcrt.get_osfhandle(fd), C.byref(status_block), C.byref(record), C.sizeof(record), 10)
        if status < 0:
            raise C.WinError(root._windows._error(status))
    else:
        libc = C.CDLL(None, use_errno=True)
        call = getattr(libc, "renameat2", None)
        if call is None:
            raise PublishError("PUBLISH_EXPORT_PLATFORM_UNSUPPORTED")
        call.argtypes, call.restype = [C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_uint], C.c_int
        if call(root._handle, os.fsencode(temporary), root._handle, os.fsencode(filename), 1):
            error = C.get_errno()
            raise OSError(error, os.strerror(error))


def export_new(directory, filename, raw, operation_id):
    if not isinstance(raw, bytes) or len(raw) > MAX_PACKAGE:
        raise PublishError("PUBLISH_PACKAGE_LIMIT")
    filename = _filename(filename)
    temporary = ".gitgo-publish-" + identifier(operation_id) + ".tmp"
    with ReadRoot(_directory(directory)) as root:
        with _new_file(root, temporary) as fd:
            position = 0
            while position < len(raw):
                written = os.write(fd, raw[position:position + 65536])
                if written <= 0:
                    raise OSError("Short export write")
                position += written
            os.fsync(fd)
            _rename_new(root, fd, temporary, filename)
    return str(Path(directory) / filename)
