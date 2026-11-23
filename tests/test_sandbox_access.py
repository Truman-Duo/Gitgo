"""Actual ordinary-token DACL/MIC updates; OS process isolation tested separately."""
from pathlib import Path
import json
import os
import ctypes as C
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
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


def test_shallow_read_access_records_only_selected_depth_and_can_revoke(access_tree, monkeypatch):
    stage, runtime, private = access_tree
    for index in range(20):
        (runtime / f'library-{index:02d}.py').write_text('data = None', encoding='utf-8')

    def unexpected_prefetch(*_):
        pytest.fail('Scoped reads should keep the sequential open path')

    monkeypatch.setattr(AccessTree, '_prefetch', unexpected_prefetch)
    target = stage.parent / 'target'
    target.mkdir()
    (target / 'sub').mkdir()
    (target / 'direct.txt').write_text('direct', encoding='utf-8')
    nested = target / 'sub' / 'nested.txt'
    nested.write_text('nested', encoding='utf-8')
    record = private / 'shallow.json'
    access = WorkspaceAccess(stage, [runtime], record, read_roots=[target], read_workspace=target, read_max_depth=1)
    try:
        access.prepare()
        payload = json.loads(record.read_text(encoding='utf-8'))
        selected = {Path(row[0]) for row in payload['records']}
        assert payload['read_max_depth'] == 1
        assert (target / 'direct.txt') in selected and nested not in selected
        revoke_record(record)
        assert json.loads(record.read_text(encoding='utf-8'))['phase'] == 'revoked'
    finally:
        if record.exists() and json.loads(record.read_text(encoding='utf-8'))['phase'] != 'revoked':
            access.close()


def test_owner_prepare_revoke_preserves_peer_grants_and_new_files(access_tree):
    w,r,p=access_tree
    for index in range(20):
        (r / f'library-{index:02d}.py').write_text('data = None', encoding='utf-8')
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


def test_failed_runtime_prefetch_retires_successful_siblings(access_tree, monkeypatch):
    workspace, runtime, private = access_tree
    for index in range(20):
        (runtime / f'library-{index:02d}.py').write_text('data = None', encoding='utf-8')
    first_opened = Event()
    handles = []
    open_file = AccessTree._open
    caller = get_ident()

    def fail_one_open(tree, path, access):
        candidate = Path(path)
        if candidate.name == 'library-01.py':
            assert get_ident() != caller
            assert first_opened.wait(5)
            raise OSError('Runtime file unavailable')
        handle = open_file(tree, path, access)
        handles.append(handle)
        if candidate.name == 'library-00.py':
            first_opened.set()
        return handle

    monkeypatch.setattr(AccessTree, '_open', fail_one_open)
    record = private / 'failed-open.json'
    access = WorkspaceAccess(workspace, [runtime], record, reuse_runtime_pins=True)
    with pytest.raises(OSError, match='Runtime file unavailable'):
        access.prepare()
    access.close()
    assert first_opened.is_set() and all(handle.closed for handle in handles)
    assert not record.exists() and access.payload is None


def test_runtime_prefetch_stays_on_caller_thread_if_token_is_unconfirmed(access_tree, monkeypatch):
    workspace, runtime, private = access_tree
    for index in range(20):
        (runtime / f'library-{index:02d}.py').write_text('data = None', encoding='utf-8')
    monkeypatch.setattr('backend.core.windows_access.uses_process_token', lambda *_: False)
    open_file = AccessTree._open
    caller = get_ident()

    def check_thread(tree, path, access):
        assert get_ident() == caller
        return open_file(tree, path, access)

    monkeypatch.setattr(AccessTree, '_open', check_thread)
    access = WorkspaceAccess(workspace, [runtime], private / 'thread-token.json')
    try:
        access.prepare()
    finally:
        access.close()
    assert access.payload['phase'] == 'revoked'


