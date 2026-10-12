"""Native token isolation tests; these never change filesystem permissions."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import os
import sys

import pytest

from backend.core.windows_security_privileges import security_privileges

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows token isolation')

_REQUIRED = ('SeSecurityPrivilege', 'SeBackupPrivilege', 'SeRestorePrivilege')
_QUERY, _DUPLICATE, _IMPERSONATE, _ADJUST = 8, 2, 4, 32
_ENABLED, _REMOVED, _NO_TOKEN = 2, 4, 1008


class _Luid(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.LONG)]

    def key(self):
        return self.low, self.high


class _Privilege(C.Structure):
    _fields_ = [('luid', _Luid), ('attributes', W.DWORD)]


class _Privileges(C.Structure):
    _fields_ = [('count', W.DWORD), ('entries', _Privilege * 3)]


class _Statistics(C.Structure):
    _fields_ = [
        ('token_id', _Luid), ('authentication_id', _Luid),
        ('expiration', C.c_longlong), ('token_type', C.c_int),
        ('impersonation_level', C.c_int), ('dynamic_charged', W.DWORD),
        ('dynamic_available', W.DWORD), ('group_count', W.DWORD),
        ('privilege_count', W.DWORD), ('modified_id', _Luid),
    ]


class _Tokens:
    """Independent readback and cleanup, without patching the implementation."""

    def __init__(self):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        self.process = self.bind(self.kernel, 'GetCurrentProcess', [], W.HANDLE)
        self.thread = self.bind(self.kernel, 'GetCurrentThread', [], W.HANDLE)
        self.close = self.bind(self.kernel, 'CloseHandle', [W.HANDLE], W.BOOL)
        self.open_process = self.bind(self.advapi, 'OpenProcessToken',
            [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
        self.open_thread = self.bind(self.advapi, 'OpenThreadToken',
            [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL)
        self.duplicate = self.bind(self.advapi, 'DuplicateTokenEx',
            [W.HANDLE, W.DWORD, C.c_void_p, C.c_int, C.c_int, C.POINTER(W.HANDLE)], W.BOOL)
        self.set_thread = self.bind(self.advapi, 'SetThreadToken',
            [C.POINTER(W.HANDLE), W.HANDLE], W.BOOL)
        self.info = self.bind(self.advapi, 'GetTokenInformation',
            [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        self.lookup = self.bind(self.advapi, 'LookupPrivilegeValueW',
            [W.LPCWSTR, W.LPCWSTR, C.POINTER(_Luid)], W.BOOL)
        self.adjust = self.bind(self.advapi, 'AdjustTokenPrivileges',
            [W.HANDLE, W.BOOL, C.POINTER(_Privileges), W.DWORD, C.c_void_p, C.c_void_p], W.BOOL)
        self.required = {name: self.luid(name) for name in _REQUIRED}

    @staticmethod
    def bind(dll, name, arguments, result):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = arguments, result
        return fn

    @staticmethod
    def check(ok):
        if not ok:
            raise C.WinError(C.get_last_error())

    def luid(self, name):
        value = _Luid()
        self.check(self.lookup(None, name, C.byref(value)))
        return value.key()

    def thread_handle(self):
        handle = W.HANDLE()
        if self.open_thread(self.thread(), _QUERY | _DUPLICATE | _IMPERSONATE,
                            True, C.byref(handle)):
            return handle
        error = C.get_last_error()
        if error != _NO_TOKEN:
            raise C.WinError(error)
        return None

    @contextmanager
    def process_handle(self):
        handle = W.HANDLE()
        self.check(self.open_process(self.process(), _QUERY | _DUPLICATE, C.byref(handle)))
        try:
            yield handle
        finally:
            self.check(self.close(handle))

    def information(self, handle, kind):
        size = W.DWORD()
        C.set_last_error(0)
        assert not self.info(handle, kind, None, 0, C.byref(size))
        assert C.get_last_error() == 122  # ERROR_INSUFFICIENT_BUFFER
        assert size.value > 0
        buffer = C.create_string_buffer(size.value)
        self.check(self.info(handle, kind, buffer, len(buffer), C.byref(size)))
        return buffer.raw[:size.value]

    def snapshot(self, handle):
        stats = _Statistics.from_buffer_copy(self.information(handle, 10))  # TokenStatistics
        data = self.information(handle, 3)  # TokenPrivileges
        count = W.DWORD.from_buffer_copy(data).value
        offset, stride = _Privileges.entries.offset, C.sizeof(_Privilege)
        assert len(data) >= offset + count * stride
        privileges = {}
        for index in range(count):
            entry = _Privilege.from_buffer_copy(data, offset + index * stride)
            privileges[entry.luid.key()] = entry.attributes
        return stats.token_id.key(), privileges

    def process_snapshot(self):
        with self.process_handle() as handle:
            return self.snapshot(handle)

    def thread_snapshot(self):
        handle = self.thread_handle()
        if handle is None:
            return None
        try:
            return self.snapshot(handle)
        finally:
            self.check(self.close(handle))

    @contextmanager
    def preserve_thread(self):
        """Restore the actual incoming identity even if the helper's cleanup fails."""
        original = self.thread_handle()
        try:
            yield
        finally:
            try:
                self.check(self.set_thread(None, original))
            finally:
                if original is not None:
                    self.check(self.close(original))

    @contextmanager
    def private_thread(self, *, remove_required=False):
        """Install an owned duplicate; never adjust the process or incoming token."""
        private = W.HANDLE()
        with self.preserve_thread():
            source = self.thread_handle()
            try:
                if source is None:
                    with self.process_handle() as handle:
                        self.check(self.duplicate(handle, _QUERY | _DUPLICATE | _IMPERSONATE | _ADJUST,
                                                  None, 2, 2, C.byref(private)))
                else:
                    self.check(self.duplicate(source, _QUERY | _DUPLICATE | _IMPERSONATE | _ADJUST,
                                              None, 2, 2, C.byref(private)))
                if remove_required:
                    # Removal is irreversible on this private token. Disabling is
                    # insufficient: the helper is expected to enable held rights.
                    privileges = _Privileges()
                    privileges.count = 3
                    for entry, luid in zip(privileges.entries, self.required.values()):
                        entry.luid.low, entry.luid.high = luid
                        entry.attributes = _REMOVED
                    self.check(self.adjust(private, False, C.byref(privileges), 0, None, None))
                    assert not (self.required.values() & self.snapshot(private)[1].keys())
                self.check(self.set_thread(None, private))
                yield self.snapshot(private)
            finally:
                if source is not None:
                    self.check(self.close(source))
                if private.value:
                    self.check(self.close(private))

    def require_held_privileges(self):
        state = self.thread_snapshot() or self.process_snapshot()
        missing = [name for name, luid in self.required.items() if luid not in state[1]]
        if missing:
            message = 'Operator token lacks required native test privileges: ' + ', '.join(missing)
            if os.environ.get('GITHUB_ACTIONS', '').lower() == 'true':
                pytest.fail(message + '; hosted Windows privilege tests must run elevated')
            pytest.skip(message + '; no elevation is requested by this test')

    def assert_enabled(self, state):
        assert state is not None
        assert all(state[1].get(luid, 0) & _ENABLED for luid in self.required.values())


