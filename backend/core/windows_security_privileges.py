"""Temporary backup/security privileges for the Windows operator thread only."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import os


_PRIVILEGES = ('SeSecurityPrivilege', 'SeBackupPrivilege', 'SeRestorePrivilege')
_TOKEN_DUPLICATE, _TOKEN_IMPERSONATE, _TOKEN_QUERY = 2, 4, 8
_TOKEN_ADJUST_PRIVILEGES = 32
_ERROR_NO_TOKEN, _ERROR_NOT_ALL_ASSIGNED = 1008, 1300


class ThreadTokenRestoreError(OSError):
    """Fatal operator error: no further ACL or privileged work may run."""


class _Luid(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.LONG)]


class _LuidAttributes(C.Structure):
    _fields_ = [('luid', _Luid), ('attributes', W.DWORD)]


class _TokenPrivileges(C.Structure):
    _fields_ = [('count', W.DWORD), ('privileges', _LuidAttributes * len(_PRIVILEGES))]


def _fn(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


def _check(ok, operation):
    if not ok:
        error = C.get_last_error()
        raise C.WinError(error, operation + ': ' + C.FormatError(error).strip())


@contextmanager
def security_privileges():
    """Enable existing operator privileges on a private impersonation token.

    The caller must enter and leave this context on the same OS thread. Each
    nesting level duplicates the current effective token and restores its
    predecessor. The process token is never adjusted. Missing privileges fail
    before impersonation; this helper never requests elevation or adds rights.
    A failure to restore the original thread context is fatal to the operator
    operation and must not be caught to continue performing privileged work.
    """
    if os.name != 'nt':
        raise OSError('Windows security privileges are required')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    advapi = C.WinDLL('advapi32', use_last_error=True)
    process = _fn(kernel, 'GetCurrentProcess', [], W.HANDLE)
    thread = _fn(kernel, 'GetCurrentThread', [], W.HANDLE)
    close = _fn(kernel, 'CloseHandle', [W.HANDLE], W.BOOL)
    open_thread = _fn(advapi, 'OpenThreadToken',
        [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL)
    open_process = _fn(advapi, 'OpenProcessToken',
        [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
    duplicate = _fn(advapi, 'DuplicateTokenEx',
        [W.HANDLE, W.DWORD, C.c_void_p, C.c_int, C.c_int, C.POINTER(W.HANDLE)], W.BOOL)
    lookup = _fn(advapi, 'LookupPrivilegeValueW',
        [W.LPCWSTR, W.LPCWSTR, C.POINTER(_Luid)], W.BOOL)
    adjust = _fn(advapi, 'AdjustTokenPrivileges',
        [W.HANDLE, W.BOOL, C.POINTER(_TokenPrivileges), W.DWORD,
         C.c_void_p, C.c_void_p], W.BOOL)
    set_thread = _fn(advapi, 'SetThreadToken',
        [C.POINTER(W.HANDLE), W.HANDLE], W.BOOL)
    revert = _fn(advapi, 'RevertToSelf', [], W.BOOL)
    original, private = W.HANDLE(), W.HANDLE()
    impersonated = False
    try:
        # OpenAsSelf changes the access check, not which token is duplicated.
        # An existing restricted/impersonated identity is never bypassed.
        had_thread_token = bool(open_thread(thread(),
            _TOKEN_QUERY | _TOKEN_DUPLICATE | _TOKEN_IMPERSONATE,
            True, C.byref(original)))
        if not had_thread_token:
            error = C.get_last_error()
            if error != _ERROR_NO_TOKEN:
                _check(False, 'Cannot preserve the current Windows thread token')
            _check(open_process(process(), _TOKEN_QUERY | _TOKEN_DUPLICATE,
                C.byref(original)), 'Cannot open the Windows operator token')
        access = _TOKEN_QUERY | _TOKEN_DUPLICATE | _TOKEN_IMPERSONATE | _TOKEN_ADJUST_PRIVILEGES
        # SecurityImpersonation=2, TokenImpersonation=2; NULL attributes make
        # the duplicate handle non-inheritable.
        _check(duplicate(original, access, None, 2, 2, C.byref(private)),
            'Cannot duplicate the Windows operator token')
        privileges = _TokenPrivileges()
        privileges.count = len(_PRIVILEGES)
        for index, name in enumerate(_PRIVILEGES):
            _check(lookup(None, name, C.byref(privileges.privileges[index].luid)),
                'Cannot look up ' + name)
            privileges.privileges[index].attributes = 2  # SE_PRIVILEGE_ENABLED
        C.set_last_error(0)
        _check(adjust(private, False, C.byref(privileges), 0, None, None),
            'Cannot enable Windows ACL management privileges')
        error = C.get_last_error()
        if error == _ERROR_NOT_ALL_ASSIGNED:
            raise PermissionError('Windows ACL management requires ' + ', '.join(_PRIVILEGES) +
                ' in the current operator token; use an authorized elevated operator terminal')
        if error:
            raise C.WinError(error, 'Windows ACL management privilege validation failed')
        _check(set_thread(None, private), 'Cannot install the private Windows operator token')
        impersonated = True
        yield
    finally:
        try:
            if impersonated:
                restored = set_thread(None, original) if had_thread_token else revert()
                if not restored:
                    error = C.get_last_error()
                    raise ThreadTokenRestoreError(error,
                        'Cannot restore the original Windows thread token; stop this operator process')
        finally:
            if private.value:
                close(private)
            if original.value:
                close(original)
