"""Exercise the actual Rust binary, never a simulated native implementation."""
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from backend.core.native_sandbox import NativeBoundaryError, get_native, installed_path


@pytest.fixture
def native():
    if not installed_path().is_file():
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            pytest.fail('CI must build the native binary before testing')
        pytest.skip('Build scripts/build_sandbox_native.py first')
    return get_native()


def test_actual_module_identity_and_capabilities(native):
    report = native.call(op='capabilities')
    assert report['abi'] == native.identity['abi_version'] == 1
    assert len(native.identity['sha256']) == 64
    assert native.identity['size'] > 0
    assert report['windows_appcontainer'] is (sys.platform == 'win32')
    assert report['linux_execution'] is False
    assert report['macos_execution'] is False


def test_unknown_fields_and_operation_are_denied(native):
    for request in ({'op': 'capabilities', 'allow': True}, {'op': 'raw_exec', 'shell': 'echo escaped'},
                    {'op': 'job_create', 'recovery_id': 'x'}):
        with pytest.raises(NativeBoundaryError):
            native.call(**request)


def test_undersized_response_is_rejected_before_effect(native):
    # Native call must never execute once for sizing and a second time for data.
    request = json.dumps({'op': 'capabilities'}).encode()
    output, size = C.create_string_buffer(1), C.c_size_t(99)
    assert native._call(request, len(request), output, 1, C.byref(size)) != 0
    assert size.value == 99
    assert native.call(op='capabilities')['ok'] is True


@pytest.mark.skipif(sys.platform == 'win32', reason='Unsupported-platform rejection')
def test_posix_abi_never_exposes_unisolated_execution(native):
    with pytest.raises(NativeBoundaryError, match='unsupported_platform'):
        native.call(op='job_retire', token=1)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows Job ownership')
def test_job_retirement_tokens_and_cold_recovery(native):
    recovery = uuid.uuid4().hex
    request = dict(op='job_create', recovery_id=recovery, processes=8, memory=256*1024*1024, cpu_seconds=10)
    job = native.call(**request)['token']
    try:
        with pytest.raises(NativeBoundaryError):
            native.call(**request)  # Never adopt an existing named boundary.
        assert native.call(op='job_query', token=job)['active'] == 0
        # A fresh interpreter reopens only the existing durable recovery name,
        # never launches an unknown invocation or invents a second journal.
        source = ('from backend.core.native_sandbox import get_native;'
                  f"n=get_native();r=n.call(op='job_recover',recovery_id={recovery!r});"
                  "assert r['absent'] is False;assert n.call(op='job_retire',token=r['token'])['complete']")
        subprocess.run([sys.executable, '-B', '-c', source], cwd=Path(__file__).resolve().parents[1], check=True)
        assert native.call(op='job_retire', token=job)['complete'] is True
        assert native.call(op='job_retire', token=job)['complete'] is True
        with pytest.raises(NativeBoundaryError):
            native.call(op='job_query', token=job)
    finally:
        native.call(op='job_retire', token=job)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows loader pin')
def test_loaded_image_cannot_be_replaced_or_written(native, tmp_path):
    image = installed_path()
    with pytest.raises(OSError):
        with image.open('r+b') as stream:
            stream.write(b'replace')
    with pytest.raises(OSError):
        image.rename(tmp_path / image.name)
    assert native.call(op='capabilities')['ok'] is True
