"""Actual ordinary-token DACL/MIC updates; OS process isolation tested separately."""
from pathlib import Path
import json
import os
import ctypes as C
from types import SimpleNamespace

import pytest
from backend.core.sandbox_access import WorkspaceAccess, WorktreeAccessService, revoke_record
from backend.core.windows_access import AccessTree
from backend.core.windows_acl import SecurityConflict

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows access configuration')


@pytest.fixture
def access_tree(tmp_path_factory, monkeypatch):
    workspace, runtime, private = [tmp_path_factory / n for n in ('workspace','runtime','private')]
    for path in (workspace,runtime,private): path.mkdir()
    (workspace/'input.txt').write_text('input')
    (runtime/'code.txt').write_text('code')
    # This fixture exercises the ACL service in place, not a native tool
    # launch. Real API tests use a physically separate dedicated runtime.
    monkeypatch.setattr('backend.core.sandbox.trusted_runtime_roots', lambda: (runtime,))
    return workspace,runtime,private


def test_owner_prepare_revoke_preserves_peer_grants_and_new_files(access_tree):
    w,r,p=access_tree
    first=WorkspaceAccess(w,[r],p/'first.json').prepare()
    other=w.parent/'other';other.mkdir();(other/'input.txt').write_text('peer')
    second=WorkspaceAccess(other,[r],p/'second.json').prepare()
    (w/'new.txt').write_text('created after preparation')
    with AccessTree([r],writable=False) as tree:
        peer=second.payload['principal']
        before=tree.snapshot()
        assert all(peer in item['sddl'] for item in before)
    first.close()
    with AccessTree([w,r],writable=False) as tree:
        current=tree.snapshot()
        assert all(first.payload['principal'] not in item['sddl'] for item in current)
        assert all(peer in item['sddl'] for item in current if Path(item['path']).is_relative_to(r))
    second.close()
    assert json.loads((p/'first.json').read_text())['phase']=='revoked'
    assert 'records' not in json.loads((p/'first.json').read_text())
    revoke_record(p/'first.json')  # Confirmed revocation is idempotent.


def test_existing_journal_never_owns_or_revokes_foreign_access(access_tree):
    w,r,p=access_tree
    record=p/'collision.json';record.write_text('existing evidence')
    access=WorkspaceAccess(w,[r],record)
    with pytest.raises(FileExistsError):access.prepare()
    assert access.payload is None
    access.close()
    assert record.read_text()=='existing evidence'


def test_prepare_failure_retains_evidence_and_recovery_revokes_partial_grants(access_tree, monkeypatch):
    w,r,p=access_tree
    write=AccessTree._write_descriptor
    count=[]
    def fail_after_write(tree, handle, raw):
        write(tree,handle,raw)
        count.append(handle)
        raise OSError('write acknowledgement lost')
    access=WorkspaceAccess(w,[r],p/'failure.json')
    with monkeypatch.context() as changed:
        changed.setattr(AccessTree,'_write_descriptor',fail_after_write)
        with pytest.raises(OSError,match='acknowledgement'):access.prepare()
    assert count and access.payload['phase']=='prepare_failed'
    revoke_record(p/'failure.json')
    assert json.loads((p/'failure.json').read_text())['phase']=='revoked'


def test_pending_native_cleanup_cannot_revoke_access(access_tree, monkeypatch):
    w,r,p=access_tree
    access=WorkspaceAccess(w,[r],p/'pending.json').prepare()
    with monkeypatch.context() as changed:
        changed.setattr('backend.core.sandbox_windows._PENDING_JOBS',{1:object()})
        with pytest.raises(Exception,match='not retired'):access.close()
        assert access.payload['phase']=='applied'
    access.close()


def test_managed_service_refuses_a_path_outside_its_lease(access_tree):
    w,r,p=access_tree
    manager=SimpleNamespace(_target_path=lambda _:w,_safe_id=lambda x:x)
    service=WorktreeAccessService(manager)
    lease=SimpleNamespace(process_id='child',path=str(r),isolated=True)
    with pytest.raises(Exception,match='managed child'):service.prepare(lease)
    assert not service.active


def test_access_tree_rejects_reparse_and_hardlink_before_mutation(access_tree):
    w,r,p=access_tree
    os.link(w/'input.txt',w/'alias.txt')
    with pytest.raises(OSError,match='multiply-linked'):AccessTree([w])
    assert not list(p.iterdir())


