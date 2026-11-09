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


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows image handle attenuation')
def test_launcher_image_pins_cannot_export_file_data(native):
    from contextlib import ExitStack
    from backend.core.native_sandbox import pin_windows_runtime_images
    from backend.core.sandbox_read_windows import WindowsReadApi
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.ReadFile.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p]
    kernel.ReadFile.restype = C.c_int
    with ExitStack() as owners:
        handles = pin_windows_runtime_images(owners)
        assert 1 <= len(handles) <= 16
        for handle in handles:
            WindowsReadApi().identity(handle, directory=False)
            output, size = C.create_string_buffer(1), C.c_uint32()
            assert not kernel.ReadFile(handle, output, 1, C.byref(size), None)
            assert C.get_last_error() == 5  # READ_ATTRIBUTES is not READ_DATA.


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
    assert report['safe_read_version'] == 2
    assert report['linux_cgroup_version'] == (2 if sys.platform == 'linux' else 0)
    assert report['linux_filter_version'] == (1 if sys.platform == 'linux' else 0)
    assert report['linux_directory_version'] == (1 if sys.platform == 'linux' else 0)


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
def test_low_level_anonymous_job_preserves_existing_contract(native):
    job = native.call(op='job_create', recovery_id='', processes=4, memory=128*1024*1024, cpu_seconds=5)['token']
    assert native.call(op='job_retire', token=job)['complete'] is True
    with pytest.raises(NativeBoundaryError):
        native.call(op='job_recover', recovery_id='')


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


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows Job and ACL ownership')
def test_retired_jobs_do_not_exhaust_native_capacity(native, tmp_path_factory):
    path = tmp_path_factory / 'acl-target'
    path.write_bytes(b'owned')
    request = dict(op='job_create', recovery_id='', processes=4,
                   memory=128 * 1024 * 1024, cpu_seconds=5)
    for _ in range(4_100):
        token = native.call(**request)['token']
        assert native.call(op='job_retire', token=token)['complete'] is True
    assert native.call(op='job_retire', token=token)['complete'] is True
    with pytest.raises(NativeBoundaryError):
        native.call(op='process_release', token=token)
    file = native.call(op='acl_open', path=str(path), access=0x20089)['token']
    try:
        with pytest.raises(NativeBoundaryError):
            native.call(op='job_retire', token=file)
        with pytest.raises(NativeBoundaryError):
            native.call(op='acl_release', token=token)
    finally:
        native.call(op='acl_release', token=file)
    assert native.call(op='acl_release', token=file)['ok'] is True


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows loader pin')
def test_loaded_image_cannot_be_replaced_or_written(native):
    image = installed_path()
    alternate = image.with_name('pin-control-' + uuid.uuid4().hex + '.dll')
    renamed = alternate.with_suffix('.renamed')
    # Same-directory positive control: cross-volume rename errors or an
    # unwritable directory cannot masquerade as a successful loader pin.
    alternate.write_bytes(b'positive-control')
    alternate.rename(renamed)
    renamed.unlink()
    with pytest.raises(OSError):
        with image.open('r+b') as stream:
            stream.write(b'replace')
    with pytest.raises(OSError):
        image.rename(alternate)
    assert native.call(op='capabilities')['ok'] is True


def test_unverified_loaded_image_is_not_loaded_again(monkeypatch):
    from backend.core import native_sandbox

    monkeypatch.setattr(native_sandbox, '_loaded', None)
    monkeypatch.setattr(native_sandbox, '_failed_loads', [object()])
    monkeypatch.setattr(native_sandbox, 'NativeLibrary',
                        lambda _: pytest.fail('attempted another load after failed verification'))
    with pytest.raises(NativeBoundaryError, match='failed verification after load'):
        native_sandbox.get_native()
