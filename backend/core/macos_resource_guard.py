"""Fail-closed resource observer, not a kernel aggregate hard cap.

The launchd execution service requires its isolated coalition CPU ledger.
Standalone diagnostics retain the older sampled CPU lower bound. Memory uses
sampled live totals plus the largest observed per-process lifetime footprint.
Exited, unobserved processes and enforcement timing remain gaps; no hard cap.
"""
from __future__ import annotations
import ctypes
import errno
import time
from functools import lru_cache
API_VERSION=4


@lru_cache(maxsize=1)
def timebase():
    class Info(ctypes.Structure):
        _fields_=[('numer',ctypes.c_uint32),('denom',ctypes.c_uint32)]
    value=Info()
    call=ctypes.CDLL('/usr/lib/libSystem.B.dylib').mach_timebase_info
    call.argtypes=[ctypes.POINTER(Info)];call.restype=ctypes.c_int
    if call(ctypes.byref(value)) or not value.numer or not value.denom:
        raise RuntimeError('Mach CPU timebase unavailable')
    return value.numer,value.denom


def validate(value):
    bounds={'memory_bytes':(16*1024**2,8*1024**3),'process_limit':(4,256),'cpu_seconds':(1,1800)}
    if type(value) is not dict or set(value)!=set(bounds):raise ValueError('Missing resource policy')
    for key,(low,high) in bounds.items():
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('Invalid resource policy: '+key)
    return dict(value)


class BSD(ctypes.Structure):
    _fields_=[(n,ctypes.c_uint32) for n in ('flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','reserved')]+[
        ('comm',ctypes.c_char*16),('name',ctypes.c_char*32)]+[(n,ctypes.c_uint32) for n in ('nfiles','pgid','jobc','tdev','tpgid','nice')]+[
        ('start_sec',ctypes.c_uint64),('start_usec',ctypes.c_uint64)]


class Usage(ctypes.Structure):
    # Darwin SDK sys/resource.h rusage_info_v4, including the entire tail:
    # proc_pid_rusage writes a fixed-size structure for the requested flavor.
    _fields_=[('uuid',ctypes.c_ubyte*16)]+[(n,ctypes.c_uint64) for n in (
        'user','system','wakeups','interrupts','pageins','wired','resident','footprint','start','exit',
        'child_user','child_system','child_wakeups','child_interrupts','child_pageins','child_elapsed','read','written',
        'qos_default','qos_maintenance','qos_background','qos_utility','qos_legacy','qos_initiated','qos_interactive',
        'billed_system','serviced_system','logical_writes','lifetime_peak','instructions','cycles',
        'billed_energy','serviced_energy','interval_peak','runnable')]


def observe(uid):
    if __package__:
        from backend.core.macos_identity import owned_rows
        from backend.core.macos_coalition import coalition_id
    else:
        from macos_identity import owned_rows
        from macos_coalition import coalition_id
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    info=lib.proc_pidinfo;info.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int];info.restype=ctypes.c_int
    usage=lib.proc_pid_rusage;usage.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_void_p];usage.restype=ctypes.c_int
    samples=[]
    def bsd(pid):
        record=BSD();size=info(pid,3,0,ctypes.byref(record),ctypes.sizeof(record))
        if size!=ctypes.sizeof(record):raise OSError(ctypes.get_errno() or errno.EIO,'Process identity observation failed')
        return record
    for row in owned_rows(uid):
        pid=row['pid']
        try:
            before=bsd(pid)
            if uid not in (before.uid,before.ruid,before.svuid):continue
            r=Usage()
            # No v2 fallback: missing peak accounting must fail closed.
            if usage(pid,4,ctypes.byref(r)):raise OSError(ctypes.get_errno() or errno.EIO,'Resource observation failed')
            cid=coalition_id(pid)
            after=bsd(pid)
            if (before.pid,before.start_sec,before.start_usec)!=(after.pid,after.start_sec,after.start_usec):
                raise RuntimeError('PID changed during resource observation')
            if uid not in (after.uid,after.ruid,after.svuid):raise RuntimeError('UID changed during observation')
            # rusage_info CPU counters use Mach absolute ticks. On this ARM
            # host the ratio is 125/3, unlike the 1/1 Intel timebase.
            numer,denom=timebase()
            samples.append(dict(key=(pid,r.start),cpu_ns=(r.user+r.system)*numer//denom,
                child_cpu_ns=(r.child_user+r.child_system)*numer//denom,memory=max(r.resident,r.footprint),
                lifetime_peak_memory=r.lifetime_peak,coalition_id=cid))
        except OSError as exc:
            if exc.errno in (errno.ESRCH,errno.ENOENT):continue
            raise
    return samples


