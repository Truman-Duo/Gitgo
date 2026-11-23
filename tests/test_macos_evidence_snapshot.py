"""Atomic replacement must not turn a valid private snapshot into lost IPC."""
import json
import os
import sys
import pytest
if sys.platform!='darwin':pytest.skip('macOS private evidence',allow_module_level=True)
from backend.core import macos_identity as identity


def replacing_reader(monkeypatch,path,replacements):
    original=os.fstat;seen=[]
    def fstat(fd):
        info=original(fd)
        if info.st_ino==path.stat().st_ino and (replacements is None or len(seen)<replacements):
            identity.atomic_json(path,dict(generation=len(seen)+1))
            seen.append(original(fd).st_nlink)
        return original(fd)
    monkeypatch.setattr(identity.os,'fstat',fstat)
    return seen


def test_replaced_open_inode_is_retried_without_mixing_snapshots(tmp_path_factory,monkeypatch):
    path=tmp_path_factory/'state.json';identity.atomic_json(path,dict(generation=0))
    seen=replacing_reader(monkeypatch,path,2)
    assert json.loads(identity.bounded_read(path,os.getuid(),True))==dict(generation=2)
    assert seen==[0,0]


def test_continuously_replaced_evidence_is_bounded_and_refused(tmp_path_factory,monkeypatch):
    path=tmp_path_factory/'state.json';identity.atomic_json(path,dict(generation=0))
    seen=replacing_reader(monkeypatch,path,None)
    with pytest.raises(RuntimeError,match='changed repeatedly'):identity.bounded_read(path,os.getuid(),True)
    assert len(seen)==3


@pytest.mark.parametrize('bad',['hardlink','symlink','fifo','oversize','mode','owner'])
def test_retries_do_not_relax_evidence_validation(tmp_path_factory,bad):
    path=tmp_path_factory/'state.json';identity.atomic_json(path,dict(generation=0))
    owner=os.getuid()
    if bad=='hardlink':os.link(path,tmp_path_factory/'alias')
    elif bad=='symlink':
        target=path.with_name('original');path.rename(target);path.symlink_to(target)
    elif bad=='fifo':path.unlink();os.mkfifo(path)
    elif bad=='oversize':path.write_bytes(b'x'*(identity.MAX_FILE+1))
    elif bad=='mode':path.chmod(0o644)
    elif bad=='owner':owner+=1
    with pytest.raises((OSError,RuntimeError)):identity.bounded_read(path,owner,True)


def test_process_poll_receives_terminal_cleanup_across_atomic_replacement(tmp_path_factory,monkeypatch):
    from backend.core import sandbox_macos
    path=tmp_path_factory/'state.json'
    old=dict(boot_id='boot',invocation_id='a'*32,uid=200000001,phase='running')
    final={**old,'phase':'clean','generation':1,'exit_code':None,'reason':'cancel',
           'cleanup':dict(observed_empty=True,issues=[])}
    identity.atomic_json(path,old)
    original=os.fstat;replaced=False
    def fstat(fd):
        nonlocal replaced
        info=original(fd)
        if not replaced and info.st_ino==path.stat().st_ino:
            replaced=True;identity.atomic_json(path,final)
            assert original(fd).st_nlink==0
        return original(fd)
    monkeypatch.setattr(identity.os,'fstat',fstat)
    monkeypatch.setattr(sandbox_macos,'load_state',lambda root:json.loads(identity.bounded_read(root/'state.json',os.getuid(),True)))
    process=sandbox_macos.DarwinTrialProcess.__new__(sandbox_macos.DarwinTrialProcess)
    process.root=tmp_path_factory;process.boot='boot';process.invocation_id='a'*32;process.uid=200000001;process.returncode=None
    assert process.poll()==-1 and process._gitgo_cleanup['complete'] and not process._gitgo_cleanup['issues']