@pytest.mark.parametrize('new_runtime_file', [False, True])
def test_short_scope_reuses_only_runtime_pins_and_revalidates_live_inventory(access_tree, new_runtime_file):
    workspace, runtime, private = access_tree
    access = WorkspaceAccess(workspace, [runtime], private / 'retained.json',
                             reuse_runtime_pins=True).prepare()
    try:
        retained, count = access._runtime_pins
        assert count > 0
        assert all(Path(path).is_relative_to(runtime) for path in retained.handles)
        (workspace / 'input.txt').rename(workspace / 'renamed.txt')
        if new_runtime_file:
            (runtime / 'new.txt').write_text('added during invocation')
    finally:
        access.close()
    assert access._runtime_pins is None
    assert access.payload['phase'] == 'revoked'
    assert bool(access.timings.get('refreshed_runtime_inventory')) is new_runtime_file
    assert bool(access.timings['reused_runtime_pins']) is not new_runtime_file
    with AccessTree([workspace, runtime], writable=False) as current:
        assert all(access.payload['principal'] not in row['sddl'] for row in current.snapshot())


def test_concurrent_runtime_pin_budget_falls_back_and_returns_capacity(access_tree, monkeypatch):
    from backend.core import sandbox_access

    workspace, runtime, private = access_tree
    other = workspace.with_name('other')
    other.mkdir()
    first = WorkspaceAccess(workspace, [runtime], private / 'first-pins.json', reuse_runtime_pins=True)
    second = WorkspaceAccess(other, [runtime], private / 'second-pins.json', reuse_runtime_pins=True)
    try:
        first.prepare()
        count = first._runtime_pins[1]
        monkeypatch.setattr(sandbox_access, '_RUNTIME_PIN_LIMIT', count)
        second.prepare()
        assert second._runtime_pins is None
        first.close()
        peer = second.payload['principal']
        with AccessTree([runtime], writable=False) as tree:
            assert all(peer in row['sddl'] for row in tree.snapshot())
    finally:
        try:
            second.close()
        finally:
            first.close()
    assert sandbox_access._runtime_pin_count == 0
    assert first.timings['reused_runtime_pins'] == count
    assert second.timings['reused_runtime_pins'] == 0
    assert first.payload['phase'] == second.payload['phase'] == 'revoked'


def test_pinned_native_image_hash_accepts_acl_only_change(access_tree, monkeypatch):
    from contextlib import ExitStack
    import hashlib
    from backend.core.native_sandbox import _hash, _pin_windows, get_native

    get_native()
    workspace, runtime, private = access_tree
    image = runtime / 'code.txt'
    data = image.read_bytes()
    access = WorkspaceAccess(workspace, [runtime], private / 'image-access.json')
    before = after = None
    read = os.read
    with ExitStack() as pins:
        fd, _ = _pin_windows(image, pins)

        def read_with_acl_update(descriptor, limit):
            nonlocal before, after
            content = read(descriptor, limit)
            if descriptor == fd and before is None:
                before = os.fstat(fd)
                access.prepare()
                after = os.fstat(fd)
            return content

        try:
            with monkeypatch.context() as patch:
                patch.setattr(os, 'read', read_with_acl_update)
                assert _hash(fd) == (hashlib.sha256(data).hexdigest(), len(data))
            assert before.st_ctime_ns != after.st_ctime_ns
            assert before.st_mtime_ns == after.st_mtime_ns
        finally:
            access.close()


def test_existing_journal_never_owns_or_revokes_foreign_access(access_tree):
    w,r,p=access_tree
    record=p/'collision.json';record.write_text('existing evidence')
    access=WorkspaceAccess(w,[r],record)
    with pytest.raises(FileExistsError):access.prepare()
    assert access.payload is None
    access.close()
    assert record.read_text()=='existing evidence'


