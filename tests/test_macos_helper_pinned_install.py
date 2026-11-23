"""Exact operator admission; real copy/hash/receipt with no host elevation.

Only ownership/OS/codesign boundaries are simulated here. Frozen launchd,
ordinary-user tampering and restart acceptance require the disposable Mac VM.
"""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys

import pytest

if os.name != 'posix':
    pytest.skip('POSIX helper installation', allow_module_level=True)

from backend.core import macos_helper_install as helper
from backend.core import macos_identity as identity
from tests.test_macos_helper_install import bundle


@pytest.fixture
def lab(tmp_path_factory, monkeypatch):
    root=tmp_path_factory/'sealed'
    source=bundle(tmp_path_factory/'candidate')
    monkeypatch.setattr(helper,'BASE',root)
    monkeypatch.setattr(sys,'platform','darwin')
    monkeypatch.setattr(os,'getuid',lambda:0)
    monkeypatch.setattr(os,'geteuid',lambda:0)
    monkeypatch.setattr(helper,'trusted_directory',lambda p:None)
    monkeypatch.setattr(helper,'_check_owner',lambda info,sealed:None)
    monkeypatch.setattr(helper,'require_no_extended_acl',lambda fd:None)
    monkeypatch.setattr(helper,'_install_lock',lambda:nullcontext())
    monkeypatch.setattr(os,'chown',lambda *a,**k:None)
    monkeypatch.setattr(identity,'bounded_read',lambda path,*a:Path(path).read_bytes())
    monkeypatch.setattr(helper.subprocess,'run',lambda *a,**k:None)
    return source,root


def admit(source):
    return helper.install(source,expected_bundle_sha256=helper.identity(helper.inventory(source)))


@pytest.mark.parametrize('pin',[True,'','A'*64,'1'*63,'../escape',1])
def test_malformed_pin_does_not_read_or_publish(lab,monkeypatch,pin):
    source,root=lab
    monkeypatch.setattr(helper,'inventory',lambda *a,**k:pytest.fail('invalid pin read source'))
    with pytest.raises(ValueError,match='SHA-256'): helper.install(source,expected_bundle_sha256=pin)
    assert not root.exists()


def test_mismatched_pin_precedes_codesign_and_root_writes(lab,monkeypatch):
    source,root=lab
    monkeypatch.setattr(helper.subprocess,'run',lambda *a,**k:pytest.fail('codesign before digest admission'))
    with pytest.raises(ValueError,match='confirmed SHA-256'):
        helper.install(source,expected_bundle_sha256='0'*64)
    assert not root.exists()


def test_pin_and_legacy_flag_are_not_combined(lab):
    source,root=lab
    with pytest.raises(ValueError,match='either'):
        helper.install(source,allow_local_adhoc=True,expected_bundle_sha256='0'*64)
    assert not root.exists()


def test_pin_binds_all_dependencies_and_survives_source_removal(lab):
    source,root=lab
    host=admit(source)
    receipt=helper.read_receipt(host.parent.parent)
    assert receipt['schema']==2 and receipt['trust']=={
        'kind':'operator-pinned-sha256','sha256':host.parent.parent.name}
    assert receipt['production_enabled'] is False
    helper.shutil.rmtree(source)
    assert helper.verify_bundle_path(host)==host.parent.parent
    (host.parent/'_internal/data').write_bytes(b'changed')
    with pytest.raises(ValueError,match='snapshot'): helper.verify_bundle_path(host)


@pytest.mark.parametrize('fault',['receipt_pin','receipt_id','duplicate','bool_schema','production','missing'])
def test_damaged_receipt_never_admits_existing_runtime(lab,fault):
    source,root=lab;host=admit(source);path=host.parent.parent/'install.json'
    receipt=json.loads(path.read_text())
    if fault=='receipt_pin': receipt['trust']['sha256']='0'*64
    elif fault=='receipt_id': receipt['bundle_id']='0'*64
    elif fault=='bool_schema': receipt['schema']=True
    elif fault=='production': receipt['production_enabled']=True
    elif fault=='missing': receipt.pop('trust')
    raw=json.dumps(receipt)
    if fault=='duplicate': raw=raw[:-1]+',"schema":2}'
    path.write_text(raw)
    with pytest.raises(ValueError): helper.verify_bundle_path(host)


def test_legacy_snapshot_adoption_is_explicit_and_never_downgrades(lab):
    source,root=lab
    host=helper.install(source,allow_local_adhoc=True)
    assert helper.read_receipt(host.parent.parent)['schema']==1
    assert admit(source)==host
    before=(host.parent.parent/'install.json').read_bytes()
    assert helper.install(source,allow_local_adhoc=True)==host
    assert (host.parent.parent/'install.json').read_bytes()==before


def test_existing_corrupted_snapshot_cannot_be_promoted(lab):
    source,root=lab;host=helper.install(source,allow_local_adhoc=True)
    before=(host.parent.parent/'install.json').read_bytes()
    (host.parent/'_internal/data').write_bytes(b'changed')
    with pytest.raises(ValueError,match='does not match'): admit(source)
    assert (host.parent.parent/'install.json').read_bytes()==before


def test_source_replacement_during_copy_is_never_published(lab,monkeypatch):
    source,root=lab;copytree=helper.shutil.copytree
    def racing(src,dest,*args,**kwargs):
        if Path(src)==source:
            (source/'_internal/data').write_bytes(b'swapped')
        return copytree(src,dest,*args,**kwargs)
    monkeypatch.setattr(helper.shutil,'copytree',racing)
    with pytest.raises(ValueError,match='changed'): admit(source)
    assert list(root.iterdir())==[]


def test_pinned_to_legacy_transition_rejected_but_pinned_older_build_allowed(lab):
    source,root=lab;old=admit(source)
    (source/'_internal/data').write_bytes(b'other')
    candidate=helper.install(source,allow_local_adhoc=True)
    with pytest.raises(ValueError,match='confirmed candidate'):
        helper.require_transition_trust(old,candidate)
    assert admit(source)==candidate
    helper.require_transition_trust(old,candidate)
    helper.require_transition_trust(candidate,old)
