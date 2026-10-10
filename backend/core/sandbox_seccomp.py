"""Host-generated Linux socket restrictions, inherited across exec and descendants.

Export BPF to bubblewrap; never install a filter into the trusted Host itself.
The network namespace handles IPv4/IPv6; other socket domains cannot reach Host
Unix sockets, VSOCK, packet or netlink endpoints. Local stream socketpair IPC
remains available for asyncio and child-process communication.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
import errno
from pathlib import Path
import socket
import sysconfig
import tempfile

from backend.core.sandbox import SandboxDenied
from backend.core.sandbox_linux import trusted_system_file

_ALLOW = 0x7FFF0000
_KILL_PROCESS = 0x80000000
_DENY = 0x00050000 | errno.EPERM
_NE, _LT, _EQ, _GT, _MASKED_EQ = 1, 2, 4, 6, 7


class _Comparison(C.Structure):
    _fields_ = [('argument', C.c_uint), ('operation', C.c_int),
                ('first', C.c_uint64), ('second', C.c_uint64)]


def _load_library(workspace):
    # Fixed system locations, never LD_LIBRARY_PATH, cwd, or a model override.
    multiarch = sysconfig.get_config_var('MULTIARCH') or ''
    if multiarch and (not isinstance(multiarch, str) or any(c in multiarch for c in '/\\')):
        raise OSError('Invalid system library architecture')
    directories = [Path('/usr/lib'), Path('/lib'), Path('/usr/lib64'), Path('/lib64')]
    if multiarch:
        directories = [Path('/usr/lib') / multiarch, Path('/lib') / multiarch, *directories]
    for directory in directories:
        path = trusted_system_file(directory / 'libseccomp.so.2', workspace)
        if path is not None:
            api = C.CDLL(str(path))
            signatures = {
                'seccomp_init': ([C.c_uint32], C.c_void_p),
                'seccomp_release': ([C.c_void_p], None),
                'seccomp_attr_set': ([C.c_void_p, C.c_uint, C.c_uint32], C.c_int),
                'seccomp_syscall_resolve_name': ([C.c_char_p], C.c_int),
                'seccomp_rule_add_array': ([C.c_void_p, C.c_uint32, C.c_int,
                                            C.c_uint, C.POINTER(_Comparison)], C.c_int),
                'seccomp_export_bpf': ([C.c_void_p, C.c_int], C.c_int),
            }
            for name, (arguments, result) in signatures.items():
                function = getattr(api, name)
                function.argtypes, function.restype = arguments, result
            return api
    raise OSError('A trusted system libseccomp.so.2 is required')


@contextmanager
def socket_filter(workspace: Path):
    """Supply one owned, anonymous BPF descriptor; all failures stop launch."""
    context = None
    api = None
    try:
        api = _load_library(workspace)
        context = api.seccomp_init(_ALLOW)
        if not context:
            raise OSError('Cannot allocate a seccomp filter')

        def check(status):
            if status != 0:
                raise OSError(f'Cannot construct the socket seccomp policy ({status})')

        check(api.seccomp_attr_set(context, 2, _KILL_PROCESS))  # ACT_BADARCH

        def deny(name, *comparisons):
            number = api.seccomp_syscall_resolve_name(name.encode('ascii'))
            if number == -1:  # __NR_SCMP_ERROR, not an ignored/missing syscall.
                raise OSError(f'libseccomp cannot resolve required syscall {name}')
            array = (_Comparison * len(comparisons))(*comparisons) if comparisons else None
            check(api.seccomp_rule_add_array(context, _DENY, number, len(comparisons), array))

        # Domain allowlist: AF_INET=2 and AF_INET6=10, inside the private netns.
        # Independent rules avoid comparing one argument twice in a rule.
        deny('socket', _Comparison(0, _LT, socket.AF_INET, 0))
        for domain in range(socket.AF_INET + 1, socket.AF_INET6):
            deny('socket', _Comparison(0, _EQ, domain, 0))
        deny('socket', _Comparison(0, _GT, socket.AF_INET6, 0))
        # AF_UNIX stream pairs have no pathname/abstract address to a Host
        # endpoint. Datagram pairs can reconnect; only stream pairs are in
        # the supported IPC policy, so all other kinds remain denied.
        deny('socketpair', _Comparison(0, _NE, socket.AF_UNIX, 0))
        for kind in range(16):  # Linux SOCK_TYPE_MASK; retain CLOEXEC/NONBLOCK.
            if kind != socket.SOCK_STREAM:
                deny('socketpair', _Comparison(1, _MASKED_EQ, 0xF, kind))
        # io_uring can create sockets without a socket syscall. It cannot be
        # available as an alternate path around the domain filter.
        for name in ('io_uring_setup', 'io_uring_enter', 'io_uring_register'):
            deny(name)
        with tempfile.TemporaryFile(prefix='gitgo-seccomp-') as program:
            check(api.seccomp_export_bpf(context, program.fileno()))
            if program.tell() <= 0:
                raise OSError('libseccomp exported an empty filter')
            program.seek(0)
            yield program
    except (OSError, AttributeError, ValueError, RuntimeError) as exc:
        if isinstance(exc, SandboxDenied):
            raise
        raise SandboxDenied('SANDBOX_UNAVAILABLE',
            f'Linux socket isolation is unavailable; install trusted system libseccomp '
            f'and a seccomp-capable bubblewrap/kernel. No unfiltered fallback: {exc}') from exc
    finally:
        if context:
            api.seccomp_release(context)
