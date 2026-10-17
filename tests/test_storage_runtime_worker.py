from dataclasses import replace
import ctypes
import json
import uuid

import pytest

from backend.core.storage.models import StorageBlocked
from backend.core.storage.native_windows_process import WindowsProcessLibrary
from backend.core.storage.runtime_manifest import canonical
from backend.core.storage.runtime_worker import RuntimeInstallRequest, materialize
from tests.test_storage_runtime_control import runtime_case
from tests.test_storage_windows_tree import native


def request():
    return RuntimeInstallRequest("a"*64,0,str(uuid.uuid4()),"b"*64,"c"*64,str(uuid.uuid4()))


def test_worker_request_has_a_closed_canonical_roundtrip():
    value=request()
    assert RuntimeInstallRequest.from_bytes(value.to_bytes())==value
    assert len(value.to_bytes())<4096


@pytest.mark.parametrize("change", ["extra","missing","duplicate","mode","version","slot","generation","source","location","noncanonical"])
def test_worker_request_never_accepts_an_alternate_location_or_ambiguous_protocol(change):
    value=request();body=json.loads(value.to_bytes())
    if change=="duplicate": raw=value.to_bytes()[:-1]+b',"slot":0}'
    elif change=="noncanonical": raw=b' '+value.to_bytes()
    else:
        if change=="extra": body["grant"]=True
        elif change=="missing": body.pop("launch")
        elif change=="mode": body["mode"]="shell"
        elif change=="version": body["version"]=True
        elif change=="slot": body["slot"]=True
        elif change=="generation": body["generation"]="../another-root"
        elif change=="source": body["source"]="self-approved"
        elif change=="location": body["host_root"]="another-authority"
        raw=canonical(body)
    with pytest.raises(StorageBlocked): RuntimeInstallRequest.from_bytes(raw)


def test_development_interpreter_cannot_materialize_an_approved_input(native,tmp_path_factory):
    with runtime_case(native,tmp_path_factory) as (registry,owner,account,source):
        processes=WindowsProcessLibrary(ctypes.CDLL(str(native[2]/"gitgo-storage-process.dll")))
        moved=owner.take_input(source)
        before=owner._read_slot(63).raw
        with pytest.raises(StorageBlocked,match="exact pinned input interpreter"):
            materialize(owner,moved,processes,request())
        assert not owner._ready
        from pathlib import Path
        assert not any((Path(account.root)/"runtimes").iterdir())
        assert (Path(account.root)/"slot-0063.json").read_bytes()==before
        assert owner.close() and processes.retained_processes==0