class Guard:
    def __init__(self,uid,policy,*,reader=observe,cpu_ledger=None):
        self.uid=uid;self.policy=validate(policy);self.reader=reader
        self.cpu_ledger=cpu_ledger
        self.seen={};self.child_seen={};self.self_cpu_ns=0;self.subtree_cpu_ns=0;self.cpu_ns=0
        self.peak_memory=0;self.peak_processes=0;self.samples=0
        self.peak_single_process_memory=0;self.memory_limit_basis=None
        self.last=None

    def check(self):
        now=time.monotonic()
        if self.last is not None and now-self.last>1.5:raise RuntimeError('Resource observer stalled')
        rows=self.reader(self.uid)
        self.samples+=1
        memory=0;keys=set()
        for row in rows:
            key=tuple(row['key']);cpu=row['cpu_ns'];previous=self.seen.get(key,0)
            child=row['child_cpu_ns'];prior_child=self.child_seen.get(key,0)
            peak=row['lifetime_peak_memory']
            if key in keys:raise RuntimeError('Duplicate process resource observation')
            keys.add(key)
            if any(type(v) is not int or v<0 for v in (cpu,child,row['memory'],peak)):
                raise RuntimeError('Invalid resource counter')
            if cpu<previous or child<prior_child:raise RuntimeError('Resource counter regressed')
            self.self_cpu_ns+=cpu-previous;self.seen[key]=cpu;self.child_seen[key]=child;memory+=row['memory']
            # XNU propagates reaped descendants into each parent's child
            # counters. Summing these with sampled child self time would count
            # the same work twice (and again when the parent is reaped).
            # Both values below are independent lower bounds: take their max,
            # never add overlapping subtree counters or infer missing ancestry.
            self.subtree_cpu_ns=max(self.subtree_cpu_ns,cpu+child)
            # Peaks from different processes may occur at different times.
            # Never sum them, or add a historical peak to today's live total.
            # Keep already observed peaks even after exit / PID reuse / exec.
            self.peak_single_process_memory=max(self.peak_single_process_memory,peak)
        self.cpu_ns=max(self.self_cpu_ns,self.subtree_cpu_ns)
        if self.cpu_ledger is not None:self.cpu_ns=self.cpu_ledger.read(rows)
        self.last=time.monotonic()
        if self.last-now>1.5:raise RuntimeError('Resource observation exceeded deadline')
        if len(self.seen)>8192:raise RuntimeError('Resource accounting capacity exceeded')
        self.peak_memory=max(self.peak_memory,memory);self.peak_processes=max(self.peak_processes,len(rows))
        if self.peak_memory>self.policy['memory_bytes']:
            self.memory_limit_basis='sampled_live_total';return 'resource_memory'
        if self.peak_single_process_memory>self.policy['memory_bytes']:
            self.memory_limit_basis='single_process_lifetime_peak';return 'resource_memory'
        if len(rows)>self.policy['process_limit']:return 'resource_processes'
        if self.cpu_ns>=self.policy['cpu_seconds']*1_000_000_000:return 'resource_cpu'
        return None

    def evidence(self):
        return dict(mode='coalition_cpu_sampled_uid_memory' if self.cpu_ledger else 'sampled_uid',strict_aggregate=False,samples=self.samples,
            cpu_accounting='launchd_resource_coalition' if self.cpu_ledger else 'max_self_sum_and_reaped_subtree',
            coalition=self.cpu_ledger.evidence() if self.cpu_ledger else None,
            observed_cpu_ns=self.cpu_ns,observed_self_cpu_ns=self.self_cpu_ns,
            reaped_subtree_cpu_lower_bound_ns=self.subtree_cpu_ns,peak_observed_memory=self.peak_memory,
            memory_accounting='sampled_live_total_and_single_process_lifetime_peak',
            peak_single_process_memory=self.peak_single_process_memory,
            memory_peak_lower_bound=max(self.peak_memory,self.peak_single_process_memory),
            memory_limit_basis=self.memory_limit_basis,
            unobserved_exited_memory_covered=False,
            peak_observed_processes=self.peak_processes,tracked_identities=len(self.seen))
