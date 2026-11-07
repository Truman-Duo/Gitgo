"""Caller-owned scratch never crosses threads or overwrites reentrant results."""
import ctypes as C
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.core.native_sandbox import NativeLibrary, NativeBoundaryError


def library(call):
    value = NativeLibrary.__new__(NativeLibrary)
    value._sealed = False
    value._scratch = threading.local()
    value._acl = call
    return value


def test_reentrant_acl_result_does_not_overwrite_outer_result():
    def call(op, token, raw, size, output, capacity, written):
        C.memmove(output, b'outer' if token == 1 else b'inner', 5)
        written._obj.value = 5
        if token == 1:
            assert native.acl(1, 2) == b'inner'
        return 0
    native = library(call)
    assert native.acl(1, 1) == b'outer'
    assert native.acl(1, 1) == b'outer'


def test_parallel_acl_results_have_independent_storage():
    rendezvous = threading.Barrier(2)
    def call(op, token, raw, size, output, capacity, written):
        C.memmove(output, bytes([token]), 1)
        written._obj.value = 1
        rendezvous.wait(timeout=5)
        return 0
    native = library(call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(native.acl, 1, 65), pool.submit(native.acl, 1, 66)
        assert first.result(timeout=5) == b'A'
        assert second.result(timeout=5) == b'B'


def test_write_has_no_output_and_bad_length_seals_boundary():
    def write(op, token, raw, size, output, capacity, written):
        assert output is None and capacity == 0
        written._obj.value = 0
        return 0
    assert library(write).acl(5, 1, b'descriptor') == b''
    def forged(op, token, raw, size, output, capacity, written):
        written._obj.value = capacity + 1
        return 0
    native = library(forged)
    with pytest.raises(NativeBoundaryError, match='bounds'):
        native.acl(1, 1)
    with pytest.raises(NativeBoundaryError, match='sealed'):
        native.acl(1, 1)


def test_json_call_reentrant_response_survives_buffer_reuse():
    native = library(None)
    def call(raw, length, output, capacity, written):
        request = json.loads(raw)
        response = json.dumps({'ok': True, 'value': request['token']}).encode()
        C.memmove(output, response, len(response))
        written._obj.value = len(response)
        if request['token'] == 1:
            assert native.call(op='test', token=2)['value'] == 2
        return 0
    native._call = call
    assert native.call(op='test', token=1)['value'] == 1
    assert native.call(op='test', token=1)['value'] == 1


def test_json_call_parallel_responses_stay_on_their_caller_thread():
    native = library(None)
    rendezvous = threading.Barrier(2)
    def call(raw, length, output, capacity, written):
        response = json.dumps({'ok': True, 'value': json.loads(raw)['token']}).encode()
        C.memmove(output, response, len(response))
        written._obj.value = len(response)
        rendezvous.wait(timeout=5)
        return 0
    native._call = call
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(native.call, op='test', token=1)
        second = pool.submit(native.call, op='test', token=2)
        assert first.result(timeout=5)['value'] == 1
        assert second.result(timeout=5)['value'] == 2
