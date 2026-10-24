"""Process-local retained installation descriptors, not a new state authority.

Only a validated installation gate registers descriptors. No environment value
alone can register one. Owned child creation preserves the lease across exec;
the registration/ledger authority remains in backend.core.update.
"""
from contextlib import contextmanager
import os
import threading

_guard = threading.RLock()
_descriptors: dict[int, int] = {}


@contextmanager
def retain_descriptor(descriptor: int):
    if os.name not in {"posix", "nt"} or type(descriptor) is not int or descriptor < 3:
        raise ValueError("Invalid installation lease descriptor")
    os.fstat(descriptor)
    with _guard:
        _descriptors[descriptor] = _descriptors.get(descriptor, 0) + 1
    try:
        yield
    finally:
        with _guard:
            count = _descriptors[descriptor] - 1
            if count: _descriptors[descriptor] = count
            else: del _descriptors[descriptor]


def runtime_descriptors() -> tuple[int, ...]:
    with _guard:
        return tuple(sorted(_descriptors))


def child_options() -> dict:
    descriptors = runtime_descriptors()
    # Windows managed spawning translates only this process's registered FDs
    # to an explicit native handle allowlist, never raw environment assertions.
    return {"pass_fds": descriptors} if descriptors and os.name == "posix" else {}

@contextmanager
def windows_sandbox_lease_handles():
    """Attenuate registered leases to metadata-only inherited capabilities.

    Duplicates retain the same kernel sharing reservation. They grant no lock
    contents, mutation, DELETE, WRITE_DAC or WRITE_OWNER authority to tool code.
    """
    import ctypes as C
    if os.name != "nt":
        raise OSError("Windows lease handles require Windows")
    descriptors=runtime_descriptors()
    if len(descriptors)>16:raise ValueError("Too many registered sandbox leases")
    kernel=C.WinDLL("kernel32",use_last_error=True)
    duplicate=kernel.DuplicateHandle
    duplicate.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_uint32,C.c_int,C.c_uint32]
    duplicate.restype=C.c_int
    kernel.GetCurrentProcess.restype=C.c_void_p
    kernel.CloseHandle.argtypes=[C.c_void_p];kernel.CloseHandle.restype=C.c_int
    import msvcrt
    handles=[]
    try:
        process=kernel.GetCurrentProcess()
        for descriptor in descriptors:
            handle=C.c_void_p()
            # SYNCHRONIZE | READ_CONTROL | FILE_READ_ATTRIBUTES, no READ_DATA.
            if not duplicate(process,msvcrt.get_osfhandle(descriptor),process,
                             C.byref(handle),0x00120080,False,0):
                raise C.WinError(C.get_last_error())
            handles.append(handle.value)
        yield tuple(handles)
    finally:
        for handle in handles:kernel.CloseHandle(handle)