@pytest.fixture
def tokens():
    api = _Tokens()
    process_before, thread_before = api.process_snapshot(), api.thread_snapshot()
    with api.preserve_thread():
        yield api
    assert api.process_snapshot() == process_before
    assert api.thread_snapshot() == thread_before


def test_operator_attempt_never_changes_process_token(tokens):
    # Exercise both the ordinary-token refusal and elevated-token success on
    # their real native entry path, starting without a thread token.
    tokens.check(tokens.set_thread(None, None))
    before = tokens.process_snapshot()
    missing = any(luid not in before[1] for luid in tokens.required.values())
    if missing:
        with pytest.raises(PermissionError, match='current operator token'):
            with security_privileges():
                pytest.fail('Missing privileges must refuse before the body')
    else:
        with security_privileges():
            tokens.assert_enabled(tokens.thread_snapshot())
            assert tokens.process_snapshot() == before
    assert tokens.thread_snapshot() is None
    assert tokens.process_snapshot() == before


def test_missing_privileges_preserve_restricted_thread_identity(tokens):
    # A privileged process must not let this caller bypass its restricted
    # effective identity by reopening and using the process token instead.
    process_before = tokens.process_snapshot()
    with tokens.private_thread(remove_required=True) as restricted:
        with pytest.raises(PermissionError, match='current operator token'):
            with security_privileges():
                pytest.fail('The restricted caller must never enter the privileged body')
        assert tokens.thread_snapshot() == restricted
        assert tokens.process_snapshot() == process_before


def test_nested_privileges_restore_exact_predecessor_tokens(tokens):
    tokens.require_held_privileges()
    with tokens.private_thread() as incoming:
        with security_privileges():
            outer = tokens.thread_snapshot()
            tokens.assert_enabled(outer)
            assert outer[0] != incoming[0]
            with security_privileges():
                inner = tokens.thread_snapshot()
                tokens.assert_enabled(inner)
                assert inner[0] not in (incoming[0], outer[0])
            assert tokens.thread_snapshot() == outer
        assert tokens.thread_snapshot() == incoming


def test_nested_exception_restores_outer_and_incoming_tokens(tokens):
    tokens.require_held_privileges()
    with tokens.private_thread() as incoming:
        with security_privileges():
            outer = tokens.thread_snapshot()
            with pytest.raises(RuntimeError, match='native token cleanup sentinel'):
                with security_privileges():
                    tokens.assert_enabled(tokens.thread_snapshot())
                    raise RuntimeError('native token cleanup sentinel')
            assert tokens.thread_snapshot() == outer
        assert tokens.thread_snapshot() == incoming


def test_body_exception_removes_private_token_without_original(tokens):
    tokens.check(tokens.set_thread(None, None))
    tokens.require_held_privileges()
    with pytest.raises(RuntimeError, match='native token cleanup sentinel'):
        with security_privileges():
            tokens.assert_enabled(tokens.thread_snapshot())
            raise RuntimeError('native token cleanup sentinel')
    assert tokens.thread_snapshot() is None
