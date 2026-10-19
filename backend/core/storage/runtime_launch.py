"""One persisted installation plan drives one owned native worker lifetime."""
from dataclasses import dataclass
from pathlib import PureWindowsPath
import time
import uuid

from .host_records import require
from .models import StorageCommitUnknown
from .native_windows_host import WindowsHostOwner
from .native_windows_process import WindowsProcessLibrary, WindowsStorageProcess, StorageWorkerCommand
from .runtime_control import RuntimeControl,RuntimeReservation,RuntimeCleanupReservation,require_whole_tail
from .runtime_installation import PinnedRuntime
from .runtime_manifest import RuntimeManifest, canonical
from .runtime_worker import RuntimeInstallRequest,RuntimeCleanupRequest


@dataclass(frozen=True)
class RuntimeInstallResult:
    generation: str
    phase: str
    manifest: RuntimeManifest | None


class RuntimeInstallation:
    _mode="install"
    _ticket_type=RuntimeReservation
    _request_type=RuntimeInstallRequest
    def __init__(self, control, ticket):
        require(type(control) is RuntimeControl,"owned runtime admission required")
        control.require_ticket(ticket)
        require(type(ticket) is self._ticket_type,"runtime operation ticket changed")
        require(getattr(control,"_launch",None) is None,"one native launch per runtime reservation")
        self._control,self._ticket,self._host=control,ticket,control._host
        self._anchor,self._registry=control._anchor,control._host._registry
        self._source=self._worker=self._identity=self._event=self._observer=None
        self._started=self._create_attempted=self._native_retired=self._parent_closed=False
        self._terminal_attempted=False
        self._before=self._after=None
        self._result=self._pending_result=None
        self._deadline=None
        control._launch=self

    def start(self, source, processes):
        require(not self._started and type(processes) is WindowsProcessLibrary,"one qualified installation launch required")
        self._control.require_ticket(self._ticket)
        self._started=True
        self._deadline=time.monotonic()+60
        host=self._host;plan=self._ticket.plan
        try:
            self._source=host.take_input(source)
        finally:
            host._ready=False
        self._worker=host._new(WindowsStorageProcess,processes)
        context=self._worker.current()
        prior=self._control._read();value=prior.head.state.value();entry=value["entries"][plan.slot]
        if self._mode=="install":
            require(entry==plan.initial_entry(),"runtime reservation is no longer fresh")
        else:
            require(canonical(entry)==self._ticket.record,"runtime cleanup reservation changed")
        require_whole_tail(prior,additional=0,
                           cleanup_slot=plan.slot if self._mode=="cleanup" else None)
        self._control._idle(value,except_slot=plan.slot)
        self._control._namespace(value)
        self._event=dict(launch=str(uuid.uuid4()),mode=self._mode,phase="INTENT",pid=0,created=0,
                         parent_pid=context.identity.pid,parent_created=context.identity.creation_time)
        entry["worker"]=self._event
        self._control._update(prior,value)
        request=self._request_type(self._anchor.binding,plan.slot,plan.generation,plan.source_manifest,
                                      plan.sha256,self._event["launch"])
        root=PureWindowsPath(self._source.manifest.root)
        command=StorageWorkerCommand((str(root/"python.exe"),"-I","-S","-B","-m",
            "backend.core.storage.runtime_worker",request.to_bytes().decode("ascii")),str(root),
            (("PATH",str(root)),("SYSTEMROOT",context.system_root),("WINDIR",context.system_root)))
        self._create_attempted=True
        self._worker.create(command)
        self._identity,resumed=self._worker.identity()
        require(not resumed,"runtime installer unexpectedly resumed before durable identity")
        prior=self._control._read();value=prior.head.state.value();entry=value["entries"][plan.slot]
        plan.require_entry(entry)
        require(entry["worker"]==self._event,"runtime installer intent changed")
        self._event=dict(self._event,phase="CREATED",pid=self._identity.pid,created=self._identity.creation_time)
        entry["worker"]=self._event
        self._control._update(prior,value)
        host._ready=True
        host.handoff_gate()
        self._worker.resume(self._identity)
        # Release carries no business authority. Sending it before retirement
        # polling also avoids needing to unseal a partially retired owner.
        self._worker.release()

    def _retire_original(self, *, cancel):
        require(self._started,"runtime installation has not started")
        require(type(cancel) is bool,"explicit runtime cancellation required")
        if not self._native_retired:
            terminate=cancel or time.monotonic()>=self._deadline
            if self._worker is not None and not self._worker.retire(terminate=terminate):
                return False
            self._native_retired=True
        if not self._parent_closed:
            if not self._host.close():
                return False
            self._parent_closed=True
        return True

    def _new_observer(self):
        # Retain the wrapper before its native scope constructor can have an
        # effect, just as the original Host owns its native children.
        self._observer=object.__new__(WindowsHostOwner)
        WindowsHostOwner.__init__(self._observer,self._registry)
        self._observer.attach()
        return self._observer

    def _close_observer(self):
        if self._observer is not None:
            if id(self._observer) in self._registry._owners and not self._observer.close():
                return False
            self._observer=None
        return True

    def _complete(self, observer):
        """Internal reconciliation; production obtains the canonical observer."""
        require(self._native_retired and self._parent_closed and observer.anchor==self._anchor,
                "original native and Host owners must close before installation completion")
        plan=self._ticket.plan
        control=RuntimeControl(observer)
        control._used=True
        observer._ready=False
        control._lease(exclusive=True)
        prior=control._read();value=prior.head.state.value();entry=value["entries"][plan.slot]
        plan.require_entry(entry)
        control._namespace(value)
        if self._terminal_attempted:
            if entry==self._after:
                # A read alone cannot repair a lost flush acknowledgement.
                # Confirm the existing exact record without replaying execution.
                observer._scope.flush_bound(observer._root,"slot-0063.json",
                    dict(self._anchor.members)["slot-0063.json"],8192)
                require(control._read().head.state.value()["entries"][plan.slot]==self._after,
                        "runtime completion record changed during durability confirmation")
                return self._pending_result
            if entry!=self._before:
                raise StorageCommitUnknown("runtime_installation_completion_changed")
            # Exact prior bytes after every previous writer has closed allow a
            # fresh evidence commit. The native worker is never launched again.
        if self._mode=="cleanup" and not self._create_attempted and canonical(entry)==self._ticket.record:
            # Cancellation before any new intent leaves the old retirement
            # debt unchanged. It does not invent a cleanup/absence receipt.
            return RuntimeInstallResult(plan.generation,"RETIRING",None)
        worker=entry["worker"]
        if worker is None:
            require(not self._create_attempted and entry["phase"]=="RESERVED",
                    "runtime launch evidence disappeared")
        else:
            require(self._event is not None and all(worker[key]==self._event[key]
                    for key in ("launch","mode","parent_pid","parent_created")),"runtime installer owner changed")
            if worker["pid"]:
                require(self._identity is not None and (worker["pid"],worker["created"])
                        ==(self._identity.pid,self._identity.creation_time),"runtime installer process identity changed")
            require(worker["phase"]!="RETIRED","runtime installer was retired by another coordinator")
        self._before=dict(entry)
        target=None
        if self._mode=="cleanup":
            from .runtime_lifecycle import observe_absence
            require(entry["phase"]=="RETIRING","unexpected runtime cleanup phase")
            if observe_absence(control,plan.generation):entry["phase"]="ABSENT"
        elif entry["phase"]=="MATERIALIZED":
            from backend.core.sandbox_read import ObjectIdentity
            target=plan.target_manifest(self._anchor,ObjectIdentity(**entry["root"]))
            require(target.sha256==entry["manifest"],"runtime materialization record changed")
            verification=observer._new(PinnedRuntime,self._registry.trees,target.to_bytes(),target.sha256,self._anchor.account.sid)
            verification.pin()
            entry["phase"]="READY"
        else:
            require(entry["phase"] in {"RESERVED","BUILDING"},"unexpected runtime installation phase")
            entry["phase"]="RETIRING"
        if worker is not None: entry["worker"]=dict(worker,phase="RETIRED")
        self._after=dict(entry)
        self._pending_result=RuntimeInstallResult(plan.generation,entry["phase"],target)
        self._terminal_attempted=True
        control._update(prior,value)
        return self._pending_result

    def poll(self, *, cancel=False):
        """Return None while closure is incomplete; never infer success from stdout."""
        if self._result is not None:
            return self._result
        if not self._retire_original(cancel=cancel):
            return None
        if not self._close_observer():
            return None
        observer=self._new_observer()
        pending=self._complete(observer)
        if not self._close_observer():
            return None
        self._result=pending
        return pending


class RuntimeCleanup(RuntimeInstallation):
    _mode="cleanup"
    _ticket_type=RuntimeCleanupReservation
    _request_type=RuntimeCleanupRequest
