"""Runtime lifecycle records inside the existing Host registry's bounded cells.

Codec transitions do not authorize creation, activation or quota release. The
Host gate, pinned identities, exact source manifest and native closure supply
those proofs. Unknown/torn cells never fall back to an older active pointer.
"""
from dataclasses import dataclass
from . import journal_cells as framing

canonical,decode,require=framing.canonical,framing.decode,framing.require
number,digest,guid=framing.number,framing.digest,framing.guid
MAGIC=b'GTRT\x01\x00\x00\x00'
PHASES={'RESERVED','BUILDING','MATERIALIZED','READY','ACTIVE','RETIRING','ABSENT'}


def native_identity(value):
    if value is None:return
    require(type(value) is dict and set(value)=={'platform','volume','file_id'} and value['platform']=='win32','runtime native identity')
    number(value['volume'],2**64-1);number(value['file_id'],2**128-1)


def worker(value):
    if value is None:return
    require(type(value) is dict and set(value)=={'launch','mode','phase','pid','created','parent_pid','parent_created'},'runtime installer fields')
    require(type(value['mode']) is str and value['mode'] in {'install','cleanup'},'runtime worker mode')
    guid(value['launch']);require(type(value['phase']) is str and value['phase'] in {'INTENT','CREATED','ACCEPTED','RETIRED'},'runtime installer phase')
    number(value['parent_pid'],2**32-1,1);number(value['parent_created'],2**64-1,1)
    number(value['pid'],2**32-1);number(value['created'],2**64-1)
    require(bool(value['pid'])==bool(value['created']) and value['pid']!=value['parent_pid'],'exact installer identity')
    if value['phase']=='INTENT':require(value['pid']==0,'installer intent before native identity')
    if value['phase'] in {'CREATED','ACCEPTED'}:require(value['pid']>0,'installer native identity required')


def entry(value):
    if value is None:return
    require(type(value) is dict and set(value)=={'generation','source_manifest','plan','phase','root','manifest','worker'},'runtime generation fields')
    guid(value['generation']);digest(value['source_manifest']);digest(value['plan'])
    require(type(value['phase']) is str and value['phase'] in PHASES,'runtime generation phase');native_identity(value['root']);worker(value['worker'])
    if value['manifest'] is not None:digest(value['manifest'])
    if value['phase']=='RESERVED':require(value['root'] is None and value['manifest'] is None,'fresh runtime reservation')
    if value['phase'] in {'BUILDING','MATERIALIZED','READY','ACTIVE'}:require(value['root'] is not None,'runtime root evidence')
    if value['phase'] in {'MATERIALIZED','READY','ACTIVE'}:require(value['manifest'] is not None,'complete runtime manifest evidence')
    if value['phase'] in {'READY','ACTIVE','ABSENT'}:
        require(value['worker'] is not None and value['worker']['phase']=='RETIRED','runtime worker closure must be recorded')
    if value['phase']=='ABSENT':require(value['worker']['mode']=='cleanup','runtime absence requires cleanup owner')


@dataclass(frozen=True)
class RuntimeRegistryState:
    raw:bytes

    def __post_init__(self):
        value=decode(self.raw)
        require(type(value) is dict and set(value)=={'binding','epoch','active','entries'},'runtime registry state')
        digest(value['binding']);number(value['epoch'],2**63-1)
        require(type(value['entries']) is list and len(value['entries'])==2,'two prepaid runtime slots')
        for item in value['entries']:entry(item)
        ids=[item['generation'] for item in value['entries'] if item is not None]
        require(len(ids)==len(set(ids)),'unique runtime generation')
        active=[item['generation'] for item in value['entries'] if item is not None and item['phase']=='ACTIVE']
        require(active==([] if value['active'] is None else [value['active']]),'one exact active runtime')

    def value(self):return decode(self.raw)


