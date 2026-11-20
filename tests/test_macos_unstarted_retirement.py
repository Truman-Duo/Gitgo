import sys
import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX identity and file operations', allow_module_level=True)

from contextlib import nullcontext
import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace
from scripts import retire_macos_unstarted_trial as retire


@pytest.fixture
def trial(tmp_path_factory,monkeypatch):
    root=tmp_path_factory/('a'*32);root.mkdir()
    state=dict(schema=1,uid=200970322,phase='prepared',generation=0,launch_count=0,
               boot_id=[1794196892,197143],invocation_id='b'*32)
    retire.identity.atomic_json(root/'state.json',state)
    helper='/sealed/runtime/gitgo-host'
    (root/'job.plist').write_bytes(plistlib.dumps(dict(Label='org.gitgo.execution-trial.'+root.name,
        ProgramArguments=[helper,'--gitgo-internal-role','macos-execution-service',str(root)])))
    monkeypatch.setattr(retire.mac,'STORE',root.parent)
    monkeypatch.setattr(retire.mac,'private_directory',lambda p:None)
    monkeypatch.setattr(retire,'locked',lambda p:nullcontext())
    monkeypatch.setattr(retire.os,'getuid',lambda:0);monkeypatch.setattr(retire.os,'geteuid',lambda:0)
    monkeypatch.setattr(retire.identity,'bounded_read',lambda p,*a:p.read_bytes())
    monkeypatch.setattr(retire.identity,'boot_id',lambda:'1712b6c7-f85c-42cc-9780-851da992c246')
    monkeypatch.setattr(retire.identity,'owned_rows',lambda uid:[])
    monkeypatch.setattr(retire,'verify_bundle_path',lambda p:None)
    monkeypatch.setattr(retire.time,'sleep',lambda _:None)
    loaded=[True];calls=[]
    def launchctl(action,label):
        calls.append(action)
        if action=='bootout':loaded[0]=False;return SimpleNamespace(returncode=0,stdout='',stderr='')
        return (SimpleNamespace(returncode=0,stdout='path = '+str(root/'job.plist')+'\nprogram = '+helper,stderr='')
            if loaded[0] else SimpleNamespace(returncode=113,stdout='',stderr='Could not find service'))
    monkeypatch.setattr(retire,'launchctl',launchctl)
    monkeypatch.setattr(retire.identity,'low_spawn',lambda *a,**k:pytest.fail('Must not execute'))
    monkeypatch.setattr(retire.identity,'send_uid_signal',lambda *a:pytest.fail('Must not signal'))
    return root,state,calls


def test_unstarted_retirement_archives_and_preserves_boot_and_uid(trial):
    root,state,calls=trial;before=(root/'state.json').read_bytes()
    result=retire.retire(root.name)
    assert result['retired'] and result['launch_count']==0 and calls.count('bootout')==1
    assert next(root.glob('before-retirement-*')).read_bytes()==before
    after=json.loads((root/'state.json').read_text())
    assert after['boot_id']==state['boot_id'] and after['uid']==state['uid']
    assert after['operator_retirement']['outcome']=='never_started'
    assert retire.retire(root.name)==result and calls.count('bootout')==1


@pytest.mark.parametrize('fault',['phase','count','generation','pid','started','live','census_error','job','archive'])
def test_ambiguous_or_live_task_never_unloaded_or_retired(trial,monkeypatch,fault):
    root,state,calls=trial
    if fault=='phase':state['phase']='running'
    if fault=='count':state['launch_count']=1
    if fault=='generation':state['generation']=1
    if fault=='pid':state['pid']=123
    if fault=='started':state['started']=123
    if fault=='live':monkeypatch.setattr(retire.identity,'owned_rows',lambda uid:[{'pid':99}])
    if fault=='census_error':
        def fail(uid):raise OSError('census failed')
        monkeypatch.setattr(retire.identity,'owned_rows',fail)
    if fault=='job':(root/'job.plist').write_bytes(plistlib.dumps({'Label':'other'}))
    if fault=='archive':state['operator_retirement']={'prior_sha256':'invalid'}
    retire.identity.atomic_json(root/'state.json',state);before=(root/'state.json').read_bytes()
    with pytest.raises((ValueError,RuntimeError,OSError)):retire.retire(root.name)
    assert (root/'state.json').read_bytes()==before and 'bootout' not in calls


