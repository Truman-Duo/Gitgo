from contextlib import contextmanager
from dataclasses import replace
import ctypes as C
import json
import os
from pathlib import Path
import uuid

import pytest

from backend.core.storage.models import StorageBlocked
from backend.core.storage.host_records import HOST_LEAF, HostAccount, HostAuthorityInvalid
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_scope import StorageOwnershipError
from backend.core.storage.native_windows_tree import StorageTreeError
from tests.test_storage_windows_tree import native, private_directory
from tests.test_storage_host_records import layout, input_copy


@contextmanager
def registry_case(native, temporary):
    scopes, trees, binaries = native
    journals = WindowsJournalLibrary(C.CDLL(str(binaries/"gitgo-storage-journal.dll")))
    registry = WindowsHostRegistry(scopes,trees,journals,layout())
    owner = registry.owner()
    sid,_ = owner._scope.account()
    root = temporary/"private"
    private_directory(root,sid)
    parent = owner._scope.root(str(root))
    identity = owner._scope.identity(parent)[0]
    # Internal object-relative protocol fixture; public bootstrap uses the real
    # token profile and is qualified separately in the disposable CI account.
    account = HostAccount(sid,str(temporary))
    owner._attempted = True
    try:
        yield registry,owner,account,root,parent,identity
    finally:
        for retained in list(registry._owners.values()): retained.close()
        assert not journals.retained_journals


def reopen(registry, account, root):
    owner = registry.owner()
    parent = owner._scope.root(str(root))
    expected = owner._scope.identity(parent)[0]
    target = owner._scope.open(HOST_LEAF,parent,directory=True)
    owner._attempted = True
    owner._verify(account,expected,target)
    return owner


