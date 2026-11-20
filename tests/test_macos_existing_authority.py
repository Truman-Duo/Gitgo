"""Real ACLs at installed-image/control boundaries, without administrator use.

Only ownership metadata is substituted for disposable test files. Actual
Darwin ACLs, open descriptors, copying, bounded reads and failure cleanup run.
These tests do not claim a root install or launchd acceptance.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

if sys.platform == 'win32':
    pytest.skip('macOS administrative file boundaries', allow_module_level=True)

from backend.core import macos_file_authority as authority
from backend.core import macos_helper_install as install
from backend.core import macos_helper_lifecycle as lifecycle
from backend.core import macos_identity as identity
from backend.core import sandbox_macos as mac


DARWIN = pytest.mark.skipif(sys.platform != 'darwin', reason='Actual macOS ACL API')


def metadata(info, **changes):
    return SimpleNamespace(**{**{name: getattr(info, name) for name in dir(info)
                                 if name.startswith('st_')}, **changes})


def local_os(monkeypatch, module, **changes):
    """Do not globally replace os.fstat or the native ACL implementation."""
    monkeypatch.setattr(module, 'os', SimpleNamespace(**{**vars(os), **changes}))


@contextmanager
def acl(path, entry):
    before = path.stat()
    subprocess.run(['/bin/chmod', '+a', entry, str(path)], check=True, capture_output=True)
    try:
        assert path.stat().st_mode == before.st_mode
        yield
    finally:
        # Test-created ACLs only, never product policy or existing root paths.
        if path.exists():
            subprocess.run(['/bin/chmod', '-N', str(path)], check=True, capture_output=True)


def pretend_root_inventory(monkeypatch):
    original = install._check_owner
    monkeypatch.setattr(install, '_check_owner',
                        lambda info, sealed: original(metadata(info, st_uid=0), sealed))


def pretend_root_directories(monkeypatch, root, module=install):
    ancestors = {(p.stat().st_dev, p.stat().st_ino) for p in root.parents}
    def fstat(fd):
        info = os.fstat(fd)
        # Disposable ancestry includes /private/tmp. Preserve the directory
        # under test's actual mode; substitute ancestry ownership/modes only.
        mode = info.st_mode & ~0o022 if (info.st_dev, info.st_ino) in ancestors else info.st_mode
        return metadata(info, st_uid=0, st_mode=mode)
    local_os(monkeypatch, module, fstat=fstat)


@DARWIN
@pytest.mark.parametrize('where', ['root', 'nested', 'leaf'])
@pytest.mark.parametrize('entry', ['everyone allow write,append', 'everyone deny delete'])
def test_sealed_inventory_refuses_real_acl_and_unsealed_inventory_remains_data_only(
        tmp_path_factory, monkeypatch, where, entry):
    from tests.test_macos_helper_install import bundle
    root = bundle(tmp_path_factory / 'bundle')
    pretend_root_inventory(monkeypatch)
    target = {'root': root, 'nested': root / '_internal', 'leaf': root / '_internal/data'}[where]
    before = install.inventory(root, sealed=True)
    with acl(target, entry):
        assert install.inventory(root) == before
        with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
            install.inventory(root, sealed=True)
        # Rejection neither edits data nor strips the permission entries.
        with pytest.raises(authority.FileAuthorityError):
            install.inventory(root, sealed=True)


@DARWIN
@pytest.mark.parametrize('where', ['parent', 'leaf'])
def test_trusted_directory_checks_every_real_opened_ancestor(tmp_path_factory, monkeypatch, where):
    root = tmp_path_factory / 'parent' / 'leaf'
    root.mkdir(parents=True, mode=0o700)
    pretend_root_directories(monkeypatch, root)
    install.trusted_directory(root)
    target = root.parent if where == 'parent' else root
    with acl(target, 'everyone allow add_file,add_subdirectory,delete_child'):
        with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
            install.trusted_directory(root)


@DARWIN
@pytest.mark.skipif(os.getuid() == 0, reason='Requires an actual ordinary-user DAC boundary')
def test_trusted_directory_accepts_search_without_listing_and_still_rejects_acl(tmp_path_factory, monkeypatch):
    root = tmp_path_factory / 'search-only'; root.mkdir(mode=0o700)
    pretend_root_directories(monkeypatch, root)
    root.chmod(0o111)
    try:
        # This is real kernel permission enforcement, not a mocked open error.
        with pytest.raises(PermissionError):
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.close(fd)
        install.trusted_directory(root)
        with acl(root, 'everyone deny delete'):
            with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
                install.trusted_directory(root)
    finally:
        root.chmod(0o700)


def test_trusted_directory_does_not_accept_unsupported_acl_platform(monkeypatch):
    monkeypatch.setattr(authority, 'sys', SimpleNamespace(platform='linux'))
    with pytest.raises(authority.FileAuthorityError, match='macOS ACL inspection'):
        install.trusted_directory(Path('/'))


@DARWIN
@pytest.mark.parametrize('entry', ['everyone allow write,append', 'everyone deny delete'])
def test_root_evidence_leaf_refuses_real_acl_before_reading_bytes(tmp_path_factory, monkeypatch, entry):
    path = tmp_path_factory / 'receipt.json'
    path.write_bytes(b'{"phase":"active"}'); path.chmod(0o600)
    local_os(monkeypatch, identity, fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    assert identity.bounded_read(path, 0, True) == path.read_bytes()
    with acl(path, entry):
        local_os(monkeypatch, identity, fstat=lambda fd: metadata(os.fstat(fd), st_uid=0),
                 read=lambda *args: pytest.fail('ACL evidence must not be read'))
        with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
            identity.bounded_read(path, 0, True)


@DARWIN
def test_nonroot_bounded_data_read_does_not_invent_administrative_authority(tmp_path_factory):
    path = tmp_path_factory / 'task-output'
    path.write_bytes(b'data'); path.chmod(0o600)
    with acl(path, 'everyone allow write,append'):
        assert identity.bounded_read(path, os.getuid(), True) == b'data'


@DARWIN
def test_private_directory_rejects_acl_and_preserves_it(tmp_path_factory, monkeypatch):
    path = tmp_path_factory / 'private'; path.mkdir(mode=0o700)
    local_os(monkeypatch, mac, fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    mac.private_directory(path)
    with acl(path, 'everyone allow add_file,delete_child'):
        for _ in range(2):
            with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
                mac.private_directory(path)


@DARWIN
@pytest.mark.parametrize('kind', ['lease', 'lifecycle', 'retirement'])
def test_existing_root_locks_reject_acl_before_acquiring_lock(tmp_path_factory, monkeypatch, kind):
    from scripts import retire_macos_unstarted_trial as retire
    private = tmp_path_factory / 'private'; private.mkdir(mode=0o700)
    path = (private if kind == 'lifecycle' else tmp_path_factory) / 'admission.lock'
    path.touch(mode=0o600)
    module = {'lease': mac, 'lifecycle': lifecycle, 'retirement': retire}[kind]
    local_os(monkeypatch, module, fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    monkeypatch.setattr(lifecycle, 'trusted_directory', lambda path: None)
    if kind != 'lease':
        monkeypatch.setattr(module, 'fcntl', SimpleNamespace(
            LOCK_EX=2, LOCK_NB=4, flock=lambda *args: pytest.fail('ACL lock was acquired')))
    with acl(path, 'everyone allow write,append'):
        with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
            if kind == 'lease':
                fd = mac.lease_owner_lock(tmp_path_factory)
                os.close(fd)
            elif kind == 'lifecycle':
                with lifecycle.locked(tmp_path_factory, 'admission.lock'): pass
            else:
                with retire.locked(path): pass


@DARWIN
@pytest.mark.parametrize('where', ['directory', 'lock'])
def test_execution_service_restart_refuses_acl_before_state_or_launch(tmp_path_factory, monkeypatch, where):
    from backend.core import macos_execution_service as service
    root = tmp_path_factory / 'lease'; root.mkdir(mode=0o700)
    lock = root / 'service.lock'; lock.touch(mode=0o600)
    # Route only this module's fixed trial location to a disposable directory;
    # real platform, directory opens and ACL syscalls stay unchanged.
    monkeypatch.setattr(service, 'Path', lambda value:
        tmp_path_factory if str(value) == '/private/tmp/gitgo-macos-execution-v1' else Path(value))
    local_os(monkeypatch, service, getuid=lambda: 0, geteuid=lambda: 0,
             fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    monkeypatch.setattr(service, 'read_state', lambda *args: pytest.fail('ACL state was read'))
    monkeypatch.setattr(service, 'fcntl', SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, flock=lambda *args: pytest.fail('ACL service lock was acquired')))
    target = root if where == 'directory' else lock
    with acl(target, 'everyone allow write,append'):
        if where == 'directory':
            with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
                service.serve(root, recover_only=True)
        else:
            assert service.serve(root, recover_only=True) == 75
    assert not (root / 'state.json').exists()


@DARWIN
def test_boot_recovery_refuses_acl_allocator_before_flock_or_lease_walk(tmp_path_factory, monkeypatch):
    from backend.core import macos_boot_recovery as recovery
    store = tmp_path_factory / 'durable'; store.mkdir(mode=0o700)
    lock = tmp_path_factory / 'allocator.lock'; lock.touch(mode=0o600)
    monkeypatch.setattr(mac, 'DURABLE_STORE', store)
    monkeypatch.setattr(mac, 'LOCK', str(lock))
    monkeypatch.setattr(install, 'trusted_directory', lambda path: None)
    local_os(monkeypatch, mac, fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    local_os(monkeypatch, recovery, getuid=lambda: 0, geteuid=lambda: 0,
             fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    monkeypatch.setattr(recovery, 'fcntl', SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, flock=lambda *args: pytest.fail('ACL recovery lock was acquired')))
    with acl(lock, 'everyone allow write,append'):
        with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
            recovery.recover_store()
    assert list(store.iterdir()) == []


@DARWIN
@pytest.mark.parametrize('where', ['root', 'control', None])
def test_persistent_allocation_rechecks_legacy_reservation_directories(tmp_path_factory, monkeypatch, where):
    legacy = tmp_path_factory / 'gitgo-uid-recovery-old'
    control = legacy / 'control'; control.mkdir(parents=True, mode=0o700)
    record = control / 'state.json'; record.write_text(json.dumps({'uid': identity.UID_MIN + 7}))
    record.chmod(0o600)
    store = tmp_path_factory / 'new'; durable = tmp_path_factory / 'durable'
    monkeypatch.setattr(mac, 'STORE', store); monkeypatch.setattr(mac, 'DURABLE_STORE', durable)
    monkeypatch.setattr(mac, 'LOCK', str(tmp_path_factory / 'allocator.lock'))
    monkeypatch.setattr(install, 'trusted_directory', lambda path: None)
    local_os(monkeypatch, mac, getuid=lambda: 0, geteuid=lambda: 0,
             fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    original_glob, original_lstat = Path.glob, Path.lstat
    monkeypatch.setattr(Path, 'glob', lambda p, pattern:
        iter([legacy]) if p == Path('/private/tmp') and pattern == 'gitgo-uid-recovery-*'
        else original_glob(p, pattern))
    monkeypatch.setattr(Path, 'lstat', lambda p, *a, **k:
        metadata(original_lstat(p, *a, **k), st_uid=0) if p == legacy else original_lstat(p, *a, **k))
    reads, selected = [], []
    def read(path, *args):
        reads.append(path)
        return path.read_bytes()
    class ReservationObserved(Exception): pass
    def choose(used):
        selected.append(set(used))
        raise ReservationObserved()
    monkeypatch.setattr(identity, 'bounded_read', read)
    monkeypatch.setattr(identity, 'choose_uid', choose)
    with tempfile.TemporaryDirectory(prefix='gitgo-macos-chain-', dir='/private/tmp') as base:
        work = Path(base) / 'work'; work.mkdir(mode=0o700)
        if where is not None:
            target = legacy if where == 'root' else control
            with acl(target, 'everyone allow add_file,delete_child'):
                with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
                    with mac.execution_trial(work, persistent=True):
                        pytest.fail('Legacy ACL reservation admitted a new task')
            assert not reads and not selected
        else:
            with pytest.raises(ReservationObserved):
                with mac.execution_trial(work, persistent=True): pass
            assert reads == [record] and selected == [{identity.UID_MIN + 7}]
    assert record.read_text() == json.dumps({'uid': identity.UID_MIN + 7})
    assert list(durable.iterdir()) == []


@DARWIN
def test_temporary_broker_refuses_top_level_acl_before_listener(tmp_path_factory, monkeypatch):
    from backend.core import macos_broker_service as server
    local_os(monkeypatch, server, getuid=lambda: 0, geteuid=lambda: 0,
             fstat=lambda fd: metadata(os.fstat(fd), st_uid=0))
    monkeypatch.setattr(server, 'listen', lambda *a, **k: pytest.fail('ACL broker opened a listener'))
    with tempfile.TemporaryDirectory(prefix='gitgo-macos-broker-', dir='/private/tmp') as name:
        root = Path(name); root.chmod(0o711)
        with acl(root, 'everyone allow add_file,delete_child'):
            with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
                server.serve(root)
        assert list(root.iterdir()) == []


@DARWIN
@pytest.mark.parametrize('where', ['runtime', 'leaf'])
def test_copied_acl_is_refused_before_publish_and_only_new_staging_is_removed(
        tmp_path_factory, monkeypatch, where):
    from tests.test_macos_helper_install import bundle
    source = bundle(tmp_path_factory / 'source')
    base = tmp_path_factory / 'installed'; base.mkdir()
    old = base / ('a' * 64); old.mkdir(); (old / 'keep').write_bytes(b'old-image')
    monkeypatch.setattr(install, 'BASE', base)
    monkeypatch.setattr(install, 'trusted_directory', lambda path: None)
    pretend_root_inventory(monkeypatch)
    local_os(monkeypatch, install, getuid=lambda: 0, geteuid=lambda: 0,
             chown=lambda *args, **kwargs: None,
             fstat=lambda fd: (metadata(os.fstat(fd), st_uid=0)
                 if (base / '.install.lock').exists() and
                    os.fstat(fd).st_ino == (base / '.install.lock').stat().st_ino
                 else os.fstat(fd)),
             rename=lambda *args: pytest.fail('ACL image was published'))
    original = install.shutil.copytree
    copied = []
    def copytree(src, dst, **options):
        result = original(src, dst, **options)
        target = Path(dst) if where == 'runtime' else Path(dst) / '_internal/data'
        # Exercise an actual ACL on the post-copy image, independent of whether
        # this Python build preserves source ACLs in shutil.copytree.
        subprocess.run(['/bin/chmod', '+a', 'everyone allow write,append', str(target)],
                       check=True, capture_output=True)
        copied.append(Path(dst).parent)
        return result
    monkeypatch.setattr(install, 'shutil', SimpleNamespace(copytree=copytree, rmtree=install.shutil.rmtree))
    calls = []
    monkeypatch.setattr(install, 'subprocess', SimpleNamespace(run=lambda *a, **k: calls.append(a[0])))
    with pytest.raises(authority.FileAuthorityError, match='ACL entries'):
        install.install(source, allow_local_adhoc=True)
    assert len(calls) == 1  # source codesign only; staged candidate not processed
    assert copied and not copied[0].exists()
    assert (old / 'keep').read_bytes() == b'old-image'
    assert sorted(p.name for p in base.iterdir()) == ['.install.lock', 'a' * 64]
    assert (source / 'gitgo-host').read_bytes() == b'fixture'


def test_missing_acl_reader_dependency_blocks_source_retirement(tmp_path_factory, monkeypatch):
    from scripts import retire_macos_unstarted_trial as retire
    root = tmp_path_factory
    for name in ('macos_identity.py', 'macos_execution_service.py', 'macos_resource_guard.py'):
        (root / name).write_bytes(Path(mac.__file__).with_name(name).read_bytes())
        (root / name).chmod(0o500)
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda p, *a, **k: metadata(original(p, *a, **k), st_uid=0))
    monkeypatch.setattr(identity, 'bounded_read', lambda path, *args: path.read_bytes())
    with pytest.raises(FileNotFoundError, match='macos_file_authority'):
        retire.verify_source_service(root, [identity.PYTHON, '-I', str(root / 'macos_execution_service.py'), str(root)])
