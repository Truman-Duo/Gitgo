"""Local NTFS ownership, pinned handles, stable IDs and byte-range locks.

No shell ACL commands, privilege escalation, reparse-point adoption or lockfile
replacement. Imported on other platforms for inspection; APIs load only on NT.
Windows directory metadata has no POSIX fsync equivalent: replacements use
MoveFileExW WRITE_THROUGH, files are flushed, and recovery retains both trees.
This does not claim universal physical power-loss durability.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes as C
from functools import lru_cache
import os
from pathlib import Path
import re
import struct
import sys
import uuid

from .validation import reject

U32, HANDLE, PTR = C.c_uint32, C.c_void_p, C.c_void_p
TRUSTED = {"S-1-5-18", "S-1-5-32-544"}  # OS/admin authority, not ordinary groups.
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
MUTATE = 0x40000000 | 0x10000000 | 0x000D0156
REPLACE_CHILD = 0x40000000 | 0x10000000 | 0x000C0040
_NAMESPACES = ContextVar("gitgo_update_namespaces", default=())
_DIRECTORY_HANDLES = ContextVar("gitgo_update_directory_handles", default=())


def pinned_directory(path):
    return next((handle for node, handle in _DIRECTORY_HANDLES.get() if node == path), None)


def supported():
    return os.name == "nt" and sys.platform == "win32" and hasattr(C, "WinDLL")


def physical_executable(path):
    """Resolve the actual opened image without querying DOS drive mappings.

    AppContainer may read an image but cannot query the mount manager used by
    Path.resolve's DOS-name lookup. NT normalized names retain junction/link
    resolution; a lexical path or environment assertion is never substituted.
    This is identity discovery, not managed-installation ownership admission.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        reject("UPDATE_CONTROL_INVALID", "Runtime image must be absolute")
    handle = api()["CreateFileW"](str(path), 0x80, 7, None, 3, 0, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        name = C.create_unicode_buffer(32768)
        length = api()["GetFinalPathNameByHandleW"](handle, name, len(name), 2)
        if not 0 < length < len(name) or not name.value.startswith("\\Device\\"):
            reject("UPDATE_CONTROL_INVALID", "Physical runtime image name is unavailable")
        return Path("\\\\?\\GLOBALROOT" + name.value)
    finally:
        api()["CloseHandle"](handle)


@lru_cache(maxsize=1)
def api():
    if not supported(): reject("UPDATE_TRANSACTION_PLATFORM", "Actual Windows APIs are required")
    kernel, security = C.WinDLL("kernel32", use_last_error=True), C.WinDLL("advapi32", use_last_error=True)
    definitions = {
        "CreateFileW": (kernel, HANDLE, [C.c_wchar_p, U32, U32, PTR, U32, U32, HANDLE]),
        "CloseHandle": (kernel, C.c_int, [HANDLE]),
        "GetCurrentProcess": (kernel, HANDLE, []),
        "GetFileInformationByHandleEx": (kernel, C.c_int, [HANDLE, C.c_int, PTR, U32]),
        "GetFinalPathNameByHandleW": (kernel, U32, [HANDLE, C.c_wchar_p, U32, U32]),
        "GetVolumeInformationByHandleW": (kernel, C.c_int, [HANDLE, C.c_wchar_p, U32, PTR, PTR, PTR, C.c_wchar_p, U32]),
        "MoveFileExW": (kernel, C.c_int, [C.c_wchar_p, C.c_wchar_p, U32]),
        "CreateDirectoryW": (kernel, C.c_int, [C.c_wchar_p, PTR]),
        "FindFirstStreamW": (kernel, HANDLE, [C.c_wchar_p, C.c_int, PTR, U32]),
        "FindNextStreamW": (kernel, C.c_int, [HANDLE, PTR]),
        "FindClose": (kernel, C.c_int, [HANDLE]),
        "LockFileEx": (kernel, C.c_int, [HANDLE, U32, U32, U32, U32, PTR]),
        "UnlockFileEx": (kernel, C.c_int, [HANDLE, U32, U32, U32, PTR]),
        "LocalFree": (kernel, HANDLE, [HANDLE]),
        "OpenProcessToken": (security, C.c_int, [HANDLE, U32, PTR]),
        "GetTokenInformation": (security, C.c_int, [HANDLE, C.c_int, PTR, U32, PTR]),
        "ConvertSidToStringSidW": (security, C.c_int, [PTR, PTR]),
        "GetSecurityInfo": (security, U32, [HANDLE, C.c_int, U32, PTR, PTR, PTR, PTR, PTR]),
        "GetAce": (security, C.c_int, [PTR, U32, PTR]),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (security, C.c_int, [C.c_wchar_p, U32, PTR, PTR]),
    }
    result = {}
    for name, (dll, returns, arguments) in definitions.items():
        call = getattr(dll, name); call.restype, call.argtypes = returns, arguments
        result[name] = call
    return result


def _check(result):
    if not result: raise C.WinError(C.get_last_error())
    return result


def _sid(pointer):
    value = PTR()
    _check(api()["ConvertSidToStringSidW"](pointer, C.byref(value)))
    try: return C.wstring_at(value)
    finally: api()["LocalFree"](value)


def current_sid():
    token, size = HANDLE(), U32()
    _check(api()["OpenProcessToken"](api()["GetCurrentProcess"](), 8, C.byref(token)))
    try:
        api()["GetTokenInformation"](token, 1, None, 0, C.byref(size))
        if not 8 <= size.value <= 65536: reject("UPDATE_ACL_INVALID", "Token identity is unavailable")
        data = C.create_string_buffer(size.value)
        _check(api()["GetTokenInformation"](token, 1, data, size, C.byref(size)))
        return _sid(PTR.from_buffer(data).value)
    finally: api()["CloseHandle"](token)


def validate_acl(handle, *, private=True, program=False):
    owner, acl, descriptor = PTR(), PTR(), PTR()
    result = api()["GetSecurityInfo"](handle, 1, 5, C.byref(owner), None, C.byref(acl), None, C.byref(descriptor))
    if result: reject("UPDATE_ACL_INVALID", "Cannot inspect installation access control")
    try:
        # TrustedInstaller can own Windows' volume/system ancestors. It is
        # not accepted as the private application's installation owner.
        allowed = TRUSTED | {current_sid()} | ({TRUSTED_INSTALLER} if not private else set())
        owner_sid = _sid(owner) if owner.value else None
        # An owner may rewrite the DACL even without an explicit write ACE.
        if not owner_sid or not acl.value or owner_sid not in allowed:
            reject("UPDATE_ACL_INVALID", "Missing or foreign installation ownership")
        header = C.string_at(acl, 8)
        _, _, total, count, _ = struct.unpack("<BBHHH", header)
        if total < 8 or total > 65535 or count > 256:
            reject("UPDATE_ACL_INVALID", "Access control is outside its bounds")
        for index in range(count):
            ace = PTR(); _check(api()["GetAce"](acl, index, C.byref(ace)))
            kind, flags, size = struct.unpack("<BBH", C.string_at(ace, 4))
            if size < 8 or size > total: reject("UPDATE_ACL_INVALID", "Invalid access entry")
            # Inherit-only entries do not authorize access to this object.
            if flags & 8: continue
            if kind == 1: continue  # A deny ACE cannot grant foreign authority.
            if kind != 0: reject("UPDATE_ACL_INVALID", "Complex access entries are unsupported")
            mask = U32.from_address(ace.value + 4).value
            sid = _sid(ace.value + 8)
            # Python 3.13+ mkdir(mode=0700) uses OWNER RIGHTS, not a foreign
            # group. Resolve that authority ONLY through the actual queried
            # owner SID. A foreign-owned ancestor must not gain this exception.
            if sid == "S-1-3-4" and owner_sid in allowed: continue
            # Signed program payloads may be read/executed by the sandbox.
            # They remain owner-controlled: reject every mutation right, while
            # private controls/credentials still reject any foreign access.
            forbidden = MUTATE if program else REPLACE_CHILD
            if sid not in allowed and ((private and not program) or mask & forbidden):
                # Only well-known public group IDs, never account/path names.
                group = sid if sid in {"S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-3-0", "S-1-3-4"} else "other"
                reject("UPDATE_ACL_INVALID", "Foreign access permits installation replacement "
                       f"(private={private}, group={group}, rights={mask:#x})")
    finally:
        api()["LocalFree"](descriptor)


@contextmanager
def opened(path, *, write=False, private=True, program=False, share=3):
    if not isinstance(path, Path) or not path.is_absolute():
        reject("UPDATE_CONTROL_INVALID", "Windows path must be explicit and absolute")
    retained = pinned_directory(path) if not write and share == 3 else None
    if retained is not None:
        validate_acl(retained, private=private, program=program)
        yield retained
        return
    # OPEN_REPARSE_POINT opens the node itself; validate attributes before use.
    access = 0x80020000 | (0x40000000 if write else 0)
    handle = api()["CreateFileW"](str(path), access, share, None, 3, 0x02200000, None)
    if handle == C.c_void_p(-1).value: raise C.WinError(C.get_last_error())
    try:
        tags = C.create_string_buffer(8)
        _check(api()["GetFileInformationByHandleEx"](handle, 9, tags, 8))
        attributes, _ = struct.unpack("<II", tags.raw)
        if attributes & (0x400 | 0x40): reject("UPDATE_REPARSE_INVALID", "Redirected or device node is unsupported")
        filesystem = C.create_unicode_buffer(32)
        _check(api()["GetVolumeInformationByHandleW"](handle, None, 0, None, None, None, filesystem, 32))
        if filesystem.value != "NTFS": reject("UPDATE_FILESYSTEM_UNSUPPORTED", "Managed Windows updates require local NTFS")
        validate_acl(handle, private=private, program=program)
        yield handle
    finally: api()["CloseHandle"](handle)


def handle_identity(handle):
    data = C.create_string_buffer(24)
    _check(api()["GetFileInformationByHandleEx"](handle, 18, data, 24))  # FILE_ID_INFO
    name = C.create_unicode_buffer(32768)
    length = api()["GetFinalPathNameByHandleW"](handle, name, len(name), 1)
    if not 0 < length < len(name): reject("UPDATE_VOLUME_ID_UNAVAILABLE", "Volume GUID is unavailable")
    match = re.match(r"^\\\\\?\\Volume\{([0-9a-fA-F-]{36})\}\\", name.value)
    if not match: reject("UPDATE_VOLUME_ID_UNAVAILABLE", "Network or unknown volume is unsupported")
    value = uuid.UUID(match[1])
    if not value.int: reject("UPDATE_VOLUME_ID_UNAVAILABLE", "Volume GUID must be nonzero")
    inode = int.from_bytes(data.raw[8:24], "little")
    if not inode: reject("UPDATE_CONTROL_INVALID", "File identity must be nonzero")
    return {"device": "windows-volume:" + str(value), "inode": inode}


def filesystem_identity(path):
    with opened(path) as handle: return handle_identity(handle)


def descriptor_identity(fd):
    import msvcrt
    handle = msvcrt.get_osfhandle(fd)
    validate_acl(handle)
    return handle_identity(handle)


@contextmanager
def _security_attributes():
    class SecurityAttributes(C.Structure):
        _fields_ = [("length", U32), ("descriptor", PTR), ("inherit", C.c_int)]
    sd = PTR()
    sddl = "O:" + current_sid() + "D:P(A;OICI;FA;;;" + current_sid() + ")(A;OICI;FA;;;SY)"
    _check(api()["ConvertStringSecurityDescriptorToSecurityDescriptorW"](sddl, 1, C.byref(sd), None))
    try: yield SecurityAttributes(C.sizeof(SecurityAttributes), sd, False)
    finally: api()["LocalFree"](sd)


def create_private_directory(path):
    with _security_attributes() as attributes:
        _check(api()["CreateDirectoryW"](str(path), C.byref(attributes)))
    with opened(path): pass


def open_file(path, *, write=False, create=False, share=3, program=False):
    """Return a CRT FD with ownership transfer, no delete sharing or redirects."""
    import msvcrt
    if not create:
        with opened(path, write=write, share=share, program=program) as source:
            # Duplicate the SAME handle, not an independently resolved path.
            duplicate = HANDLE()
            kernel = C.WinDLL("kernel32", use_last_error=True)
            kernel.DuplicateHandle.argtypes = [HANDLE, HANDLE, HANDLE, PTR, U32, C.c_int, U32]
            kernel.DuplicateHandle.restype = C.c_int
            _check(kernel.DuplicateHandle(api()["GetCurrentProcess"](), source, api()["GetCurrentProcess"](),
                                         C.byref(duplicate), 0, False, 2))
            handle = duplicate.value
    else:
        with _security_attributes() as attributes:
            handle = api()["CreateFileW"](str(path), 0xC0020000, 3, C.byref(attributes), 1, 0x80, None)
            if handle == C.c_void_p(-1).value: raise C.WinError(C.get_last_error())
    try: return msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if write or create else os.O_RDONLY))
    except BaseException:
        api()["CloseHandle"](handle); raise


