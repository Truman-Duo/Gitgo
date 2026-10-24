"""Serial operation launches consume the existing Host slot before execution."""
from dataclasses import dataclass
from .host_records import require
from .cycle_ledger import CycleLedger
from .project_control import ProjectControl
from .project_launch import ProjectInitialization
from .project_records import ProjectRecord
from .project_namespace import ProjectNamespace,ROLES
from .operation_payload import parse_payload
from .models import StorageCommitUnknown

def require_zero_wals(namespace):
    namespace.verify()
    for role in ROLES:
        # None is the native directory API's confirmed absence, not an unknown
        # SQL/VFS measurement. A present member must fit the zero-byte ceiling.
        index=namespace.pins.inspect_sidecar(namespace.indices['data'],role+'.sqlite3-wal',0)
        if index is not None:namespace.pins.release(index)

@dataclass(frozen=True)
class OperationResult:
    project:str
    slot:int
    mode:str
    request:str
    observed:dict|None
    settled:bool

class ProjectOperation(ProjectInitialization):
    def __init__(self,control):
        require(type(control) is ProjectControl and control.request is not None
                and control.request.payload is not None,'reserved operational control required')
        super().__init__(control)
        self._output=bytearray();self._errors=0;self._output_bad=False

    def _intent_state(self,prior,event):
        state=prior.head.state
        if self.request.mode=='publish_checkpoint':
            # After initialization or the previous settled cycle, no old WAL
            # bytes may consume the freshly promised complete window.
            record=ProjectRecord.from_bytes(state.project_bytes)
            ns=ProjectNamespace(self.host,record,self.request.project,self.control.profile.limits).pin()
            require_zero_wals(ns)
            require(self.host._release(ns.pins) and self.host._release(ns.scope),'prelaunch namespace observation remains live')
        after=state.with_event(event)
        if self.control._cycle is not None:after=after.with_cycle(self.control._cycle)
        return after

    def _retire(self,cancel):
        try:return self._drain_and_retire(cancel)
        except BaseException:
            # Once output/status observation is unknown, a later cancellation
            # may retire resources but cannot turn the old buffer into a result.
            self._output_bad=True
            raise

    def _drain_and_retire(self,cancel):
        if not self._native_retired and self._worker is not None and not self._worker._sealed:
            # Drain before retire(), whose ERROR_BUSY return seals Python's
            # owner. Output is bounded even when a selected worker malfunctions.
            for stderr in (False,True):
                for _ in range(65):
                    data=self._worker.read(stderr=stderr)
                    if not data:break
                    if stderr:self._errors+=len(data)
                    else:self._output.extend(data)
                    if len(self._output)>262144 or self._errors>8192:
                        self._output_bad=True;break
            status=self._worker.status()
            if status.active and not cancel and not self._output_bad:
                import time
                if time.monotonic()<self._deadline:return False
            if not status.active:
                # Capture all bytes buffered before the observed exit.
                while not self._output_bad:
                    data=self._worker.read()
                    if not data:break
                    self._output.extend(data)
                    if len(self._output)>262144:self._output_bad=True
        return super()._retire(cancel or self._output_bad)

    def _complete(self,host):
        require(self._native_retired and self._parent_closed and host.anchor==self.anchor,
                'original native owners must retire before observing outcome')
        control=ProjectControl(host);request=self.request
        prior=control.read(request.slot);state=prior.head.state
        if self._terminal_attempted:
            if state==self._after:
                name=f'slot-{request.slot:04d}.json'
                host._scope.flush_bound(host._root,name,dict(self.anchor.members)[name],8192)
                require(control.read(request.slot).head.state==self._after,'operation completion changed')
                return self._pending
            if state!=self._before:raise StorageCommitUnknown('operation_completion_changed')
        event=state.value()['event']
        require(event is not None and self._event is not None and all(event[key]==self._event[key]
                for key in ('launch','mode','request','profile','runtime','parent_pid','parent_created','operation')),
                'operational native launch evidence changed')
        require(not event['pid'] or self._identity is not None and
                (event['pid'],event['created'])==(self._identity.pid,self._identity.creation_time),'child identity changed')
        require(event['phase']!='RETIRED','another coordinator already retired operation')
        observed=None
        if not self._output_bad and event['phase']=='ACCEPTED':
            try:
                value=parse_payload(bytes(self._output).removesuffix(b'\n'),maximum=262144)
                require(set(value)=={'version','request','launch','mode','result'} and value['version']==1
                        and value['request']==request.request and value['launch']==request.launch
                        and value['mode']==request.mode and type(value['result']) is dict,'worker response binding changed')
                observed=value['result']
            except Exception:
                # Malformed or missing output never changes persisted facts.
                observed=None
        self._before=state;after=state.with_event(dict(event,phase='RETIRED'));settled=False
        cycle=None if state.cycle_bytes is None else CycleLedger.from_bytes(state.cycle_bytes)
        if request.mode=='reset_wal' and cycle is not None and cycle.phase=='VERIFIED':
            require(event['phase']=='ACCEPTED','reset fact needs its accepted worker')
            record=ProjectRecord.from_bytes(state.project_bytes)
            control.namespace()
            ns=ProjectNamespace(host,record,request.project,self.control.profile.limits).pin()
            require_zero_wals(ns)
            after=after.with_cycle(cycle.settle());settled=True
        self._after=after
        self._pending=OperationResult(request.project,request.slot,request.mode,request.request,observed,settled)
        self._terminal_attempted=True;control.update(prior,after)
        return self._pending
