"""Bounded, read-only operator stack sample of the current trial's sealed Node.

Select by root-owned lease UID, exact executable and process birth identity.
Never use a PID or path supplied by task output; discard evidence on PID change.
"""
import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import time

from backend.core import macos_identity as identity
from backend.core.macos_resource_guard import BSD
from backend.core.sandbox_macos import load_state


def snapshot(pid,uid,executable):
    lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    info=lib.proc_pidinfo
    info.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int];info.restype=ctypes.c_int
    path=lib.proc_pidpath;path.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32];path.restype=ctypes.c_int
    before=BSD();after=BSD();buffer=ctypes.create_string_buffer(4096)
    if info(pid,3,0,ctypes.byref(before),ctypes.sizeof(before))!=ctypes.sizeof(before):return None
    if path(pid,buffer,len(buffer))<=0 or os.fsdecode(buffer.value)!=str(executable):return None
    if info(pid,3,0,ctypes.byref(after),ctypes.sizeof(after))!=ctypes.sizeof(after):return None
    key=lambda value:(value.pid,value.start_sec,value.start_usec)
    if key(before)!=key(after) or before.pid!=pid:return None
    if any(value!=uid for record in (before,after) for value in (record.uid,record.ruid,record.svuid)):return None
    return key(before)


def collect(lease,helper):
    if os.getuid()!=0 or os.geteuid()!=0:raise PermissionError('Operator sample requires root')
    state=load_state(lease);uid=state['uid']
    executable=Path(helper).parent/'developer-tools/bin/node'
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        for row in identity.owned_rows(uid):
            pid=row['pid'];key=snapshot(pid,uid,executable)
            if key is None:continue
            # Let fast --version calls finish. Only collect a still-live image.
            time.sleep(.15)
            if snapshot(pid,uid,executable)!=key:continue
            with tempfile.TemporaryDirectory(prefix='gitgo-node-sample-') as directory:
                output=Path(directory)/'sample.txt'
                run=subprocess.run(['/usr/bin/sample',str(pid),'1','10','-file',str(output)],
                    stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=4,
                    env={'PATH':'/usr/bin:/bin','HOME':'/','__CF_USER_TEXT_ENCODING':'0x0:0:0'})
                if snapshot(pid,uid,executable)!=key:
                    return dict(collected=False,reason='process identity changed; sample discarded')
                trace=''
                if output.exists():
                    with output.open('rb') as stream:trace=stream.read(60000).decode(errors='replace')
                return dict(collected=run.returncode==0,uid=uid,pid=pid,birth=key[1:],
                            executable=str(executable),returncode=run.returncode,
                            stderr=run.stderr[-2000:],trace=trace)
        time.sleep(.03)
    return dict(collected=False,reason='no persistent sealed Node process observed')
