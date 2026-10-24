from dataclasses import replace
import json
import uuid

import pytest

from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage.envelope import FileEnvelope, StorageEnvelope
from backend.core.storage.host_records import (HostAccount, HostAnchor, HostAuthorityInvalid, HostInput, HostLayout, metadata_classes)
from backend.core.storage.runtime_manifest import RuntimeManifest, canonical, with_runtime_copies
from tests.test_storage_runtime_manifest import content


def layout(slots=2):
    envelope = StorageEnvelope(1,4096,slots,8*1024*1024,metadata_classes(slots),
                              (FileEnvelope("state",65536,2),))
    return HostLayout(with_runtime_copies(envelope,content()),content())


def input_copy(plan=None):
    plan = plan or layout()
    return HostInput.from_manifest(RuntimeManifest(r"E:\approved-input",ObjectIdentity("win32",8,90),plan.content))


def anchor():
    plan = layout()
    return HostAnchor(str(uuid.uuid4()),HostAccount("S-1-5-21-123",r"C:\Profiles\host"),
        ObjectIdentity("win32",7,11),ObjectIdentity("win32",7,12),plan,input_copy(plan),
        tuple((member.name,ObjectIdentity("win32",7,index+20)) for index,member in enumerate(sorted(plan.members,key=lambda item:item.name))))


def test_host_account_has_one_location_and_does_not_consult_state_home(monkeypatch):
    monkeypatch.setenv("GITGO_STATE_HOME",r"D:\another-budget")
    monkeypatch.setenv("USERPROFILE",r"D:\different-account")
    account = HostAccount("S-1-5-21-123",r"C:\Profiles\host")
    assert account.root == r"C:\Profiles\host\.gitgo\storage-authority-v1"


def test_host_record_binds_all_actual_member_identities_and_shared_layout():
    original = anchor()
    assert HostAnchor.from_bytes(original.to_bytes(),original.account,original.layout)==original
    assert len(original.intent())<=4096 and len(original.to_bytes())<=65536
    changed = replace(original.layout.content,storage_profile="e"*64)
    with pytest.raises(HostAuthorityInvalid):
        HostAnchor.from_bytes(original.to_bytes(),original.account,replace(original.layout,content=changed))


@pytest.mark.parametrize("case", ["duplicate","unknown","version","principal","root-alias","member-alias","missing","parent-volume"])
def test_unknown_or_changed_host_records_never_supply_an_anchor(case):
    original = anchor()
    body = json.loads(original.to_bytes())
    if case == "duplicate": raw=original.to_bytes()[:-1]+b',"version":1}'
    else:
        if case == "unknown": body["repair"]=True
        elif case == "version": body["version"]=True
        elif case == "principal": body["account"]["sid"]="S-1-5-21-124"
        elif case == "root-alias": body["root"]=body["parent"]
        elif case == "member-alias": body["members"]["authority.json"]=body["root"]
        elif case == "missing": body["members"].pop("envelope.json")
        elif case == "parent-volume": body["parent"]["volume"]=8
        raw=canonical(body)
    with pytest.raises(HostAuthorityInvalid):
        HostAnchor.from_bytes(raw,original.account,original.layout)


@pytest.mark.parametrize("slots", [0,12,True,64])
def test_host_bootstrap_capacity_is_checked_before_effects(slots):
    with pytest.raises(HostAuthorityInvalid):
        metadata_classes(slots)


def test_missing_input_budget_or_project_reference_capacity_is_refused():
    original = layout()
    for missing in ("host_project_ref","input_000004096"):
        files=tuple(item for item in original.envelope.registry_files if item.name!=missing)
        with pytest.raises(HostAuthorityInvalid):
            replace(original,envelope=replace(original.envelope,registry_files=files))


@pytest.mark.parametrize("change", ["root","identity","digest","content","extra","missing","nested-identity"])
def test_host_input_record_cannot_change_independently_of_its_complete_manifest(change):
    from backend.core.storage.models import StorageBlocked
    original=anchor()
    body=json.loads(original.to_bytes())
    source=body["source"]
    if change=="root": source["root"]=r"E:\second-input"
    elif change=="identity": source["root_identity"]["file_id"]+=1
    elif change=="digest": source["manifest_digest"]="0"*64
    elif change=="content": source["content_contract"]="0"*64
    elif change=="extra": source["allow_replacement"]=True
    elif change=="missing": source.pop("root")
    elif change=="nested-identity": source["root_identity"]["generation"]=1
    with pytest.raises(StorageBlocked):
        HostAnchor.from_bytes(canonical(body),original.account,original.layout)


@pytest.mark.parametrize("location", [r"C:\Profiles\host",r"C:\Profiles\host\.gitgo\storage-authority-v1",
    r"c:\PROFILES\host\.GITGO\storage-authority-v1\input"])
def test_input_copy_cannot_overlap_the_canonical_host_tree(location):
    original=anchor()
    source=HostInput.from_manifest(RuntimeManifest(location,ObjectIdentity("win32",8,90),original.layout.content))
    with pytest.raises(HostAuthorityInvalid,match="disjoint"):
        replace(original,source=source)


def test_input_relocation_changes_authority_and_intent_even_with_identical_content():
    original=anchor()
    source=HostInput.from_manifest(RuntimeManifest(r"E:\other-approved-input",ObjectIdentity("win32",8,91),original.layout.content))
    relocated=replace(original,source=source)
    assert original.layout==relocated.layout
    assert original.binding!=relocated.binding and original.intent()!=relocated.intent()


def test_input_root_must_not_alias_any_host_object():
    original=anchor()
    source=HostInput.from_manifest(RuntimeManifest(r"E:\approved-input",original.root_identity,original.layout.content))
    with pytest.raises(HostAuthorityInvalid,match="aliases"):
        replace(original,source=source)
