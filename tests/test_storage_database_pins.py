from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from dataclasses import replace
import os
from pathlib import Path
import uuid

import pytest

from backend.core.storage import native_windows_tree as n
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from tests.test_storage_windows_tree import native, private_directory

def fixture(native,temporary,extra=""):
    scopes,library,_=native
    scope=scopes.open_scope();sid,_=scope.account()
    root=temporary/"private";private_directory(root,sid,extra)
    parent=scope.root(str(root));expected=scope.identity(parent)[0]
    return scope,sid,root,parent,expected,library

def cleanup(scope,library):
    for owner in list(library._owners.values()):owner.close()
    scope.close()
    assert not library.retained_trees


def test_database_pin_allows_mutation_but_holds_file_identity_and_context(native,tmp_path_factory):
    scope,sid,root,parent,parent_id,library=fixture(native,tmp_path_factory)
    try:
        path=root/"state.sqlite3";path.write_bytes(b"")
        handle=scope.open(path.name,parent);expected=scope.identity(handle)[0];scope.close_handle(handle)
        pins=library.reserve(n.TreePurpose.DATA,sid);pins.root(parent,parent_id)
        assert pins.inventory(0)==("state.sqlite3",)
        assert pins.inspect_sidecar(0,"state.sqlite3-wal",4096) is None
        index=pins.add(0,path.name,expected,ceiling=4096)
        path.write_bytes(b"mutable SQLite bytes")
        assert pins.identity(index)==expected
        for action in (path.unlink,lambda:path.rename(root/"moved")):
            with pytest.raises(PermissionError):action()
        with ThreadPoolExecutor(max_workers=1) as pool:
            for action in (pins.close,lambda:pins.release(index),lambda:pins.identity(index)):
                with pytest.raises(n.StorageTreeError,match="foreign_context"):pool.submit(action).result()
        assert pins.identity(index)==expected
        with pytest.raises(ValueError):pins.read(index,0,1)
        pins.release(index)
        with pytest.raises(n.StorageTreeError):pins.identity(index)
        path.unlink()
    finally:cleanup(scope,library)


@pytest.mark.parametrize("case",["wrong_identity","hardlink","oversized"])
def test_database_pin_rejects_unknown_generation_and_metadata(native,tmp_path_factory,case):
    scope,sid,root,parent,parent_id,library=fixture(native,tmp_path_factory)
    try:
        path=root/"state.sqlite3";path.write_bytes(b"data")
        handle=scope.open(path.name,parent);expected=scope.identity(handle)[0];scope.close_handle(handle)
        if case=="wrong_identity":expected=replace(expected,file_id=expected.file_id^1)
        if case=="hardlink":os.link(path,root/"alias")
        pins=library.reserve(n.TreePurpose.DATA,sid);pins.root(parent,parent_id)
        with pytest.raises(n.StorageTreeError):pins.add(0,path.name,expected,ceiling=1 if case=="oversized" else 4096)
        assert library.retained_trees==1
    finally:cleanup(scope,library)


def test_database_pin_rejects_permissive_parent_acl(native,tmp_path_factory):
    scope,sid,root,parent,parent_id,library=fixture(native,tmp_path_factory,"(A;OICI;FA;;;WD)")
    try:
        pins=library.reserve(n.TreePurpose.DATA,sid)
        with pytest.raises(n.StorageTreeError):pins.root(parent,parent_id)
    finally:cleanup(scope,library)


