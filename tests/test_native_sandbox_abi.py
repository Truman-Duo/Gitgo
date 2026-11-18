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
from tests.test_native_sandbox import isolated_python, native_box  # noqa: F401


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


# Use a fresh module instance so the first Job/ACL allocations exercise the
# historical numeric collisions regardless of the surrounding test order.
_CROSS_DOMAIN_SOURCE = r'''
from contextlib import ExitStack
import ctypes as C
import hashlib
from pathlib import Path
import sys
from backend.core.native_sandbox import get_native, NativeBoundaryError
from backend.core.sandbox_read_native import NativeReadRoot, _path

directory, operation, kind, state = Path(sys.argv[1]), *sys.argv[2:]
body = b'read-object'
(directory / 'read-target').write_bytes(body)
(directory / 'acl-target').write_bytes(b'acl-object')
native = get_native()

def refused(call, *, acl_plane=False):
    try:
        call()
    except NativeBoundaryError as error:
        if acl_plane:
            assert error.errno == 6, error
        else:
            assert not error.outcome_unknown, error
    else:
        raise AssertionError(operation + ' accepted a token from another resource domain')

with ExitStack() as owners:
    root = owners.enter_context(NativeReadRoot(directory))
    readers = [owners.enter_context(root.open('read-target')) for _ in range(6)]
    job = native.call(op='job_create', recovery_id='', processes=4,
        memory=128*1024*1024, cpu_seconds=5)['token']
    owners.callback(native.call, op='job_retire', token=job)
    acl = native.call(op='acl_open', path=str(directory / 'acl-target'), access=0x20089)['token']
    owners.callback(native.call, op='acl_release', token=acl)
    acl_before = native.acl(4, token=acl)
    closed_reader = None
    if kind in ('job', 'acl'):
        foreign = job if kind == 'job' else acl
        if state == 'retired':
            native.call(op='job_retire' if kind == 'job' else 'acl_release', token=foreign)
        if operation in ('bytes', 'package_bytes'):
            function = (native._library.gitgo_native_read if operation == 'bytes'
                        else native._library.gitgo_native_package_read)
            function.argtypes = [C.c_uint64, C.c_size_t, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
            function.restype = C.c_uint32
            output, written = C.create_string_buffer(b'UNCHANGED', 16), C.c_size_t(99)
            assert function(foreign, 16, output, len(output), C.byref(written)) == 3
            assert written.value == 0 and output.value == b'UNCHANGED'
        else:
            extra = ({'path': _path('read-target'), 'metadata_only': False} if operation == 'read_open'
                     else {'max_bytes': 1024} if operation in ('read_hash', 'read_size') else {})
            refused(lambda: native.call(op=operation, token=foreign, **extra))
    else:
        # In the old untyped table these are read tokens 5 and 7, aliasing the
        # first live Job and ACL object respectively.
        selected = readers[3 if operation.startswith('job_') else 5]
        foreign = selected._token
        if state == 'retired':
            selected.close()
            closed_reader = selected
        if operation == 'acl_get':
            refused(lambda: native.acl(4, token=foreign), acl_plane=True)
        else:
            refused(lambda: native.call(op=operation, token=foreign))
    # A refusal must leave every unrelated live resource usable.
    for reader in readers:
        if reader is not closed_reader:
            assert reader.sha256() == (hashlib.sha256(body).hexdigest(), len(body))
    if not (kind == 'job' and state == 'retired'):
        assert native.call(op='job_query', token=job)['active'] == 0
    if not (kind == 'acl' and state == 'retired'):
        assert native.acl(4, token=acl) == acl_before
'''


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows Job and ACL domains')
@pytest.mark.parametrize('state', ['live', 'retired'])
@pytest.mark.parametrize('kind,operation', [
    *[(kind, operation) for kind in ('job', 'acl')
      for operation in ('read_open', 'read_size', 'read_hash', 'read_close', 'bytes', 'package_bytes')],
    *[('read', operation) for operation in ('job_query', 'job_retire', 'acl_release', 'acl_get')],
])
def test_cross_domain_read_operations_have_no_effect(native, tmp_path_factory, kind, operation, state):
    result = subprocess.run([sys.executable, '-B', '-c', _CROSS_DOMAIN_SOURCE,
        str(tmp_path_factory), operation, kind, state],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows process ownership')
def test_live_process_tokens_cannot_select_or_retire_read_objects(native, native_box):
    from contextlib import ExitStack
    from backend.core.sandbox_read_native import NativeReadRoot
    from tests.test_native_sandbox import finished

    workspace, _, spawn = native_box
    (workspace / 'note').write_bytes(b'owned-read')
    with ExitStack() as owners:
        root = owners.enter_context(NativeReadRoot(workspace))
        readers = [owners.enter_context(root.open('note')) for _ in range(6)]
        proc = spawn("import sys;print('ready',flush=True);sys.stdin.read()")
        try:
            process_token = proc._handle.token
            for operation in ('read_close', 'read_hash', 'read_size'):
                extra = {'max_bytes': 1024} if operation != 'read_close' else {}
                with pytest.raises(NativeBoundaryError):
                    native.call(op=operation, token=process_token, **extra)
            for item in [root, *readers]:
                with pytest.raises(NativeBoundaryError):
                    native.call(op='process_release', token=item._token)
            assert proc.poll() is None
            assert all(reader.read_bytes() == b'owned-read' for reader in readers)
        finally:
            code, out, err = finished(proc)
        assert code == 0 and out.strip() == 'ready', err
        proc._handle.Close()
        with pytest.raises(NativeBoundaryError):
            native.call(op='read_close', token=process_token)
