"""Real private-handle binding; selectors alone never authorize admission."""
import ctypes as C
import os
import pytest
from backend.core.update import windows_storage as win
from backend.core.update.sandbox_admission import (
    _matching_descriptor, _native_name, inherited_sandbox_runtime, LEASE_HANDLES_ENV)
from backend.core.update.validation import UpdateError

pytestmark=pytest.mark.skipif(not win.supported(),reason="Native Windows handles required")


def duplicate(descriptor):
    import msvcrt
    return duplicate_handle(msvcrt.get_osfhandle(descriptor))


def duplicate_handle(source):
    handle=C.c_void_p()
    call=C.WinDLL("kernel32",use_last_error=True).DuplicateHandle
    call.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_uint32,C.c_int,C.c_uint32]
    call.restype=C.c_int
    process=win.api()["GetCurrentProcess"]()
    assert call(process,source,process,C.byref(handle),0,False,2)
    return handle.value


def test_a_selector_does_not_admit_an_unrestricted_process(monkeypatch):
    monkeypatch.setenv(LEASE_HANDLES_ENV,"12345")
    with inherited_sandbox_runtime("tool-runner") as admitted:assert admitted is False


def test_inherited_control_handle_is_bound_to_its_real_native_name(tmp_path_factory):
    import msvcrt
    base=tmp_path_factory/"private";win.create_private_directory(base)
    path=base/"runtime.lock"
    descriptor=win.open_file(path,write=True,create=True)
    try:
        expected=_native_name(msvcrt.get_osfhandle(descriptor))
        owned=_matching_descriptor(duplicate(descriptor),expected)
        try:
            assert os.fstat(owned).st_ino==os.fstat(descriptor).st_ino
            assert not os.get_inheritable(owned)
        finally:os.close(owned)
        with pytest.raises(UpdateError,match="UPDATE_PROBE_LEASE"):
            _matching_descriptor(duplicate(descriptor),expected.parent/"other.lock")
    finally:os.close(descriptor)


def test_pipe_or_unrelated_handle_is_not_managed_authority(tmp_path_factory):
    reader,writer=os.pipe()
    try:
        with pytest.raises(UpdateError,match="UPDATE_PROBE_LEASE"):
            _matching_descriptor(duplicate(reader),tmp_path_factory/"runtime.lock")
    finally:os.close(reader);os.close(writer)

def test_inherited_sandbox_capability_has_metadata_but_no_contents_or_write(tmp_path_factory):
    import msvcrt
    from backend.core.installation_lease import retain_descriptor,windows_sandbox_lease_handles
    base=tmp_path_factory/"private";win.create_private_directory(base)
    path=base/"runtime.lock"
    original=win.open_file(path,create=True,write=True)
    try:os.write(original,b"synthetic control bytes")
    finally:os.close(original)
    original=win.open_file(path,share=1)
    try:
        expected=_native_name(msvcrt.get_osfhandle(original))
        with retain_descriptor(original),windows_sandbox_lease_handles() as handles:
            assert len(handles)==1
            descriptor=_matching_descriptor(duplicate_handle(handles[0]),expected)
            try:
                with pytest.raises(OSError):os.read(descriptor,1)
                with pytest.raises(OSError):os.write(descriptor,b"x")
            finally:os.close(descriptor)
    finally:os.close(original)
