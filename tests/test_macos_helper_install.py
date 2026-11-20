"""Installer inventory and entry guards; system install still needs acceptance."""
import os
import sys
import pytest
if sys.platform=='win32': pytest.skip('Darwin helper',allow_module_level=True)
from backend.core import macos_helper_install as helper


def bundle(root):
    root.mkdir();(root/'gitgo-host').write_bytes(b'fixture');(root/'gitgo-host').chmod(0o755)
    (root/'_internal').mkdir();(root/'_internal/data').write_bytes(b'fixed')
    return root


@pytest.mark.parametrize('fault',['external_link','absolute_link','hardlink','fifo'])
def test_install_inventory_rejects_external_or_special_files(tmp_path_factory,fault):
    root=bundle(tmp_path_factory/'bundle')
    outside=tmp_path_factory/'outside';outside.write_bytes(b'private')
    path=root/'_internal/unsafe'
    if fault=='external_link': path.symlink_to('../../outside')
    elif fault=='absolute_link': path.symlink_to(outside)
    elif fault=='hardlink': os.link(outside,path)
    else: os.mkfifo(path)
    with pytest.raises(ValueError): helper.inventory(root)


def test_inventory_pins_contents_permissions_and_internal_framework_links(tmp_path_factory):
    root=bundle(tmp_path_factory/'bundle');(root/'alias').symlink_to('_internal/data')
    original=helper.inventory(root);digest=helper.identity(original)
    assert helper.identity(helper.inventory(root))==digest
    (root/'_internal/data').write_bytes(b'tampered')
    assert helper.identity(helper.inventory(root))!=digest
    (root/'_internal/data').write_bytes(b'fixed');(root/'_internal/data').chmod(0o755)
    assert helper.identity(helper.inventory(root))!=digest


def test_privileged_roles_refuse_source_or_ordinary_user_runtime():
    with pytest.raises(PermissionError): helper.verify_installed_runtime()


def test_install_requires_explicit_local_build_authorization(monkeypatch,tmp_path_factory):
    monkeypatch.setattr(sys,'platform','darwin')
    monkeypatch.setattr(os,'getuid',lambda:0);monkeypatch.setattr(os,'geteuid',lambda:0)
    with pytest.raises(ValueError,match='Publisher trust'): helper.install(tmp_path_factory)


def test_root_trust_refuses_user_writable_ancestors(tmp_path_factory):
    with pytest.raises(ValueError): helper.trusted_directory(tmp_path_factory)


@pytest.mark.parametrize('fault',['root_link','host_link','host_not_executable','dangling','cycle','file_parent'])
def test_inventory_rejects_ambiguous_roots_entries_and_links(tmp_path_factory,fault):
    root=bundle(tmp_path_factory/'bundle')
    if fault=='root_link':
        alias=tmp_path_factory/'alias';alias.symlink_to(root);root=alias
    elif fault=='host_link':
        (root/'gitgo-host').rename(root/'other');(root/'gitgo-host').symlink_to('other')
    elif fault=='host_not_executable': (root/'gitgo-host').chmod(0o644)
    elif fault=='dangling': (root/'bad').symlink_to('missing')
    elif fault=='cycle':
        (root/'a').symlink_to('b');(root/'b').symlink_to('a')
    else: (root/'bad').symlink_to('_internal/data/../data')
    with pytest.raises((ValueError,OSError)): helper.inventory(root)


def test_framework_symlink_chains_preserve_existing_inventory_identity(tmp_path_factory):
    root=bundle(tmp_path_factory/'bundle')
    versions=root/'Python.framework/Versions';(versions/'A').mkdir(parents=True)
    (versions/'A/Python').write_bytes(b'framework')
    (versions/'Current').symlink_to('A')
    (root/'Python.framework/Python').symlink_to('Versions/Current/Python')
    (root/'_internal/framework').symlink_to('../Python.framework/Python')
    entries=helper.inventory(root)
    assert [entry for entry in entries if entry[1]=='link']==[
        ['Python.framework/Python','link','Versions/Current/Python'],
        ['Python.framework/Versions/Current','link','A'],
        ['_internal/framework','link','../Python.framework/Python']]


@pytest.mark.parametrize('fault',['grow','shrink','overwrite','replace'])
def test_inventory_refuses_changes_while_hashing_with_bounded_reads(tmp_path_factory,monkeypatch,fault):
    root=bundle(tmp_path_factory/'bundle');target=root/'_internal/data'
    before=target.stat();read=os.read;changed=False;bytes_read=0
    def racing_read(fd,size):
        nonlocal changed,bytes_read
        info=os.fstat(fd)
        if (info.st_dev,info.st_ino)==(before.st_dev,before.st_ino):
            if not changed:
                changed=True
                if fault=='grow': target.write_bytes(b'x'*10000)
                elif fault=='shrink': target.write_bytes(b'x')
                elif fault=='overwrite': target.write_bytes(b'other')
                else:
                    target.unlink();target.write_bytes(b'fixed')
            data=read(fd,size);bytes_read+=len(data);return data
        return read(fd,size)
    monkeypatch.setattr(os,'read',racing_read)
    with pytest.raises(ValueError,match='Bundle changed'): helper.inventory(root)
    assert changed and bytes_read<=before.st_size+1


def test_inventory_does_not_follow_replaced_directory(tmp_path_factory,monkeypatch):
    root=bundle(tmp_path_factory/'bundle');outside=tmp_path_factory/'outside';outside.mkdir()
    (outside/'secret').write_bytes(b'outside')
    original=os.open;changed=False
    def racing_open(path,flags,*args,**kwargs):
        nonlocal changed
        if path=='_internal' and kwargs.get('dir_fd') is not None and not changed:
            changed=True
            (root/'_internal').rename(root/'old')
            (root/'_internal').symlink_to(outside)
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',racing_open)
    with pytest.raises((ValueError,OSError)): helper.inventory(root)
    assert changed


def test_inventory_never_silently_skips_unreadable_directory(tmp_path_factory,monkeypatch):
    root=bundle(tmp_path_factory/'bundle');inode=(root/'_internal').stat().st_ino
    scan=os.scandir
    def denied(path):
        if isinstance(path,int) and os.fstat(path).st_ino==inode: raise PermissionError('unreadable')
        return scan(path)
    monkeypatch.setattr(os,'scandir',denied)
    with pytest.raises(PermissionError,match='unreadable'): helper.inventory(root)


@pytest.mark.parametrize('limit,value,match',[
    ('MAX_BUNDLE_BYTES',1,'size'),('MAX_BUNDLE_ENTRIES',2,'entry'),('MAX_BUNDLE_DEPTH',0,'depth')])
def test_inventory_enforces_bounded_work(tmp_path_factory,monkeypatch,limit,value,match):
    root=bundle(tmp_path_factory/'bundle');monkeypatch.setattr(helper,limit,value)
    with pytest.raises(ValueError,match=match+' budget'): helper.inventory(root)
