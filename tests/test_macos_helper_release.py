"""Real Ed25519/archive evidence; no candidate execution or host elevation.

Filesystem root ownership and process admission are simulated for temporary
fixtures only. Their rejection rules are tested separately. These tests are
not Developer ID, initial bootstrap, installation, or replay acceptance.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from backend.core import macos_helper_release as release
from backend.core.update.tree import inventory
from backend.core.update.validation import UpdateError
from scripts.prepare_update_release import github_policy
from tests.test_update_manifest import sign_raw, signing
from tests.test_update_transaction import archive_fixture, build_fixture


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX file descriptors and permissions')
ERRORS = (release.HelperReleaseError, UpdateError, OSError, ValueError)
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def acl_system_boundary(monkeypatch):
    # Portable data tests simulate the macOS-only ACL syscall boundary. Native
    # ACL rejection has separate real-filesystem tests and must never be skipped
    # by the shipping entry, which rejects non-macOS before reading anything.
    if release.sys.platform != 'darwin':
        monkeypatch.setattr(release, 'require_no_extended_acl', lambda fd: None)


def refresh(root):
    build_path = root / 'build-manifest.json'
    build = json.loads(build_path.read_bytes())
    build['files'] = [
        ({'path': item['path'], 'link': item['link']} if item['kind'] == 'symlink'
         else {k: item[k] for k in ('path', 'bytes', 'sha256')})
        for item in inventory(root)
        if item['kind'] != 'directory' and item['path'] != 'build-manifest.json'
    ]
    build_path.write_text(json.dumps(build))


@pytest.fixture
def lab(tmp_path_factory, signing, monkeypatch):
    root = tmp_path_factory / 'package'
    build_fixture(root)
    helper = root / 'internal/gitgo-host'
    (helper / '_internal').mkdir()
    (helper / '_internal/python-runtime').write_bytes(b'Frozen interpreter and dependency bytes')
    (helper / '_internal/config.json').write_bytes(b'{"resource": true}')
    (helper / '_internal/python-alias').symlink_to('python-runtime')
    # An executable-looking candidate must never run, even after a valid signature.
    (helper / 'gitgo-host').write_bytes(b'#!/bin/sh\necho CANDIDATE_MUST_NOT_EXECUTE\nexit 99\n')
    refresh(root)
    public = signing[0].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = github_policy(public, 'ephemeral-test', 'macos', 'arm64')
    policy['allowed_hosts'] = ['releases.example.test']
    for channel in policy['channels']:
        policy['channels'][channel] = {
            'manifest_url': 'https://releases.example.test/' + channel + '.json',
            'signature_url': 'https://releases.example.test/' + channel + '.sig',
        }
    raw_policy = json.dumps(policy).encode()
    work_parent = tmp_path_factory / 'scratch'
    work_parent.mkdir()
    actual_inventory = release.helper_inventory
    monkeypatch.setattr(release, '_require_local_admin', lambda _: None)
    monkeypatch.setattr(release, '_read_root_policy', lambda: raw_policy)
    monkeypatch.setattr(release, '_clock', lambda: NOW)
    monkeypatch.setattr(release, '_temporary_work',
                        lambda: Path(tempfile.mkdtemp(prefix='owned-', dir=work_parent)))
    monkeypatch.setattr(release, 'helper_inventory', lambda path, *, sealed: actual_inventory(path))

    def forbidden(*args, **kwargs):
        pytest.fail('Read-only evidence must never launch a process')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)

    def prepare(**changes):
        return archive_fixture(root, tmp_path_factory, signing, **changes)

    def inspect(raw, detached, archive, **kwargs):
        return release.inspect_release(archive, raw, detached, channel='preview',
                                       allow_local_adhoc=True, **kwargs)

    return SimpleNamespace(root=root, helper=helper, directory=tmp_path_factory, signing=signing,
                           policy=raw_policy, prepare=prepare, inspect=inspect, scratch=work_parent)


def test_complete_authenticated_archive_and_helper_inventory_are_diagnostic_only(lab):
    raw, detached, archive = lab.prepare()
    original = archive.read_bytes()
    report = lab.inspect(raw, detached, archive)
    assert report['purpose'] == 'local-release-inspection'
    assert report['production_enabled'] is False
    assert report['policy_sha256'] == hashlib.sha256(lab.policy).hexdigest()
    assert report['archive_sha256'] == hashlib.sha256(original).hexdigest()
    assert report['archive_bytes'] == len(original)
    assert report['manifest_sha256'] == hashlib.sha256(raw).hexdigest()
    assert report['helper_inventory_sha256'] == release.identity(report['helper_inventory'])
    assert any(item[:2] == ['_internal/python-runtime', 'file'] for item in report['helper_inventory'])
    assert ['_internal/python-alias', 'link', 'python-runtime'] in report['helper_inventory']
    assert report['source_commit'] == 'b' * 40 and report['source_tree'] == 'c' * 40
    assert archive.read_bytes() == original
    assert list(lab.scratch.iterdir()) == []
    assert not list(lab.directory.rglob('install.json'))
    assert not list(lab.directory.rglob('*.plist'))


@pytest.mark.parametrize('damage', ['changed_raw', 'wrong_key', 'unknown_key', 'duplicate_json', 'oversize_signature'])
def test_bad_authentication_precedes_archive_open_or_temporary_write(lab, monkeypatch, damage):
    raw, detached, archive = lab.prepare()
    if damage == 'changed_raw': raw += b'\n'
    elif damage == 'wrong_key': detached = sign_raw(raw, Ed25519PrivateKey.generate())
    elif damage == 'unknown_key': detached = sign_raw(raw, lab.signing[0], key_id='candidate-choice')
    elif damage == 'duplicate_json':
        raw = raw[:-1] + b', "product_id": "gitgo-terminal"}'
        detached = sign_raw(raw, lab.signing[0])
    else: detached = b' ' * 1025
    monkeypatch.setattr(release, '_open_file', lambda *a, **k: pytest.fail('Archive opened before authentication'))
    with pytest.raises(ERRORS): lab.inspect(raw, detached, archive)
    assert list(lab.scratch.iterdir()) == []


@pytest.mark.parametrize('changes', [
    {'product_id': 'other-product'}, {'platform': 'linux', 'architecture': 'aarch64'},
    {'channel': 'stable', 'version': '1.0.0'},
    {'published_at': '2026-10-10T00:00:00Z', 'expires_at': '2026-10-12T00:00:00Z'},
    {'published_at': '2026-10-14T00:00:00Z', 'expires_at': '2026-10-15T00:00:00Z'},
    {'url': 'https://untrusted.example.test/release.tar.gz'},
])
def test_signed_wrong_scope_clock_or_host_never_inspects_archive(lab, monkeypatch, changes):
    raw, detached, archive = lab.prepare(**changes)
    monkeypatch.setattr(release, '_open_file', lambda *a, **k: pytest.fail('Scope rejected too late'))
    with pytest.raises(ERRORS): lab.inspect(raw, detached, archive)


def test_candidate_embedded_key_does_not_choose_trust(lab):
    (lab.root / 'release-trust.json').write_text('{"public_keys": ["candidate-owned"]}')
    refresh(lab.root)
    raw, _, archive = lab.prepare()
    detached = sign_raw(raw, Ed25519PrivateKey.generate())
    with pytest.raises(UpdateError, match='SIGNATURE_INVALID'):
        lab.inspect(raw, detached, archive)
    # The same inert file cannot displace the independently pinned test authority.
    report = lab.inspect(raw, sign_raw(raw, lab.signing[0]), archive)
    assert report['policy_sha256'] == hashlib.sha256(lab.policy).hexdigest()


@pytest.mark.parametrize('damage', ['unconfigured_channel', 'duplicate_json', 'missing_key'])
def test_invalid_root_policy_never_reads_candidate(lab, monkeypatch, damage):
    raw, detached, archive = lab.prepare()
    value = json.loads(lab.policy)
    if damage == 'unconfigured_channel': del value['channels']['preview']
    elif damage == 'missing_key': value['public_keys'] = []
    policy = json.dumps(value).encode()
    if damage == 'duplicate_json': policy = policy[:-1] + b', "product_id": "gitgo-terminal"}'
    monkeypatch.setattr(release, '_read_root_policy', lambda: policy)
    monkeypatch.setattr(release, '_open_file', lambda *a, **k: pytest.fail('Invalid authority opened candidate'))
    with pytest.raises(UpdateError): lab.inspect(raw, detached, archive)


@pytest.mark.parametrize('damage', ['append', 'truncate', 'flip', 'symlink', 'ancestor', 'hardlink', 'fifo'])
def test_archive_bytes_and_file_types_are_rechecked(lab, damage):
    raw, detached, archive = lab.prepare()
    body = archive.read_bytes()
    if damage == 'append': archive.write_bytes(body + b'x')
    elif damage == 'truncate': archive.write_bytes(body[:-1])
    elif damage == 'flip': archive.write_bytes(bytes([body[0] ^ 1]) + body[1:])
    elif damage == 'symlink':
        target = archive.with_suffix('.real'); archive.rename(target); archive.symlink_to(target)
    elif damage == 'ancestor':
        alias = lab.directory / 'alias'; alias.symlink_to(lab.directory, target_is_directory=True)
        archive = alias / archive.name
    elif damage == 'hardlink': os.link(archive, lab.directory / 'another-link')
    else: archive.unlink(); os.mkfifo(archive)
    with pytest.raises(ERRORS): lab.inspect(raw, detached, archive)
    assert list(lab.scratch.iterdir()) == []


@pytest.mark.parametrize('damage', ['dirty', 'extra', 'missing', 'dependency', 'version', 'migration'])
def test_real_signature_does_not_rescue_an_inconsistent_complete_contract(lab, damage):
    build_path = lab.root / 'build-manifest.json'
    build = json.loads(build_path.read_bytes())
    if damage == 'dirty':
        build['source']['dirty'] = True; build_path.write_text(json.dumps(build))
    elif damage == 'extra': (lab.root / 'unexpected-resource').write_text('unlisted')
    elif damage == 'missing': (lab.root / 'uninstall.sh').unlink()
    elif damage == 'dependency': (lab.helper / '_internal/python-runtime').write_text('tampered dependency')
    elif damage == 'version':
        product = lab.root / 'product.json'; data = json.loads(product.read_bytes())
        data['version'] = '9.0.0'; product.write_text(json.dumps(data)); refresh(lab.root)
    else: (lab.root / 'migrations/state.py').write_text('changed migration'); refresh(lab.root)
    raw, detached, archive = lab.prepare()
    with pytest.raises(UpdateError, match='PACKAGE_CONTRACT'):
        lab.inspect(raw, detached, archive)
    assert list(lab.scratch.iterdir()) == []


@pytest.mark.parametrize('damage', ['duplicate', 'escape', 'outside_helper'])
def test_signed_archive_duplicate_paths_and_link_escapes_are_rejected(lab, damage):
    if damage == 'outside_helper':
        # Still inside the full package, but outside the standalone helper subtree.
        (lab.helper / '_internal/project-link').symlink_to('../../../migrations/state.py')
        refresh(lab.root)
    raw, _, archive = lab.prepare()
    if damage != 'outside_helper':
        source = io.BytesIO(archive.read_bytes()); output = io.BytesIO()
        with tarfile.open(fileobj=source, mode='r:gz') as old, tarfile.open(fileobj=output, mode='w:gz') as new:
            members = old.getmembers()
            for member in members:
                new.addfile(member, old.extractfile(member) if member.isfile() else None)
            if damage == 'duplicate':
                member = next(member for member in members if member.isfile())
                new.addfile(member, old.extractfile(member))
            else:
                member = tarfile.TarInfo('macos-arm64/../../outside'); member.size = 4
                new.addfile(member, io.BytesIO(b'evil'))
        archive.write_bytes(output.getvalue())
        wire = json.loads(raw); wire['bytes'] = archive.stat().st_size
        wire['sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest(); raw = json.dumps(wire).encode()
    detached = sign_raw(raw, lab.signing[0])
    with pytest.raises(ERRORS): lab.inspect(raw, detached, archive)
    assert list(lab.scratch.iterdir()) == []
    assert not (lab.directory / 'outside').exists()


@pytest.mark.parametrize('change', ['policy', 'expiry', 'payload', 'helper_inventory'])
def test_evidence_changes_during_inspection_are_not_returned(lab, monkeypatch, change):
    raw, detached, archive = lab.prepare()
    if change == 'policy':
        calls = 0
        def policy():
            nonlocal calls
            calls += 1
            return lab.policy if calls == 1 else lab.policy + b'\n'
        monkeypatch.setattr(release, '_read_root_policy', policy)
    elif change == 'expiry':
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            return NOW if calls < 3 else datetime(2026, 10, 15, tzinfo=timezone.utc)
        monkeypatch.setattr(release, '_clock', clock)
    elif change == 'payload':
        original = release.helper_inventory
        def mutate(path, **kwargs):
            result = original(path, **kwargs)
            (path.parents[1] / 'install.sh').write_text('changed after initial contract check')
            return result
        monkeypatch.setattr(release, 'helper_inventory', mutate)
    else:
        original = release.helper_inventory
        def inconsistent(path, **kwargs):
            result = original(path, **kwargs)
            return [item for item in result if item[0] != '_internal/config.json']
        monkeypatch.setattr(release, 'helper_inventory', inconsistent)
    with pytest.raises(ERRORS): lab.inspect(raw, detached, archive)
    assert list(lab.scratch.iterdir()) == []


def test_archive_mutation_during_read_is_detected_even_when_copied_bytes_match(lab, monkeypatch):
    raw, detached, archive = lab.prepare()
    real_fdopen = os.fdopen
    archive_inode = archive.stat().st_ino
    changed = False
    class MutatingSource:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, count):
            nonlocal changed
            data = self.stream.read(count)
            if not changed:
                changed = True
                before = archive.stat()
                os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            return data
    def opened(fd, *args, **kwargs):
        stream = real_fdopen(fd, *args, **kwargs)
        return MutatingSource(stream) if os.fstat(fd).st_ino == archive_inode else stream
    monkeypatch.setattr(release.os, 'fdopen', opened)
    with pytest.raises(release.HelperReleaseError, match='changed during copying'):
        lab.inspect(raw, detached, archive)
    assert changed and list(lab.scratch.iterdir()) == []


def test_unexpected_failure_also_removes_only_owned_staging(lab, monkeypatch):
    raw, detached, archive = lab.prepare()
    sentinel = lab.directory / 'retain-me'; sentinel.write_bytes(b'not a cleanup target')
    before = archive.read_bytes()
    def broken(*args): raise RuntimeError('unexpected contract failure')
    monkeypatch.setattr(release, 'verify_bundle_contract', broken)
    with pytest.raises(RuntimeError, match='unexpected contract failure'):
        lab.inspect(raw, detached, archive)
    assert list(lab.scratch.iterdir()) == []
    assert sentinel.read_bytes() == b'not a cleanup target' and archive.read_bytes() == before


@pytest.mark.parametrize('system,machine,uid,euid,allow', [
    ('linux', 'arm64', 0, 0, True), ('darwin', 'x86_64', 0, 0, True),
    ('darwin', 'arm64', 501, 0, True), ('darwin', 'arm64', 0, 501, True),
    ('darwin', 'arm64', 0, 0, False), ('darwin', 'arm64', 0, 0, 1),
])
def test_entry_refuses_without_reading_policy_or_input(monkeypatch, system, machine, uid, euid, allow):
    monkeypatch.setattr(release.sys, 'platform', system)
    monkeypatch.setattr(release.platform, 'machine', lambda: machine)
    monkeypatch.setattr(release.os, 'getuid', lambda: uid)
    monkeypatch.setattr(release.os, 'geteuid', lambda: euid)
    monkeypatch.setattr(release, '_read_root_policy', lambda: pytest.fail('Authority touched before admission'))
    with pytest.raises((PermissionError, release.HelperReleaseError)):
        release.inspect_release('/does-not-exist', b'', b'', channel='preview', allow_local_adhoc=allow)


def root_policy_fixture(monkeypatch, directory):
    path = directory / 'release-trust.json'; path.write_bytes(b'{"public": "fixture"}')
    monkeypatch.setattr(release, '_ROOT_POLICY', path)
    original = release._root_owned
    def controlled(info, *, directory):
        # Only root ownership and writable ancestors are simulated; opens are real.
        fixed = SimpleNamespace(st_mode=info.st_mode & ~0o022, st_uid=0,
                                st_nlink=info.st_nlink, st_size=info.st_size)
        return original(fixed, directory=directory)
    monkeypatch.setattr(release, '_root_owned', controlled)
    return path


def test_root_policy_reads_fixed_path_with_no_environment_or_parameter_override(monkeypatch, tmp_path_factory):
    path = root_policy_fixture(monkeypatch, tmp_path_factory)
    monkeypatch.setenv('GITGO_RELEASE_TRUST', '/arbitrary/candidate-policy')
    assert release._read_root_policy() == path.read_bytes()
    with pytest.raises(TypeError): release._read_root_policy(path)
    path.unlink()
    with pytest.raises(release.HelperReleaseError): release._read_root_policy()


@pytest.mark.parametrize('kind', ['leaf', 'ancestor', 'hardlink', 'fifo', 'oversize'])
def test_root_policy_descriptor_traversal_rejects_unsafe_files(monkeypatch, tmp_path_factory, kind):
    path = root_policy_fixture(monkeypatch, tmp_path_factory)
    if kind == 'leaf':
        target = path.with_suffix('.actual'); path.rename(target); path.symlink_to(target)
    elif kind == 'ancestor':
        alias = tmp_path_factory / 'alias'; alias.symlink_to(tmp_path_factory, target_is_directory=True)
        monkeypatch.setattr(release, '_ROOT_POLICY', alias / path.name)
    elif kind == 'hardlink': os.link(path, tmp_path_factory / 'alias')
    elif kind == 'fifo': path.unlink(); os.mkfifo(path)
    else: path.write_bytes(b'x' * (release.MAX_POLICY_BYTES + 1))
    with pytest.raises(release.HelperReleaseError): release._read_root_policy()


@pytest.mark.parametrize('location', ['root', 'parent', 'leaf'])
def test_root_policy_rejects_acl_failure_at_each_authority_level(monkeypatch, tmp_path_factory, location):
    path = root_policy_fixture(monkeypatch, tmp_path_factory)
    selected = Path('/') if location == 'root' else path.parent if location == 'parent' else path
    target = selected.stat()
    def denied(fd):
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) == (target.st_dev, target.st_ino):
            raise release.HelperReleaseError('ACL authority denied')
    monkeypatch.setattr(release, 'require_no_extended_acl', denied)
    with pytest.raises(release.HelperReleaseError, match='ACL authority denied'):
        release._read_root_policy()


@pytest.mark.skipif(release.sys.platform != 'darwin', reason='Real macOS extended ACL')
def test_root_policy_rejects_real_acl_even_when_posix_mode_is_unchanged(monkeypatch, tmp_path_factory):
    path = root_policy_fixture(monkeypatch, tmp_path_factory)
    path.chmod(0o644)
    subprocess.run(['/bin/chmod', '+a', 'everyone allow write,append', str(path)], check=True)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        with pytest.raises(ValueError): release._read_root_policy()
    finally:
        subprocess.run(['/bin/chmod', '-N', str(path)], check=True)


def test_root_policy_read_detects_concurrent_changes(monkeypatch, tmp_path_factory):
    path = root_policy_fixture(monkeypatch, tmp_path_factory)
    real_fdopen = os.fdopen
    class ChangingPolicy:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, count):
            raw = self.stream.read(count)
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            return raw
    monkeypatch.setattr(release.os, 'fdopen', lambda *a, **k: ChangingPolicy(real_fdopen(*a, **k)))
    with pytest.raises(release.HelperReleaseError, match='changed while being read'):
        release._read_root_policy()


@pytest.mark.skipif(not Path('/private/tmp').is_dir(), reason='macOS private temporary parent')
def test_real_private_staging_ignores_inherited_tmpdir(monkeypatch, tmp_path_factory):
    # This creates a private ordinary-user temporary directory, never root state.
    monkeypatch.setenv('TMPDIR', str(tmp_path_factory / 'must-not-use'))
    work = release._temporary_work()
    try:
        assert work.parent == Path('/private/tmp')
        assert stat.S_IMODE(work.stat().st_mode) == 0o700
        assert work.stat().st_uid == os.getuid()
        assert list(work.iterdir()) == []
    finally:
        work.rmdir()


@pytest.mark.skipif(not Path('/private/tmp').is_dir(), reason='macOS private temporary parent')
def test_failed_privacy_check_removes_new_work_before_it_is_returned(monkeypatch):
    created = []
    real_mkdtemp = tempfile.mkdtemp
    def not_private(**kwargs):
        path = Path(real_mkdtemp(**kwargs)); created.append(path)
        path.chmod(0o755)
        return str(path)
    monkeypatch.setattr(release.tempfile, 'mkdtemp', not_private)
    with pytest.raises(release.HelperReleaseError, match='not private'):
        release._temporary_work()
    assert len(created) == 1 and not created[0].exists()


@pytest.mark.skipif(not Path('/private/tmp').is_dir(), reason='macOS private temporary parent')
@pytest.mark.parametrize('location', ['parent', 'new_directory'])
def test_staging_acl_failure_does_not_leave_a_private_directory(monkeypatch, location):
    created = []
    real_mkdtemp = tempfile.mkdtemp
    def tracked(**kwargs):
        path = Path(real_mkdtemp(**kwargs)); created.append(path)
        return str(path)
    parent = Path('/private/tmp').stat()
    def denied(fd):
        info = os.fstat(fd)
        is_parent = (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino)
        if is_parent == (location == 'parent'):
            raise release.HelperReleaseError('ACL authority denied')
    monkeypatch.setattr(release.tempfile, 'mkdtemp', tracked)
    monkeypatch.setattr(release, 'require_no_extended_acl', denied)
    with pytest.raises(release.HelperReleaseError, match='ACL authority denied'):
        release._temporary_work()
    assert len(created) == (0 if location == 'parent' else 1)
    assert all(not path.exists() for path in created)


@pytest.mark.parametrize('mode,uid,links,size,directory', [
    (stat.S_IFDIR | 0o777, 0, 1, 10, True), (stat.S_IFDIR | 0o755, 501, 1, 10, True),
    (stat.S_IFLNK | 0o755, 0, 1, 10, True), (stat.S_IFREG | 0o664, 0, 1, 10, False),
    (stat.S_IFREG | 0o644, 501, 1, 10, False), (stat.S_IFREG | 0o644, 0, 2, 10, False),
    (stat.S_IFREG | 0o644, 0, 1, 0, False), (stat.S_IFIFO | 0o600, 0, 1, 10, False),
])
def test_root_policy_rejects_ownership_permissions_and_special_files(mode, uid, links, size, directory):
    info = SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links, st_size=size)
    with pytest.raises(release.HelperReleaseError): release._root_owned(info, directory=directory)
