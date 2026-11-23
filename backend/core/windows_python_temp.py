"""Keep Python's private directories usable by their creating AppContainer.

Python 3.12.4+ creates Windows mode-0700 directories with a protected DACL.
Its owner-only DACL omits the AppContainer SID, so even the creating process
cannot use a TemporaryDirectory (CPython issue 134587).  This child-local
adapter preserves that DACL and adds only the current token's container SID.
Creation is atomic; no broker, post-creation repair or broad package grant is
involved. All parent/path access checks remain enforced by the process token.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import operator
import os
import sys


def _native_failure(code, stage, error, path=None):
    failure = C.WinError(error)
    failure.code, failure.stage = code, stage
    failure.strerror = f'[{code}] {failure.strerror}'
    if path is not None:
        failure.filename = path
    return failure


def _invalid_identity(stage, message):
    failure = OSError('[PYTHON_APPCONTAINER_IDENTITY_INVALID] ' + message)
    failure.code, failure.stage = 'PYTHON_APPCONTAINER_IDENTITY_INVALID', stage
    return failure


class _SecurityAttributes(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p),
                ("inherit_handle", W.BOOL)]


def _function(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


class _PrivateDirectoryApi:
    def __init__(self):
        kernel = C.WinDLL("kernel32", use_last_error=True)
        advapi = C.WinDLL("advapi32", use_last_error=True)
        self.current_process = _function(kernel, "GetCurrentProcess", [], W.HANDLE)
        self.current_thread = _function(kernel, "GetCurrentThread", [], W.HANDLE)
        self.close = _function(kernel, "CloseHandle", [W.HANDLE], W.BOOL)
        self.free = _function(kernel, "LocalFree", [C.c_void_p], C.c_void_p)
        self.open_process_token = _function(advapi, "OpenProcessToken",
            [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
        self.open_thread_token = _function(advapi, "OpenThreadToken",
            [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL)
        self.token_info = _function(advapi, "GetTokenInformation",
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        self.sid_text = _function(advapi, "ConvertSidToStringSidW",
            [C.c_void_p, C.POINTER(W.LPWSTR)], W.BOOL)
        self.descriptor = _function(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL)
        self.create = _function(kernel, "CreateDirectoryW",
            [W.LPCWSTR, C.POINTER(_SecurityAttributes)], W.BOOL)

    def container_sid(self):
        token = W.HANDLE()
        if not self.open_thread_token(self.current_thread(), 0x0008, True, C.byref(token)):
            error = C.get_last_error()
            if error != 1008:  # ERROR_NO_TOKEN: use the process's effective token.
                raise _native_failure('PYTHON_APPCONTAINER_TOKEN_QUERY_FAILED', 'open_thread_token', error)
            if not self.open_process_token(self.current_process(), 0x0008, C.byref(token)):
                raise _native_failure('PYTHON_APPCONTAINER_TOKEN_QUERY_FAILED', 'open_process_token', C.get_last_error())
        try:
            length, is_container = W.DWORD(), W.DWORD()
            if not self.token_info(token, 29, C.byref(is_container), C.sizeof(is_container), C.byref(length)):
                raise _native_failure('PYTHON_APPCONTAINER_TOKEN_QUERY_FAILED', 'query_container_flag', C.get_last_error())
            if not is_container.value:
                return None
            if self.token_info(token, 31, None, 0, C.byref(length)) or C.get_last_error() != 122:
                raise _invalid_identity('size_container_identity', 'Cannot size the AppContainer token information')
            if not C.sizeof(C.c_void_p) <= length.value <= 65536:
                raise _invalid_identity('validate_container_identity_size', 'Invalid AppContainer token information size')
            buffer = C.create_string_buffer(length.value)
            if not self.token_info(token, 31, buffer, len(buffer), C.byref(length)):
                raise _native_failure('PYTHON_APPCONTAINER_TOKEN_QUERY_FAILED', 'query_container_identity', C.get_last_error())
            sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            text = W.LPWSTR()
            if not sid or not self.sid_text(sid, C.byref(text)):
                raise _native_failure('PYTHON_APPCONTAINER_IDENTITY_INVALID', 'convert_container_sid', C.get_last_error())
            try:
                result = text.value
                if not result or not result.startswith("S-1-15-2-") or result in {"S-1-15-2-1", "S-1-15-2-2"}:
                    raise _invalid_identity('validate_container_sid', 'A specific AppContainer identity is required')
                return result
            finally:
                self.free(C.cast(text, C.c_void_p))
        finally:
            self.close(token)

    def mkdir(self, path, sid):
        # Match CPython's private DACL, retaining SYSTEM, Administrators and
        # OWNER RIGHTS; the additional principal is this exact token's SID.
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)(A;OICI;FA;;;{sid})"
        descriptor = C.c_void_p()
        if not self.descriptor(sddl, 1, C.byref(descriptor), None):
            raise _native_failure('PYTHON_PRIVATE_DACL_INVALID', 'compile_private_dacl', C.get_last_error(), path)
        try:
            attributes = _SecurityAttributes(C.sizeof(_SecurityAttributes), descriptor, False)
            if not self.create(os.fsdecode(path), C.byref(attributes)):
                error = C.get_last_error()
                if error in {80, 183}:
                    exception = C.WinError(error)
                    exception.filename = path
                    raise exception
                code = 'PYTHON_PRIVATE_DIRECTORY_CREATE_DENIED' if error == 5 else 'PYTHON_PRIVATE_DIRECTORY_CREATE_FAILED'
                exception = _native_failure(code, 'create_private_directory', error, path)
                raise exception
        finally:
            self.free(descriptor)


def install_private_mkdir():
    """Install once in the managed child, and do nothing outside AppContainer."""
    if os.name != "nt" or getattr(os.mkdir, "_gitgo_private_directory", False):
        return False
    api = _PrivateDirectoryApi()
    sid = api.container_sid()
    if sid is None:
        return False
    original = os.mkdir

    def mkdir(path, mode=0o777, *, dir_fd=None):
        numeric_mode = operator.index(mode)
        if numeric_mode != 0o700 or dir_fd is not None:
            return original(path, numeric_mode, dir_fd=dir_fd)
        path = os.fspath(path)
        sys.audit("os.mkdir", path, numeric_mode, -1)
        return api.mkdir(path, sid)

    mkdir._gitgo_private_directory = True
    os.mkdir = mkdir
    return True
