"""Incomplete legacy root discovery must never become permission to unlink."""
from dataclasses import FrozenInstanceError, replace
import json
import os

import pytest

from backend.core.storage import StorageRuntime, StorageHealthLevel
from backend.core.storage.cas_inventory import CasInventoryIncomplete
from tests.test_sqlite_storage import _split_checkpoint


@pytest.fixture
def legacy(tmp_path_factory):
    workspace = tmp_path_factory/'work'
    workspace.mkdir()
    with StorageRuntime(workspace,state_home=tmp_path_factory/'state') as runtime:
        runtime.save_agent_checkpoint(_split_checkpoint())
        ref = runtime._state.execute('SELECT metadata_ref FROM sessions').fetchone()[0]
        digest = ref.removeprefix('sha256:')
        path = runtime.paths.cas_dir/digest[:2]/digest[2:]
        nested = runtime._nested_cas_digests(json.loads(path.read_bytes()))
        # Older stores have nested references but no explicit ownership index.
        runtime._state.execute('DELETE FROM object_refs')
        direct = runtime._cas_reference_inventory().direct
        protected = [runtime.paths.cas_dir/d[:2]/d[2:] for d in nested-direct]
        assert protected
        for member in protected: os.utime(member,(1,1))
        yield runtime,ref,path,protected


def corrupt(legacy,kind,monkeypatch):
    runtime,ref,path,_ = legacy
    if kind == 'missing': path.unlink()
    elif kind == 'wrong_hash': path.write_bytes(b'{}')
    elif kind == 'invalid_json': path.write_bytes(b'{bad')
    elif kind == 'oversized': runtime.policy = replace(runtime.policy,max_cas_object_bytes=1)
    elif kind == 'unreadable':
        original = runtime.read_blob
        def read(actual,**kwargs):
            if actual == ref: raise PermissionError('fixture metadata read denied')
            return original(actual,**kwargs)
        monkeypatch.setattr(runtime,'read_blob',read)
    elif kind == 'invalid_reference':
        runtime._state.execute("UPDATE sessions SET metadata_ref='sha256:invalid'")
    else: raise AssertionError(kind)


@pytest.mark.parametrize('kind',['missing','wrong_hash','invalid_json','oversized','unreadable','invalid_reference'])
def test_incomplete_inventory_preserves_all_candidates_and_sql(legacy,monkeypatch,kind):
    runtime,_,_,protected = legacy
    corrupt(legacy,kind,monkeypatch)
    changes = runtime._state.total_changes
    previous_gc = runtime._last_cas_gc_at
    def no_write(*args,**kwargs): pytest.fail('incomplete discovery must not start write admission or repair')
    with monkeypatch.context() as guard:
        guard.setattr(runtime,'_reserve_authoritative_write',no_write)
        guard.setattr(runtime,'_restore_canonical_blob',no_write)
        inventory = runtime._cas_reference_inventory()
        assert not inventory.complete and inventory.incomplete
        with pytest.raises(CasInventoryIncomplete): runtime.maintain_cas(force=True)
    assert all(path.exists() for path in protected)
    assert runtime._state.total_changes == changes
    assert runtime._last_cas_gc_at == previous_gc


@pytest.mark.parametrize('data',[b'{bad',b'\xff',b'{"x":1,"x":2}',b'{"nested":{"x":1,"x":2}}'])
def test_matching_hash_cannot_make_malformed_metadata_complete(legacy,data):
    runtime,_,_,protected = legacy
    ref = runtime.put_blob(data,media_type='application/json')
    runtime._state.execute('UPDATE sessions SET metadata_ref=?',(ref,))
    with pytest.raises(CasInventoryIncomplete): runtime.maintain_cas(force=True)
    assert all(path.exists() for path in protected)


def test_healthy_complete_discovery_still_collects_true_orphans(legacy):
    runtime,_,_,protected = legacy
    ref = runtime.put_blob(b'unowned fixture')
    digest = ref.removeprefix('sha256:')
    orphan = runtime.paths.cas_dir/digest[:2]/digest[2:]
    os.utime(orphan,(1,1))
    inventory = runtime._cas_reference_inventory()
    assert inventory.complete and not inventory.incomplete
    assert runtime.maintain_cas(force=True)['deleted'] == 1
    assert not orphan.exists() and all(path.exists() for path in protected)


@pytest.mark.parametrize('kind',['missing','wrong_hash','invalid_json','unreadable','invalid_reference'])
def test_health_reports_unknown_authoritative_metadata_as_blocked(legacy,monkeypatch,kind):
    runtime,_,_,_ = legacy
    corrupt(legacy,kind,monkeypatch)
    health = runtime.check_health(force_publish=True)
    assert health.level is StorageHealthLevel.BLOCKED
    assert any(reason.startswith(('cas_direct_metadata_unreadable:','cas_direct_reference_invalid:'))
               for reason in health.reasons)


def test_exact_metadata_restoration_reenables_collection_without_rebuilding_roots(legacy):
    runtime,_,metadata,protected = legacy
    original = metadata.read_bytes()
    metadata.write_bytes(b'{}')
    with pytest.raises(CasInventoryIncomplete): runtime.maintain_cas(force=True)
    metadata.write_bytes(original)  # Explicit test recovery of the exact original bytes.
    assert runtime._cas_reference_inventory().complete
    assert runtime.maintain_cas(force=True)['deleted'] == 0
    assert all(path.exists() for path in protected)


def test_inventory_result_is_immutable_and_unknown_is_not_an_empty_root_set(legacy):
    runtime,_,metadata,_ = legacy
    inventory = runtime._cas_reference_inventory()
    with pytest.raises(FrozenInstanceError): inventory.incomplete = frozenset({'unknown'})
    with pytest.raises(AttributeError): inventory.direct.add('x')
    metadata.unlink()
    unknown = runtime._cas_reference_inventory()
    assert unknown.direct and not unknown.complete
    with pytest.raises(CasInventoryIncomplete): unknown.require_complete()


def test_sql_scan_error_does_not_return_partial_roots_or_delete(legacy):
    from backend.core.storage.models import StorageCommitUnknown
    runtime,_,_,protected = legacy
    runtime._observability.quarantine()
    with pytest.raises(StorageCommitUnknown): runtime.maintain_cas(force=True)
    assert all(path.exists() for path in protected)


@pytest.mark.parametrize("owner", ["owned", "observability"])
def test_invalid_secondary_reference_cannot_authorize_collection(legacy, owner):
    runtime, _, _, protected = legacy
    if owner == "owned":
        runtime._state.execute(
            "INSERT INTO objects(digest,byte_length,media_type,created_at) "
            "VALUES ('bad',0,'fixture','fixture')"
        )
        runtime._state.execute(
            "INSERT INTO object_refs(owner_type,owner_id,field_name,digest,created_at) "
            "VALUES ('fixture','invalid-root','fixture','bad','fixture')"
        )
    else:
        runtime.append_trace_record('fixture', {'event': 'fixture'}, detail={"fixture": True})
        runtime._observability.execute(
            "UPDATE trace_events SET detail_ref='trace-object:bad'"
        )
        # The fixture must actually exercise the observability root query.
        assert runtime._observability.execute(
            "SELECT COUNT(*) FROM trace_events WHERE detail_ref='trace-object:bad'"
        ).fetchone()[0]
    inventory = runtime._cas_reference_inventory()
    assert not inventory.complete
    with pytest.raises(CasInventoryIncomplete):
        runtime.maintain_cas(force=True)
    assert all(path.exists() for path in protected)