def validate_staging(path):
    # The operation may hold a real no-delete handle chain. In that case a
    # foreign-writable ancestor cannot replace the private boundary while we
    # use its path. All descendants still require private owner-controlled ACLs.
    pinned = next((root for root in _NAMESPACES.get() if path == root or path.is_relative_to(root)), None)
    if pinned is not None:
        current = path
        while True:
            with opened(current): pass
            if current == pinned: return
            current = current.parent
    for parent in (path, *path.parents):
        with opened(parent, private=parent == path): pass


@contextmanager
def _retain_namespace_handle(handle):
    """Let existing process guardians retain this exact sharing reservation."""
    import msvcrt
    kernel = C.WinDLL("kernel32", use_last_error=True)
    duplicate = kernel.DuplicateHandle
    duplicate.argtypes = [HANDLE, HANDLE, HANDLE, PTR, U32, C.c_int, U32]
    duplicate.restype = C.c_int
    copied = HANDLE()
    _check(duplicate(api()["GetCurrentProcess"](), handle, api()["GetCurrentProcess"](), C.byref(copied), 0, False, 2))
    try:
        descriptor = msvcrt.open_osfhandle(copied.value, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api()["CloseHandle"](copied.value)
        raise
    try:
        from backend.core.installation_lease import retain_descriptor
        with retain_descriptor(descriptor, namespace=True): yield
    finally: os.close(descriptor)


@contextmanager
def private_namespace(path):
    """Pin every lexical ancestor and the private root until the operation ends.

    No ACL is changed or foreign principal trusted. Ancestor DACLs are not a
    grant to mutate the private root; no-delete sharing blocks namespace swaps.
    The root itself MUST retain the strict private ACL and local NTFS identity.
    """
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        reject("UPDATE_CONTROL_INVALID", "Private namespace must be canonical")
    if any(path == root or path.is_relative_to(root) for root in _NAMESPACES.get()):
        validate_staging(path)
        yield
        return
    held = []
    nodes = []
    try:
        for node in reversed((path, *path.parents)):
            handle = api()["CreateFileW"](str(node), 0x80020000, 3, None, 3, 0x02200000, None)
            if handle == C.c_void_p(-1).value: raise C.WinError(C.get_last_error())
            held.append(handle)
            nodes.append((node, handle))
            tags = C.create_string_buffer(8)
            _check(api()["GetFileInformationByHandleEx"](handle, 9, tags, 8))
            attributes, _ = struct.unpack("<II", tags.raw)
            if not attributes & 0x10 or attributes & (0x400 | 0x40):
                reject("UPDATE_REPARSE_INVALID", "Private namespace path is redirected or not a directory")
            filesystem = C.create_unicode_buffer(32)
            _check(api()["GetVolumeInformationByHandleW"](handle, None, 0, None, None, None, filesystem, 32))
            if filesystem.value != "NTFS":
                reject("UPDATE_FILESYSTEM_UNSUPPORTED", "Private update namespace requires local NTFS")
            if node == path: validate_acl(handle)
        token = _NAMESPACES.set((*_NAMESPACES.get(), path))
        handles_token = _DIRECTORY_HANDLES.set((*_DIRECTORY_HANDLES.get(), *nodes))
        try:
            # Retain the leaf sharing reservation through the existing process
            # guardian. NTFS also refuses ancestor rename while this private
            # descendant directory has a no-delete handle (native regression).
            with _retain_namespace_handle(held[-1]): yield
        finally:
            _DIRECTORY_HANDLES.reset(handles_token)
            _NAMESPACES.reset(token)
    finally:
        for handle in reversed(held): api()["CloseHandle"](handle)


def reject_alternate_streams(path):
    class StreamData(C.Structure):
        _fields_ = [("size", C.c_int64), ("name", C.c_wchar * 296)]
    data = StreamData()
    handle = api()["FindFirstStreamW"](str(path), 0, C.byref(data), 0)
    if handle == C.c_void_p(-1).value:
        if C.get_last_error() == 38: return
        reject("UPDATE_TREE_INVALID", "NTFS stream enumeration is unavailable")
    try:
        count = 0
        while True:
            count += 1
            if count > 1 or data.name != "::$DATA":
                reject("UPDATE_TREE_INVALID", "Alternate NTFS streams are unsupported")
            if not api()["FindNextStreamW"](handle, C.byref(data)):
                if C.get_last_error() != 38:
                    reject("UPDATE_TREE_INVALID", "NTFS stream enumeration failed")
                break
    finally: api()["FindClose"](handle)


def move(source, target, *, replace=False):
    _check(api()["MoveFileExW"](str(source), str(target), 8 | (1 if replace else 0)))


def durable_copy(source, target):
    """New private file only: flush contents, never repair a retained file ACL.

    READONLY/mtime are not portable update authority. Normalize attributes only
    on our newly created copy, without chmod or permission changes to source.
    """
    source, target = Path(source), Path(target)
    source_fd = open_file(source, share=1)
    with os.fdopen(source_fd, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        import stat
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 2 * 1024**3:
            reject("UPDATE_TREE_INVALID", "Copy requires a bounded ordinary private file")
        fd = open_file(target, create=True, write=True)
        with os.fdopen(fd, "wb") as outgoing:
            total = 0
            while chunk := incoming.read(64 * 1024):
                total += len(chunk)
                if total > before.st_size: reject("UPDATE_TREE_INVALID", "Source grew during private copy")
                outgoing.write(chunk)
            if total != before.st_size: reject("UPDATE_TREE_INVALID", "Source changed during private copy")
            outgoing.flush(); os.fsync(outgoing.fileno())
    return str(target)


def copy_tree(source, target):
    import shutil
    return shutil.copytree(source, target, symlinks=False, copy_function=durable_copy)


def rename_owned_directory(source, target, expected, *, callback=None):
    """Rename the SAME validated directory object; no path check/use gap.

    Used only by the explicit bootstrap repair after all application leases
    retire. The DELETE-capable handle denies other delete sharing while the
    rename is made through that handle. No replacement or volume crossing.
    """
    source, target = Path(source), Path(target)
    if target.exists() or target.is_symlink():
        reject("UPDATE_REPAIR_REQUIRED", "Directory rename target is occupied")
    handle = api()["CreateFileW"](str(source), 0x80030000, 3, None, 3, 0x02200000, None)
    if handle == C.c_void_p(-1).value: raise C.WinError(C.get_last_error())
    try:
        validate_acl(handle)
        tags = C.create_string_buffer(8)
        _check(api()["GetFileInformationByHandleEx"](handle, 9, tags, 8))
        attributes, _ = struct.unpack("<II", tags.raw)
        if not attributes & 0x10 or attributes & (0x400 | 0x40) or handle_identity(handle) != expected:
            reject("UPDATE_REPAIR_REQUIRED", "Directory rename source identity changed")
        from contextlib import nullcontext
        retained_parent = pinned_directory(target.parent)
        with (nullcontext(retained_parent) if retained_parent is not None else opened(target.parent)) as parent:
            if handle_identity(parent)["device"] != expected["device"]:
                reject("UPDATE_REPAIR_REQUIRED", "Directory rename cannot cross volumes")
            class RenameInformation(C.Structure):
                _fields_ = [("replace", U32), ("root", HANDLE), ("length", U32), ("name", C.c_wchar * 1)]
            encoded = str(target).encode("utf-16-le")
            raw = C.create_string_buffer(max(C.sizeof(RenameInformation), RenameInformation.name.offset + len(encoded) + 2))
            header = RenameInformation.from_buffer(raw)
            header.replace = 0; header.root = None; header.length = len(encoded)
            C.memmove(C.addressof(raw) + RenameInformation.name.offset, encoded, len(encoded))
            setter = C.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
            setter.argtypes = [HANDLE, C.c_int, PTR, U32]; setter.restype = C.c_int
            _check(setter(handle, 3, raw, len(raw)))
        with opened(target, share=7) as renamed:
            if handle_identity(renamed) != expected:
                reject("UPDATE_REPAIR_REQUIRED", "Renamed directory identity changed")
        if callback is not None:
            token = _NAMESPACES.set((*_NAMESPACES.get(), target))
            handles_token = _DIRECTORY_HANDLES.set((*_DIRECTORY_HANDLES.get(), (target, handle)))
            try:
                with _retain_namespace_handle(handle): return callback()
            finally:
                _DIRECTORY_HANDLES.reset(handles_token)
                _NAMESPACES.reset(token)
    finally: api()["CloseHandle"](handle)


@contextmanager
def lock_file(path, *, shared=False, expected_identity=None):
    import msvcrt
    class Overlapped(C.Structure):
        _fields_ = [("internal", C.c_size_t), ("internal_high", C.c_size_t),
                    ("offset", U32), ("offset_high", U32), ("event", HANDLE)]
    try:
        # Shared READ handles deny later WRITE opens; exclusive READ/WRITE
        # handles deny later shared-lease opens. These reservations survive
        # DuplicateHandle/inheritance, unlike process-owned byte-range locks.
        fd = open_file(path, write=not shared, share=1)
    except OSError as error:
        if getattr(error, "winerror", None) == 32:
            reject("UPDATE_BUSY", "Installation has an active runtime or operation")
        raise
    overlap = Overlapped()
    handle = msvcrt.get_osfhandle(fd)
    locked = False
    try:
        if expected_identity is not None and descriptor_identity(fd) != expected_identity:
            reject("UPDATE_LOCK_INVALID", "Registered Windows lock was replaced")
        if not api()["LockFileEx"](handle, 1 | (0 if shared else 2), 0, 1, 0, C.byref(overlap)):
            if C.get_last_error() in (33, 158): reject("UPDATE_BUSY", "Installation has an active runtime or operation")
            reject("UPDATE_LOCK_INVALID", "Windows runtime lock is unavailable")
        locked = True
        yield fd
    finally:
        if locked: _check(api()["UnlockFileEx"](handle, 0, 1, 0, C.byref(overlap)))
        os.close(fd)
