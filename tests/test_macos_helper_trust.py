"""Strict publisher seam tests; no real Developer ID acceptance is claimed.

Positive certificate verification is mocked. Native tests only compile the
requirement and reject unsigned/ad-hoc/tampered disposable files. No developer
credentials, host keychain changes or privileged installation are needed.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

if sys.platform == 'win32':
    pytest.skip('macOS publisher and POSIX policy checks', allow_module_level=True)

from backend.core import macos_helper_trust as trust


TEAM = 'TEST123456'
IDENTIFIER = 'org.gitgo.test.sandbox-host'


def policy_bytes(**overrides):
    value = dict(schema=1, product_id='gitgo-terminal', kind='developer-id-application',
                 team_identifier=TEAM, signing_identifier=IDENTIFIER)
    value.update(overrides)
    return json.dumps(value, sort_keys=True).encode()


def parse(raw):
    return trust.policy_from_authenticated_release(
        raw, expected_sha256=hashlib.sha256(raw).hexdigest())


@pytest.fixture
def policy():
    return parse(policy_bytes())


@pytest.fixture
def executable(tmp_path_factory):
    result = tmp_path_factory / 'gitgo-host'
    result.write_bytes(b'fixed executable fixture')
    result.chmod(0o755)
    return result


def metadata(*, team=TEAM, identifier=IDENTIFIER):
    return (
        f'Executable=/fixture/gitgo-host\nIdentifier={identifier}\n'
        'Format=Mach-O thin (arm64)\n'
        'CodeDirectory v=20500 size=123 flags=0x10000(runtime) hashes=3+7 location=embedded\n'
        'Signature size=9000\n'
        'Authority=Developer ID Application: Fixture (TEST123456)\n'
        'Authority=Developer ID Certification Authority\n'
        'Authority=Apple Root CA\n'
        f'TeamIdentifier={team}\nSealed Resources=none\n'
    ).encode()


def mock_codesign(monkeypatch, *, output=None, reject_verify=False, during_display=None):
    """Mock only the certificate-check seam, never used as native acceptance."""
    calls = []
    monkeypatch.setattr(trust.sys, 'platform', 'darwin')

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if '--verify' in argv:
            return subprocess.CompletedProcess(argv, 1 if reject_verify else 0, b'', b'')
        if during_display:
            during_display()
        return subprocess.CompletedProcess(argv, 0, b'', metadata() if output is None else output)

    monkeypatch.setattr(trust.subprocess, 'run', run)
    return calls


def test_pinned_policy_is_exact_and_cannot_enable_production(policy):
    assert policy.team_identifier == TEAM
    assert policy.signing_identifier == IDENTIFIER
    assert policy.policy_sha256 == hashlib.sha256(policy_bytes()).hexdigest()
    assert trust.PRODUCTION_ENABLED is False


@pytest.mark.parametrize('overrides', [
    {'schema': True}, {'schema': 2}, {'product_id': 'other'},
    {'kind': 'adhoc'}, {'kind': 'developer-id-installer'},
    {'team_identifier': 'test123456'}, {'team_identifier': 'OTHER1234567'},
    {'team_identifier': 'TEST12345*'}, {'team_identifier': None},
    {'team_identifier': 'TEST123456" or always'},
    {'signing_identifier': ''}, {'signing_identifier': 'org.gitgo.*'},
    {'signing_identifier': 'org.gitgo" or always'},
    {'signing_identifier': 'org.gitgo\nother'}, {'signing_identifier': '-anything'},
    {'signing_identifier': '\u006frg.\u0433itgo'}, {'signing_identifier': 'x' * 256},
    {'signing_identifier': 42}, {'production_enabled': True},
    {'allow_local_adhoc': True}, {'requirement': 'always'},
])
def test_policy_rejects_widening_or_requirement_injection(overrides):
    with pytest.raises(trust.PublisherTrustError):
        parse(policy_bytes(**overrides))


@pytest.mark.parametrize('raw', [b'', b'null', b'[]', b'{}', b'\xff', b'x' * 4097,
    b'{"schema":1,"schema":1}',
    policy_bytes().replace(b'"schema": 1', b'"schema": NaN'),
    policy_bytes().replace(b'"team_identifier": "TEST123456"',
                           b'"team_identifier": "OTHER12345", "team_identifier": "TEST123456"'),
])
def test_policy_rejects_ambiguous_invalid_or_unbounded_json(raw):
    with pytest.raises(trust.PublisherTrustError):
        parse(raw)


@pytest.mark.parametrize('digest', [None, '', '0' * 64, 'A' * 64, 'a' * 63, 'a' * 65])
def test_release_policy_cannot_supply_its_own_unchecked_digest(digest):
    with pytest.raises(trust.PublisherTrustError):
        trust.policy_from_authenticated_release(policy_bytes(), expected_sha256=digest)


def test_root_reader_has_no_environment_policy_override(monkeypatch, tmp_path_factory):
    path = tmp_path_factory / 'attacker-policy.json'
    path.write_bytes(policy_bytes())
    monkeypatch.setenv('GITGO_HELPER_PUBLISHER_POLICY', str(path))
    monkeypatch.setenv('GITGO_HELPER_TEAM_IDENTIFIER', TEAM)
    calls = []

    def missing(path, flags, **kwargs):
        calls.append(path)
        raise FileNotFoundError()

    monkeypatch.setattr(trust.os, 'open', missing)
    with pytest.raises(trust.PublisherTrustError, match='unavailable'):
        trust.load_root_policy()
    assert calls == ['/']
    assert str(trust._ROOT_POLICY_PATH) == (
        '/Library/PrivilegedHelperTools/org.gitgo.sandbox-v1/publisher-policy.json')
    with pytest.raises(TypeError):
        trust.load_root_policy(path)


@pytest.mark.parametrize('mode,uid,nlink,size,directory', [
    (stat.S_IFDIR | 0o777, 0, 1, 1, True),
    (stat.S_IFDIR | 0o755, 501, 1, 1, True),
    (stat.S_IFLNK | 0o755, 0, 1, 1, True),
    (stat.S_IFREG | 0o664, 0, 1, 100, False),
    (stat.S_IFREG | 0o644, 501, 1, 100, False),
    (stat.S_IFREG | 0o644, 0, 2, 100, False),
    (stat.S_IFREG | 0o644, 0, 1, 4097, False),
    (stat.S_IFREG | 0o644, 0, 1, 0, False),
    (stat.S_IFIFO | 0o600, 0, 1, 100, False),
])
def test_root_policy_rejects_untrusted_metadata(mode, uid, nlink, size, directory):
    info = SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=nlink, st_size=size)
    with pytest.raises(trust.PublisherTrustError):
        trust._check_root_owned(info, directory=directory)


def root_fixture_reader(monkeypatch, tmp_path_factory):
    """Exercise descriptor traversal; simulate root ownership only in fixtures."""
    path = tmp_path_factory / 'publisher-policy.json'
    path.write_bytes(policy_bytes())
    monkeypatch.setattr(trust, '_ROOT_POLICY_PATH', path)
    check = trust._check_root_owned
    observed = []

    def controlled(info, *, directory=False):
        observed.append((info, directory))
        # Temporary paths are intentionally user-owned/world-writable ancestors.
        # Root/mode rejection is exercised independently above and below.
        fixed = SimpleNamespace(st_mode=info.st_mode & ~0o022, st_uid=0,
                                st_nlink=info.st_nlink, st_size=info.st_size)
        return check(fixed, directory=directory)

    monkeypatch.setattr(trust, '_check_root_owned', controlled)
    if sys.platform != 'darwin':
        # These cases inspect portable descriptor traversal, not Darwin ACLs.
        # Native query failures and real ACLs have their own platform tests.
        monkeypatch.setattr(trust, 'require_no_extended_acl', lambda fd: None)
    return path, observed


def test_root_policy_reader_opens_every_component_without_symlinks(monkeypatch, tmp_path_factory):
    path, observed = root_fixture_reader(monkeypatch, tmp_path_factory)
    real_open = os.open
    opens = []

    def opened(name, flags, **kwargs):
        opens.append((name, flags, kwargs))
        return real_open(name, flags, **kwargs)

    monkeypatch.setattr(trust.os, 'open', opened)
    assert trust.load_root_policy() == parse(path.read_bytes())
    assert len(observed) == len(path.parts)
    assert len(opens) == len(path.parts)
    assert opens[0][0] == '/' and opens[0][2] == {}
    assert all(flags & os.O_NOFOLLOW for _, flags, _ in opens)
    assert all(flags & os.O_DIRECTORY for _, flags, _ in opens[:-1])
    assert all('dir_fd' in kwargs for _, _, kwargs in opens[1:])


@pytest.mark.skipif(os.name == 'nt', reason='POSIX descriptor ownership')
@pytest.mark.parametrize('kind', ['leaf', 'ancestor', 'hardlink', 'fifo'])
def test_root_reader_rejects_symlinks_hardlinks_and_special_files(monkeypatch, tmp_path_factory, kind):
    path, _ = root_fixture_reader(monkeypatch, tmp_path_factory)
    if kind == 'leaf':
        target = tmp_path_factory / 'target'
        path.rename(target)
        path.symlink_to(target)
    elif kind == 'ancestor':
        link = tmp_path_factory / 'alias'
        link.symlink_to(tmp_path_factory, target_is_directory=True)
        monkeypatch.setattr(trust, '_ROOT_POLICY_PATH', link / path.name)
    elif kind == 'hardlink':
        os.link(path, tmp_path_factory / 'alias')
    else:
        path.unlink()
        os.mkfifo(path)
    with pytest.raises(trust.PublisherTrustError):
        trust.load_root_policy()


def test_root_policy_reader_detects_in_place_mutation(monkeypatch, tmp_path_factory):
    path, _ = root_fixture_reader(monkeypatch, tmp_path_factory)
    read = os.read
    changed = False

    def mutating(fd, count):
        nonlocal changed
        result = read(fd, count)
        if not changed:
            changed = True
            path.write_bytes(policy_bytes(team_identifier='OTHER12345'))
        return result

    monkeypatch.setattr(trust.os, 'read', mutating)
    with pytest.raises(trust.PublisherTrustError, match='changed'):
        trust.load_root_policy()


def test_requirement_requires_apple_developer_id_application_and_both_exact_ids(policy):
    assert trust.developer_id_requirement(policy) == (
        'anchor apple generic and identifier "org.gitgo.test.sandbox-host"'
        ' and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
        ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
        ' and certificate leaf[subject.OU] = "TEST123456"'
    )
    with pytest.raises(trust.PublisherTrustError):
        trust.developer_id_requirement({'team_identifier': TEAM})


def test_mocked_certificate_success_checks_integrity_and_emits_only_bounded_identity(
        monkeypatch, executable, policy):
    calls = mock_codesign(monkeypatch)
    monkeypatch.setenv('CODESIGN_ALLOCATE', '/attacker/tool')
    monkeypatch.setenv('DYLD_INSERT_LIBRARIES', '/attacker/library')
    result = trust.verify_publisher(executable, policy)
    assert result.team_identifier == TEAM and result.signing_identifier == IDENTIFIER
    assert result.policy_sha256 == policy.policy_sha256
    assert result.executable_sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert result.production_enabled is False and trust.PRODUCTION_ENABLED is False
    assert len(calls) == 2
    assert calls[0][0] == [
        '/usr/bin/codesign', '--verify', '--deep', '--strict', '--all-architectures',
        '-R', '=' + trust.developer_id_requirement(policy), str(executable),
    ]
    assert calls[1][0] == ['/usr/bin/codesign', '--display', '--verbose=4', str(executable)]
    for _, kwargs in calls:
        assert kwargs == dict(stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                              check=False, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LANG': 'C'})


def test_rejected_requirement_cannot_be_rescued_by_matching_display_text(monkeypatch, executable, policy):
    calls = mock_codesign(monkeypatch, reject_verify=True)
    with pytest.raises(trust.PublisherTrustError, match='rejected'):
        trust.verify_publisher(executable, policy)
    assert len(calls) == 1


@pytest.mark.parametrize('raw', [
    metadata(team='OTHER12345'), metadata(identifier='org.gitgo.other'),
    metadata(team='not set'), metadata() + b'Identifier=org.gitgo.other\n',
    metadata() + b'TeamIdentifier=TEST123456\n',
    metadata().replace(b'TeamIdentifier=TEST123456\n', b''),
    metadata().replace(b'flags=0x10000(runtime)', b'flags=0x2(adhoc)'),
    metadata().replace(b'flags=0x10000(runtime)', b'flags=0x10002(runtime)'),
    metadata().replace(b'flags=0x10000(runtime)', b'flags=0x10000(runtime,adhoc)'),
    metadata().replace(b'flags=0x10000(runtime)', b'flags=bogus'),
    metadata().replace(b'Signature size=9000', b'Signature=adhoc'),
    metadata() + b'Signature=adhoc\n', metadata() + b'Signature size=9000\n',
    metadata().replace(b'Signature size=9000', b'Signature size=0'),
    metadata() + b'CodeDirectory flags=0x0(none)\n',
    b'\xff', b'X' * (64 * 1024 + 1), metadata() + b'\x00',
    metadata().replace(b'\n', b'\r\n'),
])
def test_malformed_or_cross_publisher_display_fails_closed(monkeypatch, executable, policy, raw):
    mock_codesign(monkeypatch, output=raw)
    with pytest.raises(trust.PublisherTrustError):
        trust.verify_publisher(executable, policy)


@pytest.mark.parametrize('fault', ['timeout', 'missing_tool', 'display_failure'])
def test_codesign_operational_errors_do_not_fall_back(monkeypatch, executable, policy, fault):
    monkeypatch.setattr(trust.sys, 'platform', 'darwin')

    def run(argv, **kwargs):
        if fault == 'timeout':
            raise subprocess.TimeoutExpired(argv, 30)
        if fault == 'missing_tool':
            raise FileNotFoundError()
        return subprocess.CompletedProcess(argv, 0 if '--verify' in argv else 1, b'', b'private output')

    monkeypatch.setattr(trust.subprocess, 'run', run)
    with pytest.raises(trust.PublisherTrustError) as error:
        trust.verify_publisher(executable, policy)
    assert 'private output' not in str(error.value)


@pytest.mark.parametrize('change', ['bytes', 'replacement', 'symlink'])
def test_publisher_verification_detects_candidate_changes(monkeypatch, executable, policy, change):
    def mutate():
        if change == 'bytes':
            executable.write_bytes(b'tampered')
        elif change == 'replacement':
            duplicate = executable.with_name('replacement')
            duplicate.write_bytes(executable.read_bytes())
            duplicate.replace(executable)
        else:
            target = executable.with_name('target')
            executable.rename(target)
            executable.symlink_to(target)

    mock_codesign(monkeypatch, during_display=mutate)
    with pytest.raises(trust.PublisherTrustError):
        trust.verify_publisher(executable, policy)


def test_non_macos_and_relative_candidate_rejected(monkeypatch, executable, policy):
    monkeypatch.setattr(trust.sys, 'platform', 'linux')
    with pytest.raises(trust.PublisherTrustError, match='macOS'):
        trust.verify_publisher(executable, policy)
    monkeypatch.setattr(trust.sys, 'platform', 'darwin')
    with pytest.raises(trust.PublisherTrustError, match='absolute'):
        trust.verify_publisher(Path('gitgo-host'), policy)


def test_upgrade_and_rollback_keep_publisher_but_allow_content_change(monkeypatch, executable, policy):
    mock_codesign(monkeypatch)
    first = trust.verify_publisher(executable, policy)
    executable.write_bytes(b'another same publisher version')
    second = trust.verify_publisher(executable, policy)
    assert first.executable_sha256 != second.executable_sha256
    trust.require_same_publisher(first, second)
    trust.require_same_publisher(second, first)
    for different in (replace(second, team_identifier='OTHER12345'),
                      replace(second, signing_identifier='org.gitgo.other')):
        with pytest.raises(trust.PublisherTrustError, match='trust migration'):
            trust.require_same_publisher(first, different)
        with pytest.raises(trust.PublisherTrustError, match='trust migration'):
            trust.require_same_publisher(different, first)
    with pytest.raises(trust.PublisherTrustError):
        trust.require_same_publisher(first.__dict__, second)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Native macOS requirement compiler')
def test_native_codesign_requirement_compiles_without_signing_credentials(tmp_path_factory, policy):
    compiled = tmp_path_factory / 'publisher.csreq'
    subprocess.run(['/usr/bin/csreq', '-r', '=' + trust.developer_id_requirement(policy),
                    '-b', str(compiled)], check=True, capture_output=True, timeout=30)
    assert compiled.is_file() and compiled.stat().st_size > 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='Native macOS rejection acceptance')
@pytest.mark.parametrize('kind', ['unsigned', 'adhoc', 'tampered'])
def test_native_unsigned_adhoc_and_tampered_files_never_get_publisher_identity(
        tmp_path_factory, policy, kind):
    path = tmp_path_factory / 'gitgo-host'
    shutil.copyfile('/usr/bin/false', path)
    path.chmod(0o755)
    if kind == 'unsigned':
        subprocess.run(['/usr/bin/codesign', '--remove-signature', str(path)],
                       check=True, capture_output=True, timeout=30)
    else:
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-',
                        '--identifier', IDENTIFIER, str(path)],
                       check=True, capture_output=True, timeout=30)
        # The ad-hoc fixture is internally intact, but cannot meet Developer ID.
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(path)],
                       check=True, capture_output=True, timeout=30)
        if kind == 'tampered':
            with path.open('r+b') as stream:
                stream.seek(256)
                original = stream.read(1)
                stream.seek(256)
                stream.write(bytes([original[0] ^ 1]))
    with pytest.raises(trust.PublisherTrustError, match='rejected'):
        trust.verify_publisher(path, policy)