def test_source_code_root_is_not_recursively_exposed(access_tree):
    w,r,p=access_tree
    code=w.parent/"checkout";backend=code/"backend";secret=code/"private"
    backend.mkdir(parents=True);secret.mkdir()
    (backend/"module.py").write_text("pass")
    (secret/"credential.txt").write_text("SYNTHETIC")
    with AccessTree([secret],writable=False) as tree:before=tree.snapshot()
    access=WorkspaceAccess(w,[r,backend],p/"scoped.json",shallow_runtimes=[code]).prepare()
    with AccessTree([secret],writable=False) as tree:
        assert tree.snapshot()==before
    (code/"new-private.txt").write_text("SYNTHETIC")
    with AccessTree([code],writable=False,shallow=[code]) as tree:
        root=tree.record_descriptor(tree.snapshot()[0])
        from backend.core.windows_acl_plan import _entries,_profile_sid
        sid=_profile_sid(access.payload['principal'])
        assert all(ace[1]==0 for ace in _entries(root.dacl) if sid in ace)
    access.close()


@pytest.mark.parametrize("apply", [False, True])
def test_operator_current_runtime_reuses_child_layout_with_shallow_source_root(tmp_path_factory, monkeypatch, apply):
    import sys
    import scripts.provision_sandbox as operator
    import backend.core.sandbox_access as service
    workspace=tmp_path_factory/'workspace';workspace.mkdir()
    runtime=tmp_path_factory/'runtime';runtime.mkdir()
    source=tmp_path_factory/'code';source.mkdir()
    record=tmp_path_factory/'private.json'
    seen=[]
    class Access:
        def __init__(self, w, roots, r, *, shallow_runtimes=()):
            seen.append((w, roots, r, shallow_runtimes))
            self.policy=SimpleNamespace(workspace=w)
            self.runtimes=roots;self.shallow_runtimes=shallow_runtimes
        def prepare(self):seen.append('prepared')
    monkeypatch.setattr(service,'native_runtime_access',lambda:([runtime],[source]))
    monkeypatch.setattr(service,'WorkspaceAccess',Access)
    args=['provision_sandbox.py','--access-only','--current-runtime','--workspace',str(workspace),'--record',str(record)]
    if apply:args.append('--apply')
    monkeypatch.setattr(sys,'argv',args)
    operator.main()
    assert seen[0]==(workspace,[runtime],record,[source])
    assert ('prepared' in seen)==apply
    assert not record.exists()


@pytest.mark.parametrize("recover", [False, True])
def test_metadata_parent_grant_has_no_data_list_write_or_inheritance(tmp_path_factory, monkeypatch, recover):
    import struct
    from backend.core.windows_acl_plan import _entries, _profile_sid
    base=tmp_path_factory
    parent=base/'candidates';parent.mkdir()
    w=parent/'work';w.mkdir();(w/'input.txt').write_text('input')
    peer=parent/'peer';peer.mkdir();(peer/'secret.txt').write_text('synthetic')
    r=base/'runtime';r.mkdir();(r/'code.txt').write_text('code')
    monkeypatch.setattr('backend.core.sandbox.trusted_runtime_roots', lambda:(r,))
    with AccessTree([peer],writable=False) as tree:
        before=tree.snapshot()
    access=WorkspaceAccess(w,[r],base/'access.json',metadata_roots=[parent]).prepare()
    try:
        with AccessTree([parent],writable=False,shallow=[parent]) as tree:
            descriptor=tree.record_descriptor(tree.snapshot()[0])
            sid=_profile_sid(access.payload['principal'])
            entries=[a for a in _entries(descriptor.dacl) if sid in a]
            assert len(entries)==1 and entries[0][1]==0
            assert struct.unpack_from('<I',entries[0],4)[0]==0x1000A0
        with AccessTree([peer],writable=False) as tree:
            assert tree.snapshot()==before
    finally:
        if recover:
            revoke_record(base/'access.json')
        else:
            access.close()
    assert json.loads((base/'access.json').read_text())['phase']=='revoked'


def test_metadata_scope_cannot_name_an_unrelated_directory(access_tree):
    w,r,p=access_tree
    with pytest.raises(Exception,match='immediate execution parent'):
        WorkspaceAccess(w,[r],p/'invalid.json',metadata_roots=[r])
    assert not list(p.iterdir())