def test_database_pin_rejects_permissive_member_acl(native,tmp_path_factory):
    scope,sid,root,parent,parent_id,library=fixture(native,tmp_path_factory)
    try:
        kernel,advapi=C.WinDLL("kernel32",use_last_error=True),C.WinDLL("advapi32",use_last_error=True)
        class Attributes(C.Structure):
            _fields_=[("size",C.c_ulong),("descriptor",C.c_void_p),("inherit",C.c_int)]
        parse=advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        parse.argtypes=[C.c_wchar_p,C.c_ulong,C.POINTER(C.c_void_p),C.c_void_p];parse.restype=C.c_int
        create=kernel.CreateFileW
        create.argtypes=[C.c_wchar_p,C.c_uint32,C.c_uint32,C.POINTER(Attributes),C.c_uint32,C.c_uint32,C.c_void_p]
        create.restype=C.c_void_p
        kernel.CloseHandle.argtypes=[C.c_void_p];kernel.CloseHandle.restype=C.c_int
        kernel.LocalFree.argtypes=[C.c_void_p];kernel.LocalFree.restype=C.c_void_p
        descriptor=C.c_void_p()
        assert parse(f"D:P(A;;FA;;;{sid})(A;;FA;;;WD)",1,C.byref(descriptor),None)
        try:
            attributes=Attributes(C.sizeof(Attributes),descriptor,False)
            file=create(str(root/"state.sqlite3"),0x80000000,3,C.byref(attributes),1,0x80,None)
            assert file not in (None,C.c_void_p(-1).value)
            assert kernel.CloseHandle(file)
        finally:assert not kernel.LocalFree(descriptor)
        handle=scope.open("state.sqlite3",parent);expected=scope.identity(handle)[0];scope.close_handle(handle)
        pins=library.reserve(n.TreePurpose.DATA,sid);pins.root(parent,parent_id)
        with pytest.raises(n.StorageTreeError):pins.add(0,"state.sqlite3",expected,ceiling=4096)
    finally:cleanup(scope,library)


def test_sidecar_probe_closes_without_releasing_the_parent(native,tmp_path_factory):
    scope,sid,root,parent,parent_id,library=fixture(native,tmp_path_factory)
    try:
        path=root/"state.sqlite3-wal";path.write_bytes(b"tail")
        pins=library.reserve(n.TreePurpose.DATA,sid);pins.root(parent,parent_id)
        index=pins.inspect_sidecar(0,path.name,4096)
        with pytest.raises(PermissionError):path.unlink()
        pins.release(index);path.unlink()
        assert pins.inspect_sidecar(0,path.name,4096) is None
        with pytest.raises(ValueError):pins.inspect_sidecar(0,"arbitrary",4096)
        with pytest.raises(ValueError):pins.release(0)
    finally:cleanup(scope,library)


@pytest.mark.parametrize("operation",["data_add","data_release"])
def test_lost_python_native_response_retains_exact_data_owner(native,tmp_path_factory,operation,monkeypatch):
    scope,sid,root,parent,parent_id,library=fixture(native,tmp_path_factory)
    try:
        path=root/"state.sqlite3-wal";path.write_bytes(b"tail")
        pins=library.reserve(n.TreePurpose.DATA,sid);pins.root(parent,parent_id)
        index=pins.inspect_sidecar(0,path.name,4096) if operation=="data_release" else None
        original=library._calls[operation];count=[]
        def lost(*args):
            assert original(*args)==0
            count.append(True)
            raise RuntimeError("native result lost")
        with monkeypatch.context() as patch:
            patch.setitem(library._calls,operation,lost)
            with pytest.raises(RuntimeError,match="native result lost"):
                if operation=="data_add":pins.inspect_sidecar(0,path.name,4096)
                else:pins.release(index)
            with pytest.raises(n.StorageTreeError):pins.inventory(0)
            assert pins._sealed and library.retained_trees==1 and len(count)==1
        if operation=="data_add":
            with pytest.raises(PermissionError):path.unlink()
        pins.close();path.unlink()
        assert library.retained_trees==0
    finally:cleanup(scope,library)


def test_native_data_after_effect_faults_keep_unknown_children(native,tmp_path_factory):
    import subprocess
    scope,sid,root,_,_,library=fixture(native,tmp_path_factory)
    try:
        result=subprocess.run([str(native[2]/"storage-tree-data-faults.exe"),str(root),sid],
                              capture_output=True,text=True,timeout=25)
        assert result.returncode==0,result.stdout+result.stderr
        assert "quarantined=1" in result.stdout
    finally:cleanup(scope,library)