@pytest.mark.parametrize('recovery_version', [1, 2])
def test_scoped_access_journal_preserves_large_repeated_descriptors(access_tree, monkeypatch, recovery_version):
    from backend.core import sandbox_access_records

    workspace, runtime, private = access_tree
    for index in range(120):
        (runtime / f'library-{index}.py').write_text('fixture', encoding='utf8')
    roots = sorted([workspace, runtime])
    with AccessTree(roots, writable=False) as tree:
        original = tree.snapshot()
    expanded_bytes = len(json.dumps({'records': original, 'planned': original},
                                   separators=(',', ':')).encode('utf8'))
    journal = private / 'compact.json'

    access = WorkspaceAccess(workspace, [runtime], journal)
    try:
        with monkeypatch.context() as options:
            options.setattr(sandbox_access_records, 'MAX_RECORD_BYTES', expanded_bytes // 2)
            access.prepare()
            payload = sandbox_access_records.load_scoped_access_record(journal)
            assert journal.stat().st_size < expanded_bytes // 2

        def unpack(rows):
            return [dict(path=path, device=device, inode=inode,
                         descriptor=payload['descriptors'][index][0], sddl=payload['descriptors'][index][1])
                    for path, device, inode, index in rows]

        assert payload['version'] == 2
        assert unpack(payload['records']) == original
        with AccessTree(roots, writable=False) as tree:
            planned = unpack(payload['planned'])
            live = tree.snapshot()
            assert tree.identity(planned) == tree.identity(live)
            assert [tree.record_descriptor(row).comparison_key() for row in planned] == [
                tree.record_descriptor(row).comparison_key() for row in live]
        if recovery_version == 1:
            for key in ('records', 'planned', 'boundaries'):
                payload[key] = unpack(payload[key])
            payload['version'] = 1
            payload.pop('descriptors')
            sandbox_access_records.save_record(journal, payload)
        revoke_record(journal)
    except BaseException:
        access.close()
        raise
    assert sandbox_access_records.load_scoped_access_record(journal)['phase'] == 'revoked'
    with AccessTree(roots, writable=False) as tree:
        assert all(access.payload['principal'] not in row['sddl'] for row in tree.snapshot())


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


@pytest.mark.parametrize('retiring', [False, True])
def test_pending_native_cleanup_cannot_revoke_access(access_tree, monkeypatch, retiring):
    w,r,p=access_tree
    access=WorkspaceAccess(w,[r],p/'pending.json').prepare()
    with monkeypatch.context() as changed:
        job = SimpleNamespace(profile_name=access.policy.profile_name, retirement_started=retiring)
        changed.setattr('backend.core.windows_sandbox_native.PENDING_JOBS', {1: job})
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


def test_managed_worktrees_prepare_in_parallel_and_release_in_lease_order(access_tree, monkeypatch):
    from backend.core import sandbox_access

    first, second, private = access_tree
    targets = {'first': first, 'second': second}
    storage = SimpleNamespace(paths=SimpleNamespace(project_root=private))
    manager = SimpleNamespace(storage=storage, _safe_id=lambda value: value, _target_path=targets.__getitem__)
    first_entered, allow_first, releasing = Event(), Event(), Event()
    retired = []

    class Access:
        def __init__(self, workspace, *_args, **_kwargs):
            self.workspace = workspace
            self.prepared = False

        def prepare(self):
            if self.workspace == first:
                first_entered.set()
                assert allow_first.wait(10)
            self.prepared = True

        def close(self):
            assert self.prepared
            retired.append(self.workspace)

    monkeypatch.setattr(sandbox_access, 'WorkspaceAccess', Access)
    monkeypatch.setattr(sandbox_access, 'native_runtime_access', lambda: ([], []))
    service = WorktreeAccessService(manager)
    leases = {name: SimpleNamespace(process_id=name, path=path, isolated=True)
              for name, path in targets.items()}

    def release_first():
        releasing.set()
        service.release('first')

    with ThreadPoolExecutor(3) as pool:
        preparation = pool.submit(service.prepare, leases['first'])
        try:
            assert first_entered.wait(5)
            release = pool.submit(release_first)
            assert releasing.wait(5)
            pool.submit(service.prepare, leases['second']).result(5)
            service.release('second')
            assert not release.done()
        finally:
            allow_first.set()
        preparation.result(5)
        release.result(5)

    assert retired == [second, first]
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
