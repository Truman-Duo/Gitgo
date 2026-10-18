"""Persisted selected-runtime project launch, retirement and evidence commit."""
from dataclasses import dataclass,replace
from pathlib import PureWindowsPath
import time

from .host_records import require
from .models import StorageCommitUnknown
from .native_windows_host import WindowsHostOwner
from .native_windows_process import WindowsStorageProcess,WindowsProcessLibrary,StorageWorkerCommand
from .project_control import ProjectControl
from .project_records import ProjectRecord
from .project_namespace import ProjectNamespace


@dataclass(frozen=True)
class ProjectInitializationResult:
    project: str
    slot: int
    phase: str
    record: ProjectRecord|None


class ProjectInitialization:
    def __init__(self,control):
        require(type(control) is ProjectControl and control.request is not None and not control._accepted,
                "owned reserved project launch required")
        require(getattr(control,"_launch",None) is None,"one native launch for this project reservation")
        self.control=control;control._launch=self
        self.host=control.host;self.registry=self.host._registry;self.anchor=control.anchor
        self.request=control.request;self._worker=self._identity=self._event=self._observer=None
        self._started=self._create_attempted=self._native_retired=self._parent_closed=False
        self._terminal_attempted=False;self._before=self._after=self._pending=self._result=None

    def start(self,processes):
        require(not self._started and type(processes) is WindowsProcessLibrary,"one exact native project launch required")
        manifest=self.control.selected.require_live(self.request.runtime)
        self._started=True;self._deadline=time.monotonic()+60
        host=self.host;request=self.request
        if request.mode=="initialize":host.require_ticket(self.control.ticket)
        self._worker=host._new(WindowsStorageProcess,processes)
        context=self._worker.current()
        prior=self.control.read(request.slot)
        event=prior.head.state.value()["event"]
        require(event is None or event["phase"]=="RETIRED","previous project worker retains debt")
        self._event=request.event(context)
        self.control.update(prior,prior.head.state.with_event(self._event))
        root=PureWindowsPath(manifest.root)
        command=StorageWorkerCommand((str(root/"python.exe"),"-I","-S","-B","-m","backend.core.storage.project_worker",
            request.to_bytes().decode("ascii")),str(root),(("PATH",str(root)),("SYSTEMROOT",context.system_root),("WINDIR",context.system_root)))
        self._create_attempted=True
        self._worker.create(command)
        self._identity,resumed=self._worker.identity()
        require(not resumed,"project worker resumed before durable process identity")
        prior=self.control.read(request.slot)
        require(prior.head.state.value()["event"]==self._event,"project worker intent changed")
        self._event=dict(self._event,phase="CREATED",pid=self._identity.pid,created=self._identity.creation_time)
        self.control.update(prior,prior.head.state.with_event(self._event))
        host.handoff_gate()
        # The child must acquire the slot lease, while the parent's runtime pins
        # and shared runtime lease remain until actual Job retirement.
        host._scope.close_handle(host._members[f"slot-{request.slot:04d}.lease"])
        self._worker.resume(self._identity);self._worker.release()

    def _retire(self,cancel):
        require(self._started and type(cancel) is bool,"started project worker and explicit cancellation required")
        if not self._native_retired:
            if self._worker is not None and not self._worker.retire(terminate=cancel or time.monotonic()>=self._deadline):return False
            self._native_retired=True
        if not self._parent_closed:
            if not self.host.close():return False
            self._parent_closed=True
        return True

    def _new_observer(self):
        self._observer=object.__new__(WindowsHostOwner)
        WindowsHostOwner.__init__(self._observer,self.registry)
        self._observer.attach();return self._observer

    def _close_observer(self):
        if self._observer is not None:
            if id(self._observer) in self.registry._owners and not self._observer.close():return False
            self._observer=None
        return True

    def _complete(self,host):
        require(self._native_retired and self._parent_closed and host.anchor==self.anchor,
                "all original native owners must retire before project completion")
        control=ProjectControl(host);request=self.request
        prior=control.read(request.slot);state=prior.head.state
        if self._terminal_attempted:
            if state==self._after:
                name=f"slot-{request.slot:04d}.json"
                host._scope.flush_bound(host._root,name,dict(self.anchor.members)[name],8192)
                require(control.read(request.slot).head.state==self._after,"project completion changed during confirmation")
                return self._pending
            if state!=self._before:raise StorageCommitUnknown("project_completion_changed")
        event=state.value()["event"]
        if event is None:
            require(not self._create_attempted and state.project_bytes is None,
                    "project launch evidence disappeared")
            return ProjectInitializationResult(request.project,request.slot,"RESERVED",None)
        require(event is not None and self._event is not None and all(event[key]==self._event[key]
                for key in ("launch","mode","request","profile","runtime","parent_pid","parent_created")),
                "project native launch evidence changed")
        require(not event["pid"] or self._identity is not None and
                (event["pid"],event["created"])==(self._identity.pid,self._identity.creation_time),
                "project native child identity changed")
        require(event["phase"]!="RETIRED","another coordinator already retired this project worker")
        record=None if state.project_bytes is None else ProjectRecord.from_bytes(state.project_bytes)
        self._before=state
        after=state.with_event(dict(event,phase="RETIRED"))
        if record is not None and record.phase=="VERIFIED":
            require(request.mode=="verify_initialize" and event["phase"]=="ACCEPTED",
                    "independent verifier must supply the completion fact")
            # A fresh canonical observer rechecks exact native IDs and closed
            # names after the verifier's entire native lifetime has ended.
            control.namespace()
            namespace=ProjectNamespace(host,record,request.project,self.control.profile.limits).pin()
            namespace.require_empty_cas();namespace.verify()
            record=replace(record,phase="READY");after=after.with_project(record)
        self._after=after
        self._pending=ProjectInitializationResult(request.project,request.slot,"UNKNOWN" if record is None else record.phase,record)
        self._terminal_attempted=True
        control.update(prior,after)
        return self._pending

    def poll(self, *, cancel=False):
        if self._result is not None:return self._result
        if not self._retire(cancel) or not self._close_observer():return None
        host=self._new_observer();pending=self._complete(host)
        if not self._close_observer():return None
        self._result=pending;return pending
