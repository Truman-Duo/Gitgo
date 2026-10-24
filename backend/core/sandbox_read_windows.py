"""Unprivileged relative Windows file opens for the native read helper.

No ACL changes, token privileges, backup semantics, or inheritable handles.
The ACL recovery operator intentionally does not participate in this path.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from pathlib import Path

from backend.core.sandbox_read import ObjectIdentity, ReadBoundaryError, relative_parts


class UnicodeString(C.Structure):
    _fields_ = [('Length', W.USHORT), ('MaximumLength', W.USHORT), ('Buffer', W.LPWSTR)]


class ObjectAttributes(C.Structure):
    _fields_ = [('Length', W.ULONG), ('RootDirectory', W.HANDLE),
                ('ObjectName', C.POINTER(UnicodeString)), ('Attributes', W.ULONG),
                ('SecurityDescriptor', C.c_void_p), ('SecurityQualityOfService', C.c_void_p)]


class IoStatus(C.Structure):
    _fields_ = [('Status', C.c_void_p), ('Information', C.c_size_t)]


class FileInfo(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME),
                ('accessed', W.FILETIME), ('written', W.FILETIME),
                ('volume', W.DWORD), ('size_high', W.DWORD), ('size_low', W.DWORD),
                ('links', W.DWORD), ('index_high', W.DWORD), ('index_low', W.DWORD)]


class FileId(C.Structure):
    _fields_ = [('volume', C.c_uint64), ('identifier', C.c_ubyte * 16)]


def _function(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn


class WindowsReadApi:
    def __init__(self):
        self._nt = C.WinDLL('ntdll', use_last_error=True)
        self._kernel = C.WinDLL('kernel32', use_last_error=True)
        self._create = _function(self._nt, 'NtCreateFile',
            [C.POINTER(W.HANDLE), W.ULONG, C.POINTER(ObjectAttributes),
             C.POINTER(IoStatus), C.c_void_p, W.ULONG, W.ULONG, W.ULONG,
             W.ULONG, C.c_void_p, W.ULONG], C.c_long)
        self.close = _function(self._kernel, 'CloseHandle', [W.HANDLE], W.BOOL)
        self._error = _function(self._nt, 'RtlNtStatusToDosError', [C.c_long], W.ULONG)
        self._type = _function(self._kernel, 'GetFileType', [W.HANDLE], W.DWORD)
        self._info = _function(self._kernel, 'GetFileInformationByHandle',
                              [W.HANDLE, C.POINTER(FileInfo)], W.BOOL)
        self._id = _function(self._kernel, 'GetFileInformationByHandleEx',
                            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL)

    def root(self, path: Path):
        # UNC/device namespaces, drive-relative paths and alternate streams are
        # outside this first local-filesystem contract.
        if len(path.drive) != 2 or path.drive[1] != ':' or not path.is_absolute():
            raise ReadBoundaryError('Stable reads require a local drive root')
        relative_parts(str(path.relative_to(path.anchor)))
        return self.open('\\??\\' + str(path), None, directory=True)

    def open(self, name: str, parent, *, directory: bool, read_data: bool = True,
             share_write: bool = True):
        buffer = C.create_unicode_buffer(name)
        length = len(name.encode('utf-16-le'))
        if length > 65532:
            raise ReadBoundaryError('File name exceeds NT string bounds')
        string = UnicodeString(length, length + 2, C.cast(buffer, W.LPWSTR))
        # OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE. No OBJ_INHERIT.
        attributes = ObjectAttributes(C.sizeof(ObjectAttributes), parent,
                                      C.pointer(string), 0x40 | 0x1000, None, None)
        status_block = IoStatus()
        handle = W.HANDLE()
        # SYNCHRONIZE | READ_ATTRIBUTES | (TRAVERSE or READ_DATA).
        access = 0x100080 | (0x20 if directory else (1 if read_data else 0))
        # FILE_OPEN_REPARSE_POINT | SYNCHRONOUS_IO_NONALERT | DIRECTORY_FILE
        # for directories. Leaf type is checked on the returned handle.
        options = 0x200000 | 0x20 | (1 if directory else 0)
        status = self._create(C.byref(handle), access, C.byref(attributes),
            C.byref(status_block), None, 0, 1 | (2 if share_write else 0), 1, options, None, 0)
        if status < 0:
            raise C.WinError(self._error(status))
        return handle.value

    def identity(self, handle, *, directory: bool) -> ObjectIdentity:
        if self._type(handle) != 1:  # FILE_TYPE_DISK
            raise ReadBoundaryError('Read requires a disk object')
        info = FileInfo()
        if not self._info(handle, C.byref(info)):
            raise C.WinError(C.get_last_error())
        if info.attributes & 0x400:
            raise ReadBoundaryError('Reparse objects cannot be read')
        is_directory = bool(info.attributes & 0x10)
        if is_directory != directory:
            if is_directory:
                raise IsADirectoryError('Read target is a directory')
            raise NotADirectoryError('Read parent is not a directory')
        if not directory and info.links != 1:
            raise ReadBoundaryError('Multiply linked files cannot be read')
        identity = FileId()
        if not self._id(handle, 18, C.byref(identity), C.sizeof(identity)):
            raise C.WinError(C.get_last_error())
        return ObjectIdentity('win32', identity.volume,
                              int.from_bytes(bytes(identity.identifier), 'little'))
