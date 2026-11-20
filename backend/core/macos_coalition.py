"""CPU ledger for a fresh, single-invocation launchd resource coalition.

Uses the versioned prefix of Darwin's private coalition ABI. Unsupported
systems fail closed. Never create/join privileged coalitions or use entitlements.
CPU includes this invocation's supervisor and its observation subprocesses;
memory remains sampled and CPU enforcement still has a polling delay.
"""
from __future__ import annotations
import ctypes
import errno
import os


def coalition_id(pid):
    # XNU proc_info_private.h: PROC_PIDCOALITIONINFO=20; two IDs + 3 reserves.
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    call=lib.proc_pidinfo
    call.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
    call.restype=ctypes.c_int
    value=(ctypes.c_uint64*5)()
    if call(pid,20,0,ctypes.byref(value),ctypes.sizeof(value))!=ctypes.sizeof(value):
        raise OSError(ctypes.get_errno() or errno.EIO,'Resource coalition identity unavailable')
    if not value[0]:raise RuntimeError('Invalid resource coalition identity')
    return value[0]


def usage(cid):
    # Stable prefix: tasks_started, tasks_exited, time_nonempty, cpu_time.
    # XNU copies MIN(request size, current struct size). CPU uses Mach ticks.
    lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    call=lib.coalition_info_resource_usage
    call.argtypes=[ctypes.c_uint64,ctypes.c_void_p,ctypes.c_size_t];call.restype=ctypes.c_int
    value=(ctypes.c_uint64*4)()
    if call(cid,ctypes.byref(value),ctypes.sizeof(value)):
        raise OSError(ctypes.get_errno() or errno.EIO,'Resource coalition ledger unavailable')
    return dict(started=value[0],exited=value[1],cpu_ticks=value[3])


class CoalitionCPU:
    def __init__(self,*,ids=coalition_id,reader=usage):
        if __package__:from backend.core.macos_resource_guard import timebase
        else:from macos_resource_guard import timebase
        self.ids=ids;self.reader=reader;self.pid=os.getpid();self.cid=ids(self.pid)
        # A direct launchd child, distinct from launchd's coalition, with only
        # this supervisor alive BEFORE accepting any workload. Historical
        # startup/exec helpers are excluded by taking a baseline here.
        if os.getppid()!=1 or self.cid==ids(1):raise RuntimeError('Expected an isolated launchd resource coalition')
        self.base=self._snapshot()
        if self.base['started']-self.base['exited']!=1:
            raise RuntimeError('Resource coalition already contains other live tasks')
        self.previous=self.base;self.cpu_ns=0;self.numer,self.denom=timebase()

    def _snapshot(self):
        value=self.reader(self.cid)
        if (set(value)!={'started','exited','cpu_ticks'}
            or any(type(v) is not int or v<0 for v in value.values())
            or value['started']<=value['exited']):
            raise RuntimeError('Invalid resource coalition counters')
        return value

    def read(self,rows):
        if os.getpid()!=self.pid or self.ids(self.pid)!=self.cid:
            raise RuntimeError('Supervisor resource coalition changed')
        if any(row.get('coalition_id')!=self.cid for row in rows):
            raise RuntimeError('Task escaped its resource coalition')
        current=self._snapshot()
        if any(current[k]<self.previous[k] for k in current):
            raise RuntimeError('Resource coalition counter regressed')
        self.previous=current
        self.cpu_ns=(current['cpu_ticks']-self.base['cpu_ticks'])*self.numer//self.denom
        return self.cpu_ns

    def evidence(self):
        return dict(id=self.cid,cpu_ns=self.cpu_ns,
            tasks_started=self.previous['started']-self.base['started'],
            tasks_exited=self.previous['exited']-self.base['exited'],
            includes_supervisor=True,includes_retired_tasks=True)
