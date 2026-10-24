"""Managed admission for restricted Windows payloads via an inherited lease.

The parent has already validated the installation and retains the same kernel
sharing lease through its owned Job. A selector is never authority: the child
must be an actual AppContainer in a Job and possess the private lock handle
whose final native name belongs to its physically opened managed image.
"""
from contextlib import contextmanager, ExitStack
import ctypes as C
import os
from pathlib import Path
import stat
import sys
from .validation import reject

LEASE_HANDLES_ENV = "GITGO_SANDBOX_INSTALL_LEASE_HANDLES"


def in_appcontainer():
    from .windows_storage import api, _check, HANDLE, U32
    token, result, size = HANDLE(), U32(), U32()
    _check(api()["OpenProcessToken"](api()["GetCurrentProcess"](), 8, C.byref(token)))
    try:
        _check(api()["GetTokenInformation"](token, 29, C.byref(result), C.sizeof(result), C.byref(size)))
        return result.value == 1
    finally:api()["CloseHandle"](token)


def _native_name(handle):
    from .windows_storage import api
    name=C.create_unicode_buffer(32768)
    length=api()["GetFinalPathNameByHandleW"](handle,name,len(name),2)
    if not 0 < length < len(name) or not name.value.startswith("\\Device\\"):
        reject("UPDATE_PROBE_LEASE","Inherited managed lease has no native file identity")
    return Path("\\\\?\\GLOBALROOT"+name.value)


def _matching_descriptor(handle_value, expected):
    import msvcrt
    from .windows_storage import validate_acl
    descriptor=msvcrt.open_osfhandle(handle_value,os.O_RDONLY|os.O_BINARY)
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            reject("UPDATE_PROBE_LEASE","Managed lease is not an ordinary lock object")
        handle=msvcrt.get_osfhandle(descriptor)
        validate_acl(handle)  # private control policy, never program RX policy
        if str(_native_name(handle)).casefold()!=str(expected).casefold():
            reject("UPDATE_PROBE_LEASE","Inherited lease belongs to another installation")
        os.set_inheritable(descriptor,False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def inherited_sandbox_runtime(role):
    if role not in {"tool-runner","python"} or not in_appcontainer():
        yield False
        return
    from .windows_storage import api, _check, physical_executable, HANDLE
    kernel=C.WinDLL("kernel32",use_last_error=True)
    query=kernel.IsProcessInJob
    query.argtypes=[HANDLE,HANDLE,C.POINTER(C.c_int)];query.restype=C.c_int
    member=C.c_int()
    _check(query(api()["GetCurrentProcess"](),None,C.byref(member)))
    if not member.value:reject("UPDATE_PROBE_LEASE","Restricted managed payload requires an owned Job")
    image=physical_executable(Path(sys.executable))
    if (image.parent.name!="gitgo-host" or image.parents[1].name!="internal"
            or image.parents[2].name!="active"):
        reject("UPDATE_PROBE_LEASE","Restricted managed image layout is invalid")
    expected=image.parents[3]/"control"/"runtime.lock"
    raw=os.environ.get(LEASE_HANDLES_ENV,"")
    values=raw.split(",")
    if (not raw or len(raw)>336 or not 1<=len(values)<=16
            or any(not v.isascii() or not v.isdigit() or len(v)>20 or int(v)<=0 for v in values)
            or len(set(values))!=len(values)):
        reject("UPDATE_PROBE_LEASE","Restricted managed payload lacks an inherited lease")
    from backend.core.installation_lease import retain_descriptor
    with ExitStack() as scope:
        for value in values:
            descriptor=_matching_descriptor(int(value),expected)
            scope.callback(os.close,descriptor)
            scope.enter_context(retain_descriptor(descriptor))
        yield True