def test_real_host_bootstrap_reservation_and_reopen_preserve_shared_capacity(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        anchor = owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        assert anchor.layout == registry.layout
        project = str(uuid.uuid4())
        first = owner.reserve_project(project,"a"*64)
        owner.require_ticket(first)
        assert first.slot==0
        with pytest.raises(HostAuthorityInvalid): owner.require_ticket(replace(first))
        owner.close()
        second = reopen(registry,account,root)
        other = second.reserve_project(str(uuid.uuid4()),"b"*64)
        assert other.slot==1
        second.close()
        full = reopen(registry,account,root)
        with pytest.raises(HostAuthorityInvalid,match="exhausted"):
            full.reserve_project(str(uuid.uuid4()),"c"*64)
        full.close()
        assert len(list((root/HOST_LEAF).glob("slot-*.json")))==3


def test_existing_project_debt_is_never_replayed_or_refunded(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        project = str(uuid.uuid4())
        owner.reserve_project(project,"a"*64)
        owner.close()
        second = reopen(registry,account,root)
        with pytest.raises(HostAuthorityInvalid,match="no replay"):
            second.reserve_project(project,"a"*64)
        second.close()
        assert (root/HOST_LEAF/"slot-0001.json").read_bytes()==bytes(8192)


def test_host_gate_excludes_another_owner_until_known_close(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        with pytest.raises(StorageOwnershipError) as caught:
            reopen(registry,account,root)
        assert caught.value.native_error==33
        for other in list(registry._owners.values()):
            if other is not owner: other.close()
        owner.close()
        reopened = reopen(registry,account,root)
        assert reopened.anchor.registry_id
        reopened.close()


@pytest.mark.parametrize("change", ["authority","envelope","intent","slot","runtime","extra"])
def test_changed_or_incomplete_registry_is_not_adopted(native,tmp_path_factory,change):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        owner.close()
        names={"authority":"authority.json","envelope":"envelope.json","intent":"bootstrap.intent",
               "slot":"slot-0000.json","runtime":"slot-0063.json","extra":"unowned.json"}
        path=root/HOST_LEAF/names[change]
        path.write_bytes(b"unexpected")
        with pytest.raises(StorageBlocked): reopen(registry,account,root)
        assert path.read_bytes()==b"unexpected"


def test_lost_native_constructor_return_keeps_child_in_parent_close_order(native,tmp_path_factory,monkeypatch):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        original=registry.trees._calls["tree_reserve"]
        def lose_return(*args):
            assert original(*args)==0
            raise RuntimeError("lost native reserve result")
        monkeypatch.setitem(registry.trees._calls,"tree_reserve",lose_return)
        with pytest.raises(RuntimeError,match="lost native reserve result"):
            owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        assert registry.trees.retained_trees==1
        owner.close()
        assert registry.trees.retained_trees==0 and not (root/HOST_LEAF).exists()


def test_unknown_journal_return_retains_committed_slot_and_issues_no_ticket(native,tmp_path_factory,monkeypatch):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        original=registry.journals._calls["update"]
        def lose_return(*args):
            assert original(*args)==0
            raise RuntimeError("lost reservation return")
        with monkeypatch.context() as context:
            context.setitem(registry.journals._calls,"update",lose_return)
            with pytest.raises(RuntimeError,match="lost reservation return"):
                owner.reserve_project(str(uuid.uuid4()),"a"*64)
        assert not owner._tickets and registry.journals.retained_journals==1
        owner.close()
        resumed=reopen(registry,account,root)
        ticket=resumed.reserve_project(str(uuid.uuid4()),"b"*64)
        assert ticket.slot==1
        resumed.close()


def test_unknown_child_close_keeps_host_gate_and_parent_owner(native,tmp_path_factory,monkeypatch):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        original=registry.trees._calls["tree_retire"]
        armed=[True]
        def lost_close(*args):
            result=original(*args)
            if armed[0]:
                armed[0]=False
                assert result==0
                return 31
            return result
        with monkeypatch.context() as context:
            context.setitem(registry.trees._calls,"tree_retire",lost_close)
            with pytest.raises(StorageTreeError): owner.close()
            assert id(owner) in registry._owners and not owner._scope.closed
            with pytest.raises(StorageOwnershipError) as caught: reopen(registry,account,root)
            assert caught.value.native_error==33
            for other in list(registry._owners.values()):
                if other is not owner: other.close()
        owner.close()
        reopened=reopen(registry,account,root)
        reopened.close()


def test_lost_bootstrap_write_preserves_partial_tree_without_adoption(native,tmp_path_factory,monkeypatch):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        original=registry.trees._calls["bootstrap_write_file"]
        def lose_return(*args):
            assert original(*args)==0
            raise RuntimeError("lost bootstrap write")
        with monkeypatch.context() as context:
            context.setitem(registry.trees._calls,"bootstrap_write_file",lose_return)
            with pytest.raises(RuntimeError,match="lost bootstrap write"):
                owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        owner.close()
        assert (root/HOST_LEAF/"bootstrap.intent").is_file()
        with pytest.raises((StorageBlocked,FileNotFoundError)): reopen(registry,account,root)
        assert not (root/HOST_LEAF/"authority.json").exists()


def test_foreign_thread_cannot_consume_ticket_or_release_parent(native,tmp_path_factory):
    import threading
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        ticket=owner.reserve_project(str(uuid.uuid4()),"a"*64)
        errors=[]
        def foreign():
            for action in (lambda:owner.require_ticket(ticket),owner.close):
                try: action()
                except HostAuthorityInvalid as error: errors.append(str(error))
        thread=threading.Thread(target=foreign)
        thread.start();thread.join(timeout=10)
        assert not thread.is_alive() and len(errors)==2 and not owner._scope.closed
        owner.require_ticket(ticket)
        owner.close()


def test_reserved_uuid_does_not_approve_an_unqualified_reference_file(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        project=str(uuid.uuid4())
        owner.reserve_project(project,"a"*64)
        owner.close()
        (root/HOST_LEAF/(project+".ref")).write_bytes(b"unqualified reference")
        with pytest.raises(HostAuthorityInvalid,match="project binding required"):
            reopen(registry,account,root)


def test_source_mismatch_is_refused_before_any_host_creation(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        source=replace(input_copy(registry.layout),manifest_digest="0"*64)
        with pytest.raises(HostAuthorityInvalid,match="complete content"):
            owner._bootstrap_at(account,parent,identity,source)
        assert not (root/HOST_LEAF).exists() and registry.trees.retained_trees==0


def test_second_pinned_input_copy_cannot_spend_the_first_copy_allowance(native,tmp_path_factory):
    from backend.core.storage.host_records import HostInput, HostLayout
    from backend.core.storage.runtime_manifest import with_runtime_copies
    from backend.core.storage.runtime_installation import PinnedRuntime
    from tests.test_storage_runtime_installation import installation
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        first=installation(registry.trees,account.sid,parent,identity,root)
        second=installation(registry.trees,account.sid,parent,identity,root)
        assert first.content==second.content and first.sha256!=second.sha256
        plan=registry.layout
        envelope=replace(plan.envelope,registry_files=tuple(item for item in plan.envelope.registry_files
                         if not item.name.startswith(("input_","runtime_"))))
        registry.layout=HostLayout(with_runtime_copies(envelope,first.content),first.content)
        owner._bootstrap_at(account,parent,identity,HostInput.from_manifest(first))
        source=PinnedRuntime(registry.trees,first.to_bytes(),first.sha256,account.sid).pin()
        other=PinnedRuntime(registry.trees,second.to_bytes(),second.sha256,account.sid).pin()
        try:
            owner.require_input(source)
            with pytest.raises(HostAuthorityInvalid,match="second input allowance"):
                owner.require_input(other)
            source.close()
            with pytest.raises(HostAuthorityInvalid,match="completely pinned"):
                owner.require_input(source)
        finally:
            source.close();other.close()
        owner.close()
        resumed=reopen(registry,account,root)
        assert resumed.anchor.source==HostInput.from_manifest(first)
        resumed.close()


def test_gate_handoff_ends_old_admission_without_releasing_other_pins(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        ticket=owner.reserve_project(str(uuid.uuid4()),"a"*64)
        owner.handoff_gate()
        assert not owner._scope.closed and registry.trees.retained_trees==1
        for action in (lambda:owner.require_ticket(ticket),lambda:owner._read_slot(63),owner.handoff_gate):
            with pytest.raises(HostAuthorityInvalid): action()
        observer=reopen(registry,account,root)
        assert owner.close()
        with pytest.raises(StorageOwnershipError) as caught: reopen(registry,account,root)
        assert caught.value.native_error==33
        for other in list(registry._owners.values()):
            if other is not observer: assert other.close()
        assert observer.close()


def test_lost_gate_release_never_restores_old_owner_admission(native,tmp_path_factory,monkeypatch):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        original=registry.scopes._calls["close"]
        def lost(*args):
            assert original(*args)==0
            raise RuntimeError("lost gate release")
        with monkeypatch.context() as context:
            context.setitem(registry.scopes._calls,"close",lost)
            with pytest.raises(RuntimeError,match="lost gate release"): owner.handoff_gate()
        assert not owner._ready and not owner._gate_held
        with pytest.raises(HostAuthorityInvalid): owner._read_slot(63)
        observer=reopen(registry,account,root)
        assert observer.close() and owner.close()


def test_busy_worker_retirement_preserves_gate_and_all_outer_resources(native,tmp_path_factory,monkeypatch):
    import time
    from backend.core.storage.native_windows_process import WindowsProcessLibrary,WindowsStorageProcess
    from tests.test_storage_windows_process import command
    processes=WindowsProcessLibrary(C.CDLL(str(native[2]/"gitgo-storage-process.dll")))
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        worker=owner._new(WindowsStorageProcess,processes)
        worker.create(command(root,"import sys; sys.stdin.buffer.read(1)"))
        worker.resume(worker.identity()[0])
        assert worker.status().active==1
        with monkeypatch.context() as context:
            context.setitem(processes._calls,"retire",lambda *args:170)
            assert owner.close() is False
        assert worker.status().active==1 and id(worker) in owner._resources
        assert not owner._scope.closed and registry.trees.retained_trees==1
        with pytest.raises(StorageOwnershipError) as caught: reopen(registry,account,root)
        assert caught.value.native_error==33
        for other in list(registry._owners.values()):
            if other is not owner: assert other.close()
        deadline=time.monotonic()+15
        while not owner.close():
            assert time.monotonic()<deadline
            time.sleep(.01)
        assert processes.retained_processes==0 and registry.trees.retained_trees==0


def test_process_constructor_loss_remains_in_host_close_order(native,tmp_path_factory,monkeypatch):
    from backend.core.storage.native_windows_process import WindowsProcessLibrary,WindowsStorageProcess
    processes=WindowsProcessLibrary(C.CDLL(str(native[2]/"gitgo-storage-process.dll")))
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        original=processes._calls["reserve"]
        def lost(*args):
            assert original(*args)==0
            raise RuntimeError("lost process reserve")
        monkeypatch.setitem(processes._calls,"reserve",lost)
        with pytest.raises(RuntimeError,match="lost process reserve"):
            owner._new(WindowsStorageProcess,processes)
        assert processes.retained_processes==1
        assert owner.close() and processes.retained_processes==0


@pytest.mark.parametrize("failure",["reserve","close"])
def test_pinned_wrapper_keeps_its_native_leaf_inside_host_lifetime(native,tmp_path_factory,monkeypatch,failure):
    from backend.core.storage.runtime_installation import PinnedRuntime
    from tests.test_storage_runtime_installation import installation
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        manifest=installation(registry.trees,account.sid,parent,identity,root)
        owner._bootstrap_at(account,parent,identity,input_copy(registry.layout))
        operation="tree_reserve" if failure=="reserve" else "tree_retire"
        original=registry.trees._calls[operation]
        def lost(*args):
            assert original(*args)==0
            raise RuntimeError("lost wrapped native effect")
        if failure=="close":
            wrapper=owner._new(PinnedRuntime,registry.trees,manifest.to_bytes(),manifest.sha256,account.sid)
            wrapper.pin()
        with monkeypatch.context() as context:
            context.setitem(registry.trees._calls,operation,lost)
            with pytest.raises(RuntimeError,match="lost wrapped native effect"):
                if failure=="reserve":
                    owner._new(PinnedRuntime,registry.trees,manifest.to_bytes(),manifest.sha256,account.sid)
                else:
                    owner.close()
        assert registry.trees.retained_trees==2 and not owner._scope.closed
        with pytest.raises(StorageOwnershipError) as caught: reopen(registry,account,root)
        assert caught.value.native_error==33
        for other in list(registry._owners.values()):
            if other is not owner: assert other.close()
        assert owner.close() and registry.trees.retained_trees==0


def test_native_input_ancestor_is_rejected_before_host_parent_creation(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        profile=owner._scope.root(str(tmp_path_factory))
        excluded=owner._scope.identity(profile)[0]
        with pytest.raises(StorageOwnershipError) as caught:
            owner._parent(account,create=True,exclude=excluded)
        assert caught.value.operation=="excluded_ancestor"
        assert not (tmp_path_factory/".gitgo").exists()


def test_actual_input_alias_of_existing_host_parent_is_rejected_before_authority_creation(native,tmp_path_factory):
    with registry_case(native,tmp_path_factory) as (registry,owner,account,root,parent,identity):
        account=HostAccount(account.sid,str(root))
        path=root/".gitgo"
        private_directory(path,account.sid)
        handle=owner._scope.root(str(path))
        excluded=owner._scope.identity(handle)[0]
        with pytest.raises(HostAuthorityInvalid,match="aliases the Host parent"):
            owner._parent(account,create=True,exclude=excluded)
        assert not (path/HOST_LEAF).exists()
