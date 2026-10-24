"""Explicit checkpoint route behind StorageRuntime; owns no second database.

Only new protected projects in an already installed canonical Windows Host are
supported. Selected workers own all SQLite and CAS access. Other repository
operations must be qualified separately and cannot fall through to legacy I/O.
"""
import os,threading,time,uuid
from .host_records import guid,require
from .native_windows_host import WindowsHostRegistry
from .native_windows_process import WindowsProcessLibrary
from .runtime_control import RuntimeControl
from .runtime_lifecycle import select
from .project_control import ProjectControl
from .project_profile import ProjectProfile
from .project_records import ProjectRecord
from .project_launch import ProjectInitialization
from .project_recovery import recover
from .operational_launch import ProjectOperation
from .cycle_ledger import CycleLedger
from .operation_payload import payload_bytes
from .lifecycle import CheckpointRef
from .models import StorageBlocked,StorageCommitUnknown
from .support import _utc_now

class StorageCleanupIncomplete(StorageBlocked):
    def __init__(self,reference):
        self.committed_reference=reference
        super().__init__('STORAGE_CHECKPOINT_COMMITTED_CLEANUP_INCOMPLETE')

class ProtectedCheckpoints:
    def __init__(self,registry,processes,project,slot,workspace_hint):
        require(type(registry) is WindowsHostRegistry and type(processes) is WindowsProcessLibrary,
                'explicit trusted canonical Host dependencies required')
        guid(project)
        require(slot is None or type(slot) is int and 0<=slot<registry.layout.envelope.slots,'exact project slot required')
        require(type(workspace_hint) is str and 0<len(workspace_hint.encode('utf8'))<=8192,'bounded workspace metadata required')
        self.registry,self.processes,self.project,self.slot=registry,processes,project,slot
        self.workspace_hint=workspace_hint
        self._pid,self._thread=os.getpid(),threading.get_ident()
        self._hosts=[];self._active=None;self._closed=self._close_started=False

    @classmethod
    def open(cls,registry,processes,project,slot,workspace_hint):
        result=cls(registry,processes,project,slot,workspace_hint)
        require(slot is not None,'existing protected project slot required')
        result._inspect()
        return result

    @classmethod
    def create(cls,registry,processes,workspace_hint):
        result=cls(registry,processes,str(uuid.uuid4()),None,workspace_hint)
        first=result._initialize()
        result.slot=first.slot
        require(first.phase=='MATERIALIZED','new protected project initialization remains incomplete')
        second=result._initialize(verify=True)
        require(second.phase=='READY','independent project readiness remains unconfirmed')
        return result

    def _require(self):
        require((self._pid,self._thread)==(os.getpid(),threading.get_ident()) and not self._closed
                and not self._close_started,'live process-owned checkpoint route required')
        require(self._active is None and not self._hosts,'previous native operation must finish before another request')

    def _owner(self):
        host=self.registry.owner();self._hosts.append(host)
        host.attach()
        return host

    def _release(self,host):
        require(host.close(),'checkpoint Host closure remains incomplete')
        self._hosts.remove(host)

    def _wait(self,launch):
        self._active=launch
        launch.start(self.processes)
        deadline=time.monotonic()+75
        while True:
            value=launch.poll(cancel=time.monotonic()>=deadline)
            if value is not None:
                self._active=None
                if launch.host in self._hosts:self._hosts.remove(launch.host)
                return value
            require(time.monotonic()<deadline+15,'native operation retirement remains incomplete')
            time.sleep(.01)

    def _initialize(self,*,verify=False):
        self._require();host=self._owner()
        try:
            selected=select(RuntimeControl(host))
            require(ProjectProfile.from_pins(selected._pins).operational is not None,'explicit operational runtime profile required')
            control=ProjectControl(host)
            control.reserve(selected,self.project,verify_slot=self.slot if verify else None)
            launch=ProjectInitialization(control)
        except BaseException:
            self._release(host);raise
        return self._wait(launch)

    def _inspect(self):
        self._require();host=self._owner()
        try:
            state=host._read_slot(self.slot).head
            require(state is not None,'recorded protected project is absent')
            require(state.state.value()['reservation']['body']['project']==self.project,'project slot belongs to another project')
            event=state.state.value()['event']
            if event is not None and event['phase']!='RETIRED':
                recover(ProjectControl(host),self.slot,self.processes)
                self._release(host);host=self._owner()
            selected=select(RuntimeControl(host));profile=ProjectProfile.from_pins(selected._pins)
            profile.require_host(host.anchor)
            require(profile.operational is not None,'operational profile required')
            control=ProjectControl(host);control.namespace();control._lease(self.slot)
            state=control.read(self.slot).head.state
            require(state.value()['reservation']['body']['project']==self.project,'project slot belongs to another project')
            record=ProjectRecord.from_bytes(state.project_bytes)
            require(record.phase=='READY' and record.profile==profile.sha256,'protected project is not ready under this runtime')
            return None if state.cycle_bytes is None else CycleLedger.from_bytes(state.cycle_bytes)
        finally:
            if host in self._hosts:self._release(host)

    def _run(self,mode,payload=None):
        self._require();payload={} if payload is None else payload
        payload_bytes(mode,payload)  # Refuse invalid input before Host effects.
        host=self._owner()
        try:
            control=ProjectControl(host)
            control.reserve_operation(select(RuntimeControl(host)),self.project,self.slot,mode,payload)
            launch=ProjectOperation(control)
        except BaseException:
            self._release(host);raise
        return self._wait(launch)

    def _settle(self):
        cycle=self._inspect()
        if cycle is None or cycle.phase=='SETTLED':return
        require(cycle.phase=='OPEN','reset proof needs original native retirement recovery')
        mode=('retire_checkpoint' if 'retire_checkpoint' not in cycle.used else
              'reconcile_preparation' if 'reconcile_preparation' not in cycle.used else None)
        if mode is not None:self._run(mode)
        cycle=self._inspect()
        mode=('collect_obsolete' if 'collect_obsolete' not in cycle.used else
              'reconcile_collection' if 'reconcile_collection' not in cycle.used else None)
        if mode is not None:self._run(mode)
        reset=self._run('reset_wal')
        require(reset.settled,'cleanup facts or native quiescence remain incomplete')

    def recover(self):
        """Reconcile retained facts and cleanup only; never replay publication."""
        self._require();self._settle()

    def save_agent_checkpoint(self,snapshot):
        self._require()
        payload=dict(snapshot=snapshot,workspace_hint=self.workspace_hint,created_at=_utc_now())
        payload_bytes('publish_checkpoint',payload)
        self._settle()
        self._run('publish_checkpoint',payload)
        verified=self._run('verify_checkpoint').observed
        if verified is None:raise StorageCommitUnknown('checkpoint_independent_verification')
        reference=None
        if verified.get('status')=='PUBLISHED':
            require(set(verified)=={'status','checkpoint','revision','event'} and type(verified['checkpoint']) is str
                    and type(verified['revision']) is int and verified['revision']>0,'verified checkpoint reference required')
            reference=CheckpointRef(verified['checkpoint'],verified['revision'])
        try:self._settle()
        except Exception as error:
            if reference is not None:raise StorageCleanupIncomplete(reference) from error
            raise
        if reference is None:raise StorageBlocked('STORAGE_CHECKPOINT_NOT_PUBLISHED')
        return reference

    def load_agent_session_state(self,process_id):
        self._require();self._settle()
        result=self._run('read_checkpoint',dict(process_id=process_id)).observed
        if result is None:raise StorageCommitUnknown('checkpoint_read')
        require(set(result)=={'status','session'} and result['status']=='READ'
                and (result['session'] is None or type(result['session']) is dict),'verified session response required')
        return result['session']

    def __getattr__(self,name):
        from .runtime import StorageRuntime
        if not name.startswith('_') and callable(getattr(StorageRuntime,name,None)):
            raise StorageBlocked('STORAGE_PROTECTED_OPERATION_UNSUPPORTED: '+name)
        raise AttributeError(name)

    def close(self):
        require((self._pid,self._thread)==(os.getpid(),threading.get_ident()),'checkpoint owner changed')
        if self._closed:return
        self._close_started=True
        if self._active is not None:
            deadline=time.monotonic()+15
            while self._active.poll(cancel=True) is None:
                require(time.monotonic()<deadline,'native checkpoint retirement remains incomplete');time.sleep(.01)
            self._active=None
        for host in tuple(self._hosts):self._release(host)
        self._closed=True

    def __enter__(self):self._require();return self
    def __exit__(self,*_):self.close()
