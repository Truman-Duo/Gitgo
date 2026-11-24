"""Pure decoding reuse cannot cache live filesystem/security authority."""
import base64
import struct
from dataclasses import dataclass

import pytest

from backend.core.windows_acl import SecurityTree, SecurityConflict
from backend.core.windows_security_descriptor import SecurityDescriptor, SE_SELF_RELATIVE


@dataclass(frozen=True)
class Parsed:
    raw: bytes

    def comparison_key(self):
        return self.raw


class ParsingProbe(SecurityTree):
    def __init__(self):
        self.parses = 0
        self.reads = 0
        self.current = None
        self.handles = {}
        self.ancestors = {'parent': object()}

    def _validated(self, raw):
        self.parses += 1
        return Parsed(raw)

    def _text(self, raw):
        return raw.hex()

    def _file_identity(self, handle):
        return self.current['device'], self.current['inode']

    def _security_bytes(self, handle):
        self.reads += 1
        return base64.b64decode(self.current['descriptor'])

    def _observations(self, handles):
        for handle in handles:
            yield (*self._file_identity(handle), self._security_bytes(handle))


def record(raw):
    return {'path': 'parent', 'device': 1, 'inode': 'same-pinned-object',
        'descriptor': base64.b64encode(raw).decode(), 'sddl': raw.hex()}


def test_identical_bytes_reuse_parsing_but_changed_text_or_bytes_do_not():
    tree = ParsingProbe()
    first = record(b'first')
    parsed = tree.record_descriptor(first)
    assert tree.record_descriptor(dict(first, path='another-pinned-object')) is parsed
    assert tree.parses == 1
    with pytest.raises(ValueError, match='diagnostic text'):
        tree.record_descriptor({**first, 'sddl': 'a forged display'})
    assert tree.record_descriptor(record(b'changed')).raw == b'changed'
    with pytest.raises(ValueError, match='encoding'):
        tree.record_descriptor({**first, 'descriptor': 'not base64!'})


def test_bound_expected_state_still_reads_live_boundaries_and_rejects_drift():
    tree = ParsingProbe()
    original = record(b'original')
    tree.current = dict(original)
    bound = tree._bind_boundaries([original])
    assert tree.reads == 0 and tree.parses == 1
    tree._verify_boundaries(bound)
    tree._verify_boundaries(bound)
    assert tree.reads == 2
    tree.current = record(b'changed outside this transaction')
    with pytest.raises(SecurityConflict):
        tree._verify_boundaries(bound)
    assert tree.reads == 3
    tree.current = {**original, 'inode': 'replacement-object'}
    with pytest.raises(SecurityConflict):
        tree._verify_boundaries(bound)


def test_descriptor_reuse_is_bounded_and_large_records_are_not_retained():
    tree = ParsingProbe()
    for value in range(tree.descriptor_cache_entries + 10):
        tree.record_descriptor(record(str(value).encode()))
    assert len(tree._descriptor_cache) <= tree.descriptor_cache_entries
    assert tree._descriptor_cache_size <= tree.descriptor_cache_bytes
    before = len(tree._descriptor_cache)
    tree.record_descriptor(record(b'x' * tree.descriptor_cache_bytes))
    assert len(tree._descriptor_cache) == before


def test_native_validity_reuse_is_only_for_identical_immutable_bytes():
    tree = SecurityTree.__new__(SecurityTree)
    calls = []
    tree.valid_descriptor = lambda buffer: calls.append('validate') or True
    tree.dacl = tree.sacl = lambda *args: True
    sid = b'\x01\x01' + (5).to_bytes(6, 'big') + struct.pack('<I', 18)
    original = SecurityDescriptor(SE_SELF_RELATIVE, 0, sid, sid, None, None).to_bytes()
    first = tree._validated(original)
    assert tree._validated(original) is first and len(calls) == 1
    changed = SecurityDescriptor(SE_SELF_RELATIVE, 1, sid, sid, None, None).to_bytes()
    assert tree._validated(changed) != first and len(calls) == 2
    for invalid in (b'', original[:15]):
        with pytest.raises(ValueError):
            tree._validated(invalid)
    assert b'' not in tree._validated_cache
    assert tree._validated_cache_size <= tree.descriptor_cache_bytes


def test_diagnostic_cache_reuses_rust_conversion_without_caching_failures(monkeypatch):
    from backend.core import windows_acl_native
    tree = SecurityTree.__new__(SecurityTree)
    conversions = []
    def convert(raw):
        conversions.append(raw)
        if raw == b'invalid':
            raise OSError('native descriptor conversion rejected')
        return raw.hex()
    monkeypatch.setattr(windows_acl_native, 'descriptor_text', convert)
    assert tree._text(b'first') == tree._text(b'first') == b'first'.hex()
    assert conversions == [b'first']
    assert tree._text(b'changed') == b'changed'.hex()
    for _ in range(2):
        with pytest.raises(OSError, match='native descriptor'):
            tree._text(b'invalid')
    assert conversions.count(b'invalid') == 2
    for index in range(tree.descriptor_cache_entries + 10):
        tree._text(str(index).encode())
    assert len(tree._text_cache) <= tree.descriptor_cache_entries
    assert tree._text_cache_size <= tree.descriptor_cache_bytes


def test_close_keeps_failed_rust_ownership_for_retry_and_clears_pure_caches():
    from backend.core.windows_acl_native import AclHandle
    from types import SimpleNamespace
    tree = SecurityTree.__new__(SecurityTree)
    attempts = []
    def call(*, op, token):
        assert op == 'acl_release'
        attempts.append(token)
        if token == 1 and attempts.count(1) == 1:
            raise OSError('release not confirmed')
    library = SimpleNamespace(call=call)
    failed, successful = AclHandle(10, 1, library), AclHandle(20, 2, library)
    tree.handles, tree.ancestors = {'failed': failed}, {'successful': successful}
    tree.record_descriptor = lambda _: None
    for name in ('_descriptor_cache', '_validated_cache', '_text_cache'):
        setattr(tree, name, {'pure': object()})
        setattr(tree, name+'_size', 99)
    with pytest.raises(OSError, match='not confirmed'):
        tree.close()
    assert tree.handles == {'failed': failed} and not failed.closed
    assert not tree.ancestors and successful.closed
    assert all(not getattr(tree, name) and getattr(tree, name+'_size') == 0
               for name in ('_descriptor_cache', '_validated_cache', '_text_cache'))
    tree.close()
    tree.close()
    assert not tree.handles and failed.closed
    assert attempts.count(1) == 2 and attempts.count(2) == 1