def source_trial(trial, monkeypatch):
    root,state,calls=trial
    state['boot_id']=retire.identity.boot_id()
    retire.identity.atomic_json(root/'state.json',state)
    (root/'job.plist').write_bytes(plistlib.dumps(dict(Label='org.gitgo.execution-trial.'+root.name,
        ProgramArguments=[retire.identity.PYTHON,'-I',str(root/'macos_execution_service.py'),str(root)])))
    monkeypatch.setattr(retire,'verify_source_service',lambda root,argv:None)
    original=retire.launchctl
    def launchctl(*args):
        result=original(*args)
        result.stdout=result.stdout.replace('/sealed/runtime/gitgo-host',retire.identity.PYTHON)
        return result
    monkeypatch.setattr(retire,'launchctl',launchctl)
    return root,state,calls


def test_current_source_trial_retirement_keeps_proof_and_uid(trial,monkeypatch):
    root,state,calls=source_trial(trial,monkeypatch)
    before=(root/'state.json').read_bytes()
    result=retire.retire(root.name,source=True)
    assert result['retired'] and result['launch_count']==0
    assert next(root.glob('before-retirement-*')).read_bytes()==before
    assert retire.retire(root.name,source=True)==result and calls.count('bootout')==1


@pytest.mark.parametrize('fault',['different_boot','started','module','owner'])
def test_source_retirement_refuses_changed_or_active_lease(trial,monkeypatch,fault):
    root,state,calls=source_trial(trial,monkeypatch)
    if fault=='different_boot':state['boot_id']='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    if fault=='started':state['launch_count']=1
    if fault=='module':
        def fail(*a):raise ValueError('Source differs')
        monkeypatch.setattr(retire,'verify_source_service',fail)
    if fault=='owner':
        def lock(path):
            if Path(path).name=='admission.lock':raise BlockingIOError('Owner still alive')
            return nullcontext()
        monkeypatch.setattr(retire,'locked',lock)
    retire.identity.atomic_json(root/'state.json',state)
    before=(root/'state.json').read_bytes()
    with pytest.raises((ValueError,RuntimeError,BlockingIOError)):retire.retire(root.name,source=True)
    assert (root/'state.json').read_bytes()==before and 'bootout' not in calls


@pytest.mark.parametrize('fault',[None,'bytes','writable','owner','link_count','argv'])
def test_source_module_identity_checked_before_retirement(trial,monkeypatch,fault):
    root,_,_=trial
    names=('macos_identity.py','macos_execution_service.py','macos_resource_guard.py','macos_file_authority.py')
    for name in names:
        (root/name).write_bytes(Path(retire.mac.__file__).with_name(name).read_bytes())
        (root/name).chmod(0o500)
    if fault=='bytes':(root/names[0]).chmod(0o700);(root/names[0]).write_text('different');(root/names[0]).chmod(0o500)
    original=Path.lstat
    def metadata(path,*args,**kwargs):
        info=original(path,*args,**kwargs)
        if path.parent==root and path.name in names:
            fields=list(info);fields[4]=0
            if path.name==names[0]:
                if fault=='writable':fields[0]|=0o200
                if fault=='owner':fields[4]=501
                if fault=='link_count':fields[3]=2
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(Path,'lstat',metadata)
    argv=[retire.identity.PYTHON,'-I',str(root/'macos_execution_service.py'),str(root)]
    if fault=='argv':argv[2]='/other/service.py'
    if fault is None:retire.verify_source_service(root,argv)
    else:
        with pytest.raises(ValueError):retire.verify_source_service(root,argv)
