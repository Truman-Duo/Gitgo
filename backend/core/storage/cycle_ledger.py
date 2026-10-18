"""One serial storage cycle in the existing Host slot, not a grant by itself.

The coordinator must commit a debit with the exact native launch intent before
effects, and independently prove quiescent reset before settling a cycle.
These values cannot prove worker death, file absence, or durable journal I/O.
"""
from dataclasses import asdict,dataclass,replace

from backend.core.storage.host_records import guid,require
from backend.core.storage.runtime_manifest import canonical,digest
from backend.core.storage.journal_cells import decode
from backend.core.storage.wal_capacity import WalFrameBound,WalDebt,WalSchedule

MAX_MEMBERS=8
# Closed whole-worker attempts. Planning is charged conservatively despite
# being read-only. Each member has a birth and seal transaction; the complete
# prepare/publication are each atomic. Retirement and collection have one
# durable intent plus one final metadata transaction, not per-object commits.
COUNTS=(('publish_checkpoint',1+1+2*MAX_MEMBERS+1),
        ('retire_checkpoint',2),('collect_obsolete',2),
        ('reconcile_preparation',2),('reconcile_collection',2))
READ_MODES=('verify_checkpoint','read_checkpoint','reset_wal')


def schedule():
    # Both normal and bounded recovery attempts remain charged. More faults
    # than the finite range permits retain debt and block; they do not refill it.
    return WalSchedule(WalFrameBound(4096,2048,65536),0,
        tuple(sorted((WalDebt(mode,1,0,count) for mode,count in COUNTS),key=lambda item:item.operation)),1)


@dataclass(frozen=True)
class CycleLedger:
    epoch:int
    request:str
    input_digest:str
    phase:str
    used:tuple[str,...]

    def __post_init__(self):
        require(type(self.epoch) is int and 0<self.epoch<(1<<63),'finite cycle epoch')
        guid(self.request);digest(self.input_digest)
        require(self.phase in {'OPEN','VERIFIED','SETTLED'},'closed cycle phase')
        require(type(self.used) is tuple and 1<=len(self.used)<=len(COUNTS)
                and self.used[0]=='publish_checkpoint'
                and len(set(self.used))==len(self.used)
                and all(type(mode) is str and mode in dict(COUNTS) for mode in self.used),
                'complete unique consumed worker ranges')
        require(self.phase=='OPEN' or 'retire_checkpoint' in self.used
                or 'reconcile_preparation' in self.used,'preparation retirement must be accounted')

    def to_bytes(self):return canonical(dict(version=1,**asdict(self)))

    @classmethod
    def from_bytes(cls,raw):
        require(type(raw) is bytes and 0<len(raw)<=768,'bounded cycle ledger')
        value=decode(raw)
        require(type(value) is dict and set(value)=={'version','epoch','request','input_digest','phase','used'}
                and type(value['version']) is int and value['version']==1
                and type(value['used']) is list,'closed cycle ledger encoding')
        result=cls(**{k:v for k,v in value.items() if k not in {'version','used'}},used=tuple(value['used']))
        require(result.to_bytes()==raw,'canonical cycle ledger')
        return result

    @property
    def attempts(self):return sum(dict(COUNTS)[mode] for mode in self.used)

    def consume(self,mode):
        require(self.phase=='OPEN' and mode in dict(COUNTS) and mode not in self.used,
                'write range cannot be repeated or refunded')
        require(mode!='reconcile_preparation' or 'retire_checkpoint' in self.used,
                'reconciliation follows its original consumed attempt')
        require(mode!='reconcile_collection' or 'collect_obsolete' in self.used,
                'collection reconciliation follows original attempt')
        return replace(self,used=(*self.used,mode))

    def verified(self):
        require(self.phase=='OPEN','one complete quiescent verification required')
        return replace(self,phase='VERIFIED')

    def settle(self):
        # Data transition only. Actual reset/retirement proof belongs to the
        # canonical coordinator and must precede committing this new value.
        require(self.phase=='VERIFIED','independent complete quiescent verification required')
        return replace(self,phase='SETTLED')


def start(previous,request,input_digest):
    require(previous is None or type(previous) is CycleLedger and previous.phase=='SETTLED',
            'unsettled previous cycle retains its complete debt')
    require(previous is None or request!=previous.request,'logical request cannot replay')
    return CycleLedger(1 if previous is None else previous.epoch+1,request,input_digest,'OPEN',('publish_checkpoint',))


def transition(old,new,old_event,new_event):
    require(type(new) is CycleLedger,'complete immutable cycle record')
    if old==new:
        require(type(new_event) is dict,'cycle needs an exact worker event')
        require(new_event['phase']!='INTENT' or new_event['mode'] in READ_MODES,
                'a fresh writing worker must consume its range in the same journal commit')
        require(new_event['request']==new.request
                or new.phase=='SETTLED' and new_event['mode']=='read_checkpoint',
                'active cycle worker request changed')
        return
    require(type(new_event) is dict and new_event['request']==new.request,'cycle must bind the same worker request')
    if old is None or old.phase=='SETTLED':
        require(new==start(old,new.request,new.input_digest)
                and new_event['phase']=='INTENT' and new_event['mode']=='publish_checkpoint',
                'fresh cycle debit and publication intent must commit together')
    elif new.phase=='VERIFIED':
        require(new==old.verified() and new_event['phase']=='ACCEPTED' and new_event['mode']=='reset_wal',
                'quiescent verification belongs to its accepted reset worker')
    elif new.phase=='SETTLED':
        require(new==old.settle() and new_event['phase']=='RETIRED' and new_event['mode']=='reset_wal',
                'settlement belongs to the retired reset worker')
    else:
        mode=new_event['mode']
        require(new==old.consume(mode) and new_event['phase']=='INTENT'
                and old_event is not None and old_event['phase']=='RETIRED',
                'one fresh worker debit after original retirement required')
