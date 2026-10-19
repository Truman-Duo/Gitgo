import copy,uuid
import pytest
from backend.core.storage import runtime_records as c
from backend.core.storage import slot_records


def state(value):return c.RuntimeRegistryState(c.canonical(value))


def empty():return dict(binding='a'*64,epoch=0,active=None,entries=[None,None])


def fresh():return dict(generation=str(uuid.uuid4()),source_manifest='b'*64,plan='c'*64,
    phase='RESERVED',root=None,manifest=None,worker=None)


def worker(mode='install'):
    return dict(launch=str(uuid.uuid4()),mode=mode,phase='INTENT',pid=0,created=0,parent_pid=10,parent_created=20)


def advance(view,value):
    update=c.FORMAT.next_update(view,state(value));offset=update.target*c.framing.CELL_BYTES
    after=c.FORMAT.read(view.raw[:offset]+update.encoded+view.raw[offset+c.framing.CELL_BYTES:]);update.require_result(after)
    return after


def initial():return advance(c.FORMAT.read(bytes(8192)),empty())


def ready(view,index):
    value=view.head.state.value();entry=fresh();value['entries'][index]=entry;view=advance(view,value)
    entry['worker']=worker();view=advance(view,value)
    entry['worker'].update(phase='CREATED',pid=11,created=21);view=advance(view,value)
    entry['worker']['phase']='ACCEPTED';view=advance(view,value)
    entry.update(phase='BUILDING',root=dict(platform='win32',volume=1,file_id=100+index));view=advance(view,value)
    entry.update(phase='MATERIALIZED',manifest='d'*64);view=advance(view,value)
    entry['phase']='READY';entry['worker']['phase']='RETIRED';return advance(view,value)


def active():
    view=ready(initial(),0);value=view.head.state.value();value['entries'][0]['phase']='ACTIVE'
    value.update(active=value['entries'][0]['generation'],epoch=1);return advance(view,value)


def upgraded():
    view=ready(active(),1);value=view.head.state.value();value['entries'][0]['phase']='RETIRING'
    value['entries'][1]['phase']='ACTIVE';value.update(active=value['entries'][1]['generation'],epoch=2)
    return advance(view,value)


def test_runtime_install_activation_upgrade_cleanup_and_slot_reuse():
    view=upgraded();value=view.head.state.value();old=value['entries'][0]
    assert old['phase']=='RETIRING' and old['root'] is not None
    old['worker']=worker('cleanup');view=advance(view,value)
    old['worker'].update(phase='CREATED',pid=11,created=22);view=advance(view,value)
    old['worker']['phase']='ACCEPTED';view=advance(view,value)
    old['worker']['phase']='RETIRED';old['phase']='ABSENT';view=advance(view,value)
    value['entries'][0]=None;view=advance(view,value)
    view=ready(view,0)
    assert view.head.state.value()['entries'][1]['phase']=='ACTIVE'
    assert view.head.state.value()['epoch']==2


@pytest.mark.parametrize('kind',['root','manifest','plan','source','worker','active','epoch','drop','reinstall'])
def test_runtime_active_generation_cannot_silently_change_or_release(kind):
    view=active();value=view.head.state.value();entry=value['entries'][0]
    if kind=='root':entry['root']['file_id']+=1
    elif kind=='manifest':entry['manifest']='e'*64
    elif kind=='plan':entry['plan']='e'*64
    elif kind=='source':entry['source_manifest']='e'*64
    elif kind=='worker':entry['worker']['pid']+=1
    elif kind=='active':value['active']=None
    elif kind=='epoch':value['epoch']+=1
    elif kind=='drop':value['entries'][0]=None
    else:entry['worker']=worker()
    with pytest.raises(c.framing.JournalUnknown):advance(view,value)


@pytest.mark.parametrize('kind',['same_slot','old_untracked','old_absent','no_epoch','no_old_retirement'])
def test_upgrade_requires_atomic_pointer_and_retained_old_generation(kind):
    view=ready(active(),1);value=view.head.state.value()
    value['entries'][1]['phase']='ACTIVE';value['entries'][0]['phase']='RETIRING'
    value.update(active=value['entries'][1]['generation'],epoch=2)
    if kind=='same_slot':value['entries'][1]['generation']=value['entries'][0]['generation']
    elif kind=='old_untracked':value['entries'][0]=None
    elif kind=='old_absent':value['entries'][0]['phase']='ABSENT'
    elif kind=='no_epoch':value['epoch']=1
    else:value['entries'][0]['phase']='ACTIVE'
    with pytest.raises(c.framing.JournalUnknown):advance(view,value)


def test_live_installer_cannot_be_activated_or_discarded():
    view=ready(initial(),0);value=view.head.state.value();value['entries'][0]['worker']['phase']='ACCEPTED'
    with pytest.raises(c.framing.JournalUnknown):state(value)
    value=view.head.state.value();value['entries'][0]=None
    with pytest.raises(c.framing.JournalUnknown):advance(view,value)


@pytest.mark.parametrize('target',['head','peer'])
def test_torn_runtime_cell_never_falls_back_to_reusable_state(target):
    view=upgraded();which=getattr(view,target);raw=bytearray(view.raw)
    raw[which.index*4096+100]^=1
    with pytest.raises(c.framing.JournalUnknown,match='torn'):c.FORMAT.read(bytes(raw))


def test_missing_runtime_peer_after_bootstrap_is_unknown():
    view=upgraded();offset=view.peer.index*4096
    with pytest.raises(c.framing.JournalUnknown,match='missing peer'):
        c.FORMAT.read(view.raw[:offset]+bytes(4096)+view.raw[offset+4096:])


def test_runtime_codec_rejects_storage_worker_record_and_duplicate_keys():
    value=empty();raw=b'{"epoch":0,'+c.canonical(value)[1:]
    with pytest.raises(c.framing.JournalUnknown,match='duplicate'):c.RuntimeRegistryState(raw)
    with pytest.raises(c.framing.JournalUnknown):slot_records.FORMAT.read(initial().raw)


def test_cleanup_intent_cannot_create_another_install_ticket():
    view=upgraded();value=view.head.state.value();value['entries'][0]['worker']=worker('install')
    with pytest.raises(c.framing.JournalUnknown,match='another installer'):advance(view,value)


def test_unknown_cleanup_keeps_generation_charged_and_blocks_slot_clear():
    view=upgraded();value=view.head.state.value();value['entries'][0]['worker']=worker('cleanup');view=advance(view,value)
    value['entries'][0]=None
    with pytest.raises(c.framing.JournalUnknown,match='committed absence'):advance(view,value)


def test_installer_intent_requires_exact_identity_before_acceptance():
    view=initial();value=view.head.state.value();value['entries'][0]=fresh();view=advance(view,value)
    value['entries'][0]['worker']=worker();view=advance(view,value)
    value['entries'][0]['worker'].update(phase='ACCEPTED',pid=11,created=21)
    with pytest.raises(c.framing.JournalUnknown):advance(view,value)