def worker_transition(old,new,*,allow_cleanup=False):
    if old==new:return
    if old is None:
        require(new is not None and new['phase']=='INTENT' and
            (new['mode']=='install' or allow_cleanup and new['mode']=='cleanup'),'installer intent precedes creation');return
    require(new is not None,'installer debt cannot disappear')
    same=lambda excluded:all(old[key]==new[key] for key in old if key not in excluded)
    if old['phase']=='RETIRED':
        require(allow_cleanup and new['phase']=='INTENT' and new['mode']=='cleanup' and new['launch']!=old['launch'],
            'runtime generation cannot issue another installer');return
    if new['phase']=='CREATED':
        require(old['phase']=='INTENT' and same({'phase','pid','created'}),'installer creation transition')
    else:
        require(same({'phase'}) and ((old['phase']=='CREATED' and new['phase']=='ACCEPTED') or new['phase']=='RETIRED'),
            'installer retirement transition')


def transition(old,new):
    require(type(new) is RuntimeRegistryState,'immutable runtime state');b=new.value()
    if old is None:
        require(b['epoch']==0 and b['active'] is None and b['entries']==[None,None],'empty runtime authority bootstrap');return
    a=old.value();require(a['binding']==b['binding'],'runtime Host binding cannot change')
    activation=a['active']!=b['active']
    require(b['epoch']==a['epoch']+(1 if activation else 0),'runtime activation epoch')
    changes=[]
    for index,(prior,next_value) in enumerate(zip(a['entries'],b['entries'])):
        if prior==next_value:continue
        changes.append(index)
        if prior is None:
            require(next_value is not None and next_value['phase']=='RESERVED' and next_value['worker'] is None,'reserve before runtime effects')
            continue
        if next_value is None:
            require(prior['phase']=='ABSENT' and prior['worker']['phase']=='RETIRED','runtime capacity requires committed absence')
            continue
        require(all(prior[key]==next_value[key] for key in ('generation','source_manifest','plan')),'immutable runtime installation intent')
        worker_transition(prior['worker'],next_value['worker'],allow_cleanup=prior['phase']==next_value['phase']=='RETIRING')
        if prior['root'] is not None:require(prior['root']==next_value['root'],'runtime root identity cannot change')
        if prior['manifest'] is not None:require(prior['manifest']==next_value['manifest'],'runtime manifest cannot change')
        p,q=prior['phase'],next_value['phase']
        allowed={'RESERVED':{'RESERVED','BUILDING','RETIRING'},'BUILDING':{'BUILDING','MATERIALIZED','RETIRING'},
            'MATERIALIZED':{'MATERIALIZED','READY','RETIRING'},'READY':{'ACTIVE','RETIRING'},'ACTIVE':{'RETIRING'},
            'RETIRING':{'RETIRING','ABSENT'},'ABSENT':set()}
        require(q in allowed[p],'runtime phase transition')
        if p==q:require(prior['root']==next_value['root'] and prior['manifest']==next_value['manifest'],
            'phase-stable evidence cannot change')
        if q in {'BUILDING','MATERIALIZED'}:
            require(next_value['worker'] is not None and next_value['worker']['phase']=='ACCEPTED','native installer must own materialization')
        if q=='RETIRING' and p!='RETIRING':
            never_started=p=='RESERVED' and prior['worker'] is None and next_value['worker'] is None
            require(never_started or next_value['worker'] is not None and next_value['worker']['phase']=='RETIRED',
                'installer must retire before generation cleanup')
    require(changes,'runtime state transition must progress')
    if activation:
        require(b['active'] is not None,'active runtime cannot vanish during upgrade')
        promoted=[i for i in changes if b['entries'][i] is not None and b['entries'][i]['phase']=='ACTIVE']
        require(len(promoted)==1 and a['entries'][promoted[0]] is not None and a['entries'][promoted[0]]['phase']=='READY',
            'only a verified ready runtime can activate')
        expected=1 if a['active'] is None else 2
        require(len(changes)==expected,'atomic runtime activation handoff')
        if a['active'] is not None:
            old_index=next(i for i,v in enumerate(a['entries']) if v is not None and v['generation']==a['active'])
            require(b['entries'][old_index] is not None and b['entries'][old_index]['phase']=='RETIRING','old runtime remains charged')
    else:require(len(changes)==1,'one runtime preparation step')


FORMAT = framing.JournalFormat(MAGIC, RuntimeRegistryState,
                               lambda state: state.raw, transition)
