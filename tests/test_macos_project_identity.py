"""Actual descriptor attribute checks plus remount authorization boundaries."""
import json
import os
from pathlib import Path
import struct
import sys
import pytest
if sys.platform!='darwin':pytest.skip('Darwin attributes',allow_module_level=True)
from backend.core import macos_project_identity as objects,macos_project_authority as auth
from backend.core import macos_identity as identity,macos_broker_protocol as wire,macos_broker_service as broker
from tests.test_macos_project_authority import scope
from tests.test_macos_helper_lifecycle import rig


def test_actual_pinned_directory_identity_survives_rename_but_not_replacement(tmp_path_factory):
    project=tmp_path_factory/'project';project.mkdir();fd=wire.open_directory(project)
    try:
        before=objects.persistent_identity(fd);s=os.fstat(fd)
        assert before['file_id']==s.st_ino and before['owner_uid']==os.getuid()
        assert before['volume_uuid']!='0'*32 and len(before['volume_uuid'])==32
        (project/'ordinary-output').write_text('contents do not change identity')
        assert objects.persistent_identity(fd)==before
        project.rename(project.with_name('held'));project.mkdir()
        assert objects.persistent_identity(fd)==before
        other=wire.open_directory(project)
        try:
            after=objects.persistent_identity(other)
            assert after!=before and after['volume_uuid']==before['volume_uuid']
        finally:os.close(other)
    finally:os.close(fd)


@pytest.mark.parametrize('fault',['volume','file_id','birth','owner','missing','same_boot'])
def test_renew_refuses_object_changes_even_when_device_changes(scope,monkeypatch,fault):
    root,old=scope;key=old['identity'];object_id=dict(schema=1,volume_uuid='a'*32,file_id=key[1],birth=[100,5],owner_uid=key[2])
    if fault!='missing':old['project_object']=dict(object_id)
    identity.atomic_json(root/'private/grant.json',old);before=(root/'private/grant.json').read_bytes()
    actual=dict(object_id)
    if fault=='volume':actual['volume_uuid']='b'*32
    if fault=='file_id':actual['file_id']+=1
    if fault=='birth':actual['birth']=[100,6]
    if fault=='owner':actual['owner_uid']+=1
    monkeypatch.setattr(objects,'persistent_identity',lambda fd:actual)
    monkeypatch.setattr(wire,'directory_identity',lambda fd:[key[0]+2,*key[1:]])
    monkeypatch.setattr(identity,'boot_id',lambda:'boot' if fault=='same_boot' else 'new-boot')
    with pytest.raises(ValueError):auth.renew('demo',after_reboot=True)
    assert (root/'private/grant.json').read_bytes()==before and not auth.pending(root)


def test_explicit_cross_boot_renewal_updates_dev_only_after_persistent_match(scope,monkeypatch):
    root,old=scope;key=old['identity'];anchor=dict(schema=1,volume_uuid='a'*32,file_id=key[1],birth=[100,5],owner_uid=key[2])
    old['project_object']=anchor;identity.atomic_json(root/'private/grant.json',old)
    monkeypatch.setattr(objects,'persistent_identity',lambda fd:anchor)
    monkeypatch.setattr(wire,'directory_identity',lambda fd:[key[0]+2,*key[1:]])
    monkeypatch.setattr(identity,'boot_id',lambda:'new-boot')
    with pytest.raises(ValueError,match='Cross-boot'):auth.renew('demo')
    assert auth.renew('demo',after_reboot=True)['authorization_live']
    new=broker.grant(root,live=False);client=json.loads((root/'client.json').read_text())
    assert new['identity']==[key[0]+2,*key[1:]] and new['project_object']==anchor
    assert client['identity']==new['identity'] and client['project_object']==anchor
    assert new['grant_id']!=old['grant_id'] and new['boot']=='new-boot'


@pytest.mark.parametrize('fault',['zero_volume','bad_nsec','different_inode','different_owner'])
def test_incomplete_or_invalid_filesystem_identity_is_rejected(tmp_path_factory,monkeypatch,fault):
    fd=wire.open_directory(tmp_path_factory);s=os.fstat(fd)
    def attrs(fd,common,volume,size):
        if volume:return bytes(16) if fault=='zero_volume' else bytes.fromhex('a'*32)
        return struct.pack('=qqIQ',100,1000000000 if fault=='bad_nsec' else 0,
            s.st_uid+1 if fault=='different_owner' else s.st_uid,s.st_ino+1 if fault=='different_inode' else s.st_ino)
    monkeypatch.setattr(objects,'attributes',attrs)
    try:
        with pytest.raises(ValueError):objects.persistent_identity(fd)
    finally:os.close(fd)
