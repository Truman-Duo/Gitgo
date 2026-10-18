"""Immutable worker reservations and process/runtime lease transitions.

These records describe committed debt. The Host coordinator must supply its
actual gate, admission ticket, selected runtime and native retirement proofs.
Checksums or phase strings alone never authorize replay or capacity release.
"""
from dataclasses import dataclass
import hashlib
import re

from .journal_cells import canonical, decode, require, number, guid, digest, JournalFormat
from .project_records import ProjectRecord, slot_transition
from .cycle_ledger import CycleLedger,COUNTS,transition as cycle_transition

def reservation(value):
    require(type(value) is dict and set(value)=={'body','checksum'},'reservation envelope')
    body=value['body']
    require(type(body) is dict and set(body)=={'version','registry_id','envelope','slot','project','fingerprint'},'reservation fields')
    require(type(body['version']) is int and body['version']==1,'reservation version')
    number(body['slot'],62);guid(body['registry_id']);guid(body['project'])
    digest(body['envelope']);digest(body['fingerprint'])
    require(value['checksum']==hashlib.sha256(canonical(body)).hexdigest(),'reservation checksum')


def event(value):
    if value is None:return
    fields={'phase','launch','pid','created','parent_pid','parent_created','profile','mode','request','generation'}
    require(type(value) is dict and set(value) in (fields|{'runtime'},fields|{'runtime','operation'}),'event fields')
    if 'operation' in value:
        operation=value['operation']
        require(type(operation) is dict and set(operation)=={'input','epoch'},'closed operation binding')
        digest(operation['input']);number(operation['epoch'],(1<<63)-1)
    runtime=value['runtime']
    require(type(runtime) is dict and set(runtime)=={'generation','manifest','epoch'},'exact runtime lease')
    guid(runtime['generation']);digest(runtime['manifest']);number(runtime['epoch'],(1<<63)-1,1)
    require(type(value['phase']) is str and value['phase'] in {'INTENT','CREATED','ACCEPTED','RETIRED'},'event phase')
    guid(value['launch']);guid(value['request']);digest(value['profile'])
    number(value['generation'],(1<<63)-1)
    number(value['parent_pid'],(1<<32)-1,1);number(value['parent_created'],(1<<64)-1,1)
    number(value['pid'],(1<<32)-1);number(value['created'],(1<<64)-1)
    require(bool(value['pid'])==bool(value['created']),'complete child identity')
    require(value['pid']!=value['parent_pid'],'different parent and child')
    require(type(value['mode']) is str and re.fullmatch('[a-z][a-z_0-9]{0,31}',value['mode']) is not None,'bounded mode')
    if value['phase']=='INTENT':require(value['pid']==0,'intent before child identity')
    if value['phase'] in {'CREATED','ACCEPTED'}:require(value['pid']>0,'exact child identity required')


@dataclass(frozen=True)
class WorkerSlotState:
    reservation_bytes:bytes
    event_bytes:bytes|None=None
    project_bytes:bytes|None=None
    cycle_bytes:bytes|None=None

    def __post_init__(self):
        reservation(decode(self.reservation_bytes))
        event(None if self.event_bytes is None else decode(self.event_bytes))
        if self.project_bytes is not None:
            record=ProjectRecord.from_bytes(self.project_bytes)
            require(record.profile==decode(self.reservation_bytes)['body']['fingerprint'],'reserved project profile')

        if self.cycle_bytes is not None:
            CycleLedger.from_bytes(self.cycle_bytes)
            require(self.project_bytes is not None and ProjectRecord.from_bytes(self.project_bytes).phase=='READY',
                    'operational cycle requires a ready protected project')

    def value(self):
        value=dict(reservation=decode(self.reservation_bytes),event=None if self.event_bytes is None else decode(self.event_bytes))
        if self.project_bytes is not None:value['project']=decode(self.project_bytes)
        if self.cycle_bytes is not None:value['cycle']=decode(self.cycle_bytes)
        return value

    def with_event(self,value):return WorkerSlotState(self.reservation_bytes,canonical(value),self.project_bytes,self.cycle_bytes)

    def with_project(self,value):
        require(type(value) is ProjectRecord,'immutable project fact required')
        return WorkerSlotState(self.reservation_bytes,self.event_bytes,value.to_bytes(),self.cycle_bytes)

    def with_cycle(self,value):
        require(type(value) is CycleLedger,'exact immutable cycle required')
        return WorkerSlotState(self.reservation_bytes,self.event_bytes,self.project_bytes,value.to_bytes())


def transition(old,new):
    require(type(new) is WorkerSlotState,'immutable slot state')
    if old is None:
        require(new.event_bytes is None and new.project_bytes is None and new.cycle_bytes is None,'reservation precedes all effects');return
    require(old.reservation_bytes==new.reservation_bytes,'reservation cannot change')
    a=None if old.event_bytes is None else decode(old.event_bytes)
    b=None if new.event_bytes is None else decode(new.event_bytes)
    require(b is not None,'worker debt cannot be erased')
    previous=None if old.project_bytes is None else ProjectRecord.from_bytes(old.project_bytes)
    current=None if new.project_bytes is None else ProjectRecord.from_bytes(new.project_bytes)
    slot_transition(previous,current,a,b,decode(new.reservation_bytes)['body'])
    prior_cycle=None if old.cycle_bytes is None else CycleLedger.from_bytes(old.cycle_bytes)
    next_cycle=None if new.cycle_bytes is None else CycleLedger.from_bytes(new.cycle_bytes)
    if next_cycle is None:
        require(prior_cycle is None,'cycle debt cannot be erased')
        require(b['mode'] not in dict(COUNTS),'writing worker requires its durable cycle debit')
    else:cycle_transition(prior_cycle,next_cycle,a,b)
    if a==b:
        require(previous!=current or prior_cycle!=next_cycle,'unchanged worker cannot replay its journal entry')
        return
    if a is None or a['phase']=='RETIRED':
        require(b['phase']=='INTENT','fresh intent required')
        if a is not None:require(a['launch']!=b['launch'],'launch identity cannot replay')
        return
    same=lambda excluded:a.keys()==b.keys() and all(a[key]==b[key] for key in a if key not in excluded)
    if b['phase']=='CREATED':
        require(a['phase']=='INTENT' and same({'phase','pid','created'}),'created identity transition')
    else:
        require(same({'phase'}) and ((a['phase']=='CREATED' and b['phase']=='ACCEPTED') or
            (a['phase'] in {'INTENT','CREATED','ACCEPTED'} and b['phase']=='RETIRED')),'worker phase transition')


def _parse_state(raw):
    value = decode(raw)
    require(type(value) is dict and set(value) in ({'reservation','event'},{'reservation','event','project'},{'reservation','event','project','cycle'}), 'worker slot payload')
    return WorkerSlotState(canonical(value['reservation']),
        None if value['event'] is None else canonical(value['event']),
        canonical(value['project']) if 'project' in value else None,
        canonical(value['cycle']) if 'cycle' in value else None)


FORMAT = JournalFormat(b'GTSC\x01\x00\x00\x00', _parse_state,
                       lambda state: canonical(state.value()), transition)
