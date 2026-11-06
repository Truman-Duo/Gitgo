"""Release recovery with real files/processes and a disposable remote model.

The fake remote exercises response loss, NOT GitHub/CDN/native acceptance.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from scripts.release_control import assets, delivery
from scripts.release_control.checks import run_checks
from scripts.release_control.evidence import Evidence, atomic, file_fact
from scripts.release_control.github import GitHub
from scripts.release_control.downloads import acquire, unpack
from scripts.release_control.plan import check_source, git, load
from scripts.release_controller import run, main


@pytest.fixture
def release(tmp_path_factory):
    base = tmp_path_factory
    source = base/'source'
    (source/'packaging').mkdir(parents=True)
    atomic(source/'packaging/product.json', {'version': '1.0.0-alpha.9'})
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Release fixture',
        '-c', 'user.email=fixture@local.invalid', 'commit', '-qm', 'fixture'], check=True)
    notes = base/'notes.md'; notes.write_text('Disposable release')
    output = base/'check.txt'
    declaration = {'schema_version': 1, 'source': str(source), 'notes': str(notes),
        'release': {'repository': 'fixture/repository', 'version': '1.0.0-alpha.9', 'tag': 'v1.0.0-alpha.9',
            'commit': git(source, 'rev-parse', 'HEAD'), 'tree': git(source, 'rev-parse', 'HEAD^{tree}'),
            'channel': 'preview', 'sequence': 9},
        'signing': {'private_key': str(base/'test-only.pem'), 'key_id': 'ephemeral-test'},
        'ci': [{'run_id': 123, 'workflow': 'ci.yml', 'required_jobs': ['CI gate']}],
        'checks': [{'id': 'source-test', 'argv': [sys.executable, '-c',
            'from pathlib import Path; p=Path(' + repr(str(output)) + '); p.write_text("pass")'],
            'outputs': [str(output)]}], 'targets': {}}
    from backend.core.update.manifest import TARGETS
    for platform, architecture in sorted(TARGETS):
        target = platform + '-' + architecture
        path = base/(target + '.zip' if platform == 'windows' else target + '.tar.gz')
        path.write_bytes(('synthetic archive ' + target).encode())
        manifest = base/(target + '.baseline.json'); manifest.write_text('old manifest ' + target)
        signature = base/(target + '.baseline.sig'); signature.write_text('old signature ' + target)
        declaration['targets'][target] = {'archive': str(path), 'baseline_manifest': str(manifest),
            'baseline_signature': str(signature)}
    config = base/'config.json'; atomic(config, declaration)
    plan = load(config)
    evidence = Evidence(base/'evidence', plan)
    return plan, evidence, config


def synthetic_prepared(plan, evidence):
    """Wire fixtures for delivery tests, intentionally not signing acceptance."""
    directory = evidence.root/'assets'; directory.mkdir(exist_ok=True)
    facts = {}
    for name, target in plan['targets'].items():
        facts[target['archive']] = file_fact(target['archive'])
        for suffix in ('.manifest.json', '.manifest.sig'):
            path = directory/(name + suffix); path.write_text('new ' + name + suffix)
            facts[str(path)] = file_fact(path)
    return evidence.record('prepared', 'success', binding='fixture-only', assets=facts,
        inputs=assets.inputs(plan), manifests=[{'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}])


class Remote:
    """Persistent in-memory effects; optional failure AFTER an effect committed."""
    def __init__(self, plan):
        self.plan, self.tags, self.releases, self.files, self.writes = plan, {}, {}, {}, []
        self.next_id, self.fail, self.ci_failed = 1, None, False
        self.releases['updates-preview'] = {'id': 900, 'draft': False, 'tag_name': 'updates-preview'}
        self.files[900] = {}
        for name, target in plan['targets'].items():
            for suffix, key in (('.manifest.json', 'baseline_manifest'), ('.manifest.sig', 'baseline_signature')):
                self.put(900, name + suffix, file_fact(target[key]))

    def put(self, release_id, name, fact):
        self.next_id += 1
        self.files.setdefault(release_id, {})[name] = {'id': self.next_id, 'name': name,
            'state': 'uploaded', 'size': fact['bytes'], 'digest': 'sha256:' + fact['sha256']}

    def committed(self, name):
        self.writes.append(name)
        if self.fail and self.fail(name):
            self.fail = None
            raise RuntimeError('Injected lost response after commit')

    def source(self, release):
        assert all(release[k] == self.plan['release'][k] for k in ('commit', 'tree', 'repository'))

    def ci(self, declaration, release):
        if self.ci_failed:
            raise ValueError('CI rerun failed')
        return {'run_id': declaration['run_id'], 'attempt': 1}

    def tag(self, tag): return self.tags.get(tag)
    def release(self, tag): return deepcopy(self.releases.get(tag))
    def assets(self, row): return deepcopy(self.files.get(row['id'], {}))

    def api(self, path, method='GET', payload=None, absent=False):
        if path == 'git/refs' and method == 'POST':
            self.tags[payload['ref'].removeprefix('refs/tags/')] = payload['sha']
            self.committed('tag')
        elif path == 'releases' and method == 'POST':
            tag = payload['tag_name']
            assert tag not in self.releases
            self.releases[tag] = {'id': 901, **payload}
            self.files[901] = {}
            self.committed('draft')
        elif path == 'releases/901' and method == 'PATCH':
            self.releases[self.plan['release']['tag']].update(payload)
            self.committed('publish')
        elif path.startswith('releases/assets/') and method == 'DELETE':
            wanted = int(path.rsplit('/', 1)[1])
            for rows in self.files.values():
                for name, row in list(rows.items()):
                    if row['id'] == wanted:
                        del rows[name]
                        self.committed('delete:' + name)
                        return
            raise AssertionError('Already-deleted asset replayed')
        else:
            raise AssertionError((path, method))

    def upload(self, tag, path):
        release_id = self.releases[tag]['id']
        assert path.name not in self.files[release_id], 'Duplicate upload/clobber'
        self.put(release_id, path.name, file_fact(path))
        self.committed('upload:' + tag + ':' + path.name)


@pytest.mark.parametrize('point', ['tag', 'draft', 'release-upload', 'publish', 'feed-delete', 'feed-upload'])
def test_response_loss_resumes_missing_effects_without_replaying_committed_writes(release, point):
    plan, evidence, _ = release
    remote = Remote(plan)
    predicates = {'tag': lambda n: n == 'tag', 'draft': lambda n: n == 'draft',
        'release-upload': lambda n: n.startswith('upload:v'), 'publish': lambda n: n == 'publish',
        'feed-delete': lambda n: n.startswith('delete:'),
        'feed-upload': lambda n: n.startswith('upload:updates-')}
    remote.fail = predicates[point]
    with evidence.locked():
        prepared = synthetic_prepared(plan, evidence)
        with pytest.raises(RuntimeError, match='lost response'):
            row = delivery.draft(plan, evidence, remote, prepared)
            delivery.publish(plan, evidence, remote, prepared, row)
        row = delivery.draft(plan, evidence, remote, prepared)
        delivery.publish(plan, evidence, remote, prepared, row)
        writes = list(remote.writes)
        row = delivery.draft(plan, evidence, remote, prepared)
        delivery.publish(plan, evidence, remote, prepared, row)
        assert remote.writes == writes
        assert len(writes) == len(set(writes))
        assert evidence.read('published')['state'] == 'success'
        assert len(list((evidence.root/'history').glob('*.json'))) > len(writes)


def test_changed_feed_is_not_overwritten_or_rolled_back(release):
    plan, evidence, _ = release
    remote = Remote(plan)
    with evidence.locked():
        prepared = synthetic_prepared(plan, evidence)
        row = delivery.draft(plan, evidence, remote, prepared)
        remote.put(900, 'linux-x86_64.manifest.json', {'bytes': 100, 'sha256': 'a'*64})
        before = list(remote.writes)
        with pytest.raises(ValueError, match='outside this release'):
            delivery.advance_feeds(plan, evidence, remote, prepared)
        assert remote.writes == before
        assert not evidence.read('feeds')


@pytest.mark.parametrize('change', ['tag', 'asset', 'extra-asset', 'source', 'ci', 'archive'])
def test_old_success_never_authorizes_changed_release(release, change):
    plan, evidence, _ = release
    remote = Remote(plan)
    with evidence.locked():
        prepared = synthetic_prepared(plan, evidence)
        row = delivery.draft(plan, evidence, remote, prepared)
        if change == 'tag': remote.tags[plan['release']['tag']] = '0'*40
        elif change == 'asset':
            name = next(iter(remote.files[901])); remote.files[901][name]['digest'] = 'sha256:' + '0'*64
        elif change == 'extra-asset': remote.put(901, 'test.bat', {'bytes': 1, 'sha256': 'a'*64})
        elif change == 'source': (Path(plan['source'])/'untracked.txt').write_text('dirty')
        elif change == 'ci': remote.ci_failed = True
        elif change == 'archive': Path(next(iter(plan['targets'].values()))['archive']).write_bytes(b'changed')
        before = list(remote.writes)
        with pytest.raises(ValueError):
            row = delivery.draft(plan, evidence, remote, prepared)
            delivery.publish(plan, evidence, remote, prepared, row)
        assert remote.writes == before


def test_local_check_reuses_evidence_but_reexecutes_when_input_or_output_changes(release):
    plan, evidence, _ = release
    step = plan['checks'][0]
    external = evidence.root.parent/'external.txt'; external.write_text('first')
    step['inputs'] = [str(external)]
    with evidence.locked():
        first = run_checks(plan, evidence, 2)['source-test']
        assert run_checks(plan, evidence, 2)['source-test'] == first
        external.write_text('second')
        second = run_checks(plan, evidence, 2)['source-test']
        assert second['time'] != first['time']
        Path(step['outputs'][0]).write_text('corrupted evidence')
        third = run_checks(plan, evidence, 2)['source-test']
        assert third['time'] != second['time']


def test_independent_checks_really_overlap_and_dependents_wait(release):
    plan, evidence, _ = release
    base = evidence.root.parent
    def step(name, other):
        started, peer, output = base/(name + '.started'), base/(other + '.started'), base/(name + '.result')
        program = ('import time; from pathlib import Path; '
            f'Path({str(started)!r}).write_text("started"); end=time.monotonic()+10\n'
            f'while not Path({str(peer)!r}).exists():\n'
            ' if time.monotonic()>end: raise SystemExit(2)\n time.sleep(.01)\n'
            f'Path({str(output)!r}).write_text("done")')
        return {'id': name, 'argv': [sys.executable, '-c', program], 'cwd': plan['source'],
                'inputs': [], 'outputs': [str(output)], 'timeout': 15}
    plan['checks'] = [step('left', 'right'), step('right', 'left')]
    with evidence.locked():
        assert set(run_checks(plan, evidence, 2)) == {'left', 'right'}


def test_failed_check_retains_successful_sibling_and_blocks_dependent(release):
    plan, evidence, _ = release
    good = plan['checks'][0]
    bad = {**good, 'id': 'bad', 'argv': [sys.executable, '-c', 'raise SystemExit(3)']}
    blocked = {**good, 'id': 'blocked', 'needs': ['bad']}
    plan['checks'] = [good, bad, blocked]
    with evidence.locked():
        with pytest.raises(ValueError, match='failed'):
            run_checks(plan, evidence, 3)
        assert evidence.read('check-source-test')['state'] == 'success'
        assert evidence.read('check-bad')['state'] == 'failed'
        assert evidence.read('check-blocked') is None


def test_ready_dependency_can_start_without_waiting_for_an_unrelated_long_check(release):
    plan, evidence, _ = release
    base = evidence.root.parent
    left_started, right_done, dependent_done = [base/n for n in ('left-started', 'right-done', 'dependent-done')]
    left = ('import time; from pathlib import Path; '
        f'Path({str(left_started)!r}).write_text("start"); deadline=time.monotonic()+10\n'
        f'while not Path({str(dependent_done)!r}).exists():\n'
        ' if time.monotonic()>deadline: raise SystemExit(2)\n time.sleep(.01)')
    make = lambda name, argv, outputs, needs=[]: {'id': name, 'argv': [sys.executable, '-c', argv],
        'outputs': [str(p) for p in outputs], 'inputs': [], 'needs': needs, 'cwd': plan['source'], 'timeout': 15}
    plan['checks'] = [make('long', left, [left_started]),
        make('short', f'from pathlib import Path; Path({str(right_done)!r}).write_text("done")', [right_done]),
        make('next', f'from pathlib import Path; Path({str(dependent_done)!r}).write_text("done")', [dependent_done], ['short'])]
    with evidence.locked():
        assert len(run_checks(plan, evidence, 2)) == 3


def test_real_process_exit_releases_lock_and_retains_intent(release):
    plan, evidence, _ = release
    root = Path(__file__).resolve().parents[1]
    program = ('import json, os, sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
        'from scripts.release_control.evidence import Evidence; '
        'e=Evidence(Path(sys.argv[2]), json.loads(sys.argv[3])); '
        '\nwith e.locked():\n e.record("crash", "intent"); os._exit(17)')
    result = subprocess.run([sys.executable, '-c', program, str(root), str(evidence.root), json.dumps(plan)])
    assert result.returncode == 17
    with evidence.locked():
        assert evidence.read('crash')['state'] == 'intent'
        with pytest.raises(ValueError, match='Another release controller'):
            with Evidence(evidence.root, plan).locked(): pass


def test_plan_drift_and_graph_cycle_cannot_reuse_evidence(release):
    plan, evidence, config = release
    with evidence.locked(): pass
    changed = deepcopy(plan); changed['release']['sequence'] += 1
    with pytest.raises(ValueError, match='Plan changed'):
        with Evidence(evidence.root, changed).locked(): pass
    declaration = json.loads(config.read_bytes())
    declaration['checks'][0]['needs'] = ['missing']
    atomic(config, declaration)
    with pytest.raises(ValueError, match='cycle or missing'):
        load(config)


def test_preparation_default_cannot_publish_and_public_verification_checks_all_bytes(release, monkeypatch):
    plan, evidence, _ = release
    remote = Remote(plan)
    monkeypatch.setattr(assets, 'prepare', synthetic_prepared)
    assert run(plan, evidence, remote)['phase'] == 'prepared'
    assert remote.writes == []
    with evidence.locked():
        prepared = evidence.read('prepared')
        seen = []
        def fetch(url, fact): seen.append(url); return fact
        result = delivery.verify_public(plan, evidence, prepared, fetch)
        assert len(seen) == len(prepared['assets']) + 8
        assert not result['native_managed_install_verified']
        def corrupt(url, fact): return {**fact, 'sha256': '0'*64}
        with pytest.raises(ValueError, match='Anonymous public download mismatch'):
            delivery.verify_public(plan, evidence, prepared, corrupt)


def test_ci_gateway_rejects_wrong_source_workflow_job_and_current_attempt(release):
    plan, _, _ = release
    class Runs(GitHub):
        def __init__(self):
            super().__init__(plan['release']['repository'])
            self.run = {'head_sha': plan['release']['commit'], 'path': '.github/workflows/ci.yml',
                'status': 'completed', 'conclusion': 'success', 'run_attempt': 2,
                'repository': {'full_name': self.repository}}
            self.jobs = [{'name': 'CI gate', 'conclusion': 'success'}]
        def api(self, *args, **kwargs): return deepcopy(self.run)
        def pages(self, path, field):
            assert '/attempts/2/jobs' in path
            return self.jobs
    remote = Runs()
    assert remote.ci(plan['ci'][0], plan['release'])['attempt'] == 2
    for key, value in [('head_sha', '0'*40), ('path', '.github/workflows/other.yml'),
                       ('status', 'queued'), ('conclusion', 'failure')]:
        original = remote.run[key]; remote.run[key] = value
        with pytest.raises(ValueError): remote.ci(plan['ci'][0], plan['release'])
        remote.run[key] = original
    remote.jobs = [{'name': 'CI gate', 'conclusion': 'skipped'}]
    with pytest.raises(ValueError): remote.ci(plan['ci'][0], plan['release'])


def test_plan_and_status_are_read_only_and_expired_signatures_are_not_reused(release, capsys):
    plan, evidence, config = release
    assert main(['plan', '--plan', str(config)]) == 0
    assert not evidence.root.joinpath('plan.json').exists()
    assert main(['status', '--plan', str(config), '--evidence', str(evidence.root)]) == 0
    assert not evidence.root.joinpath('plan.json').exists()
    with pytest.raises(ValueError, match='expired'):
        assets.validate_fresh({'manifests': [{'expires_at': '2000-01-01T00:00:00Z'}]})


def test_artifact_download_binds_remote_run_and_digest_and_reuses_verified_bytes(release):
    import zipfile
    plan, evidence, _ = release
    seed = evidence.root.parent/'transport.zip'
    with zipfile.ZipFile(seed, 'w') as bundle: bundle.writestr('package.tar.gz', b'synthetic package')
    plan['artifacts'] = [{'artifact_id': 42, 'run_id': 123, 'name': 'native-fixture',
                          'output': str(evidence.root.parent/'download')}]
    fact = file_fact(seed)
    class Download(Remote):
        count, corrupt, wrong_head = 0, False, False
        def api(self, *args):
            return {'id': 42, 'name': 'native-fixture', 'expired': False,
                'workflow_run': {'id': 123, 'head_sha': '0'*40 if self.wrong_head else plan['release']['commit']},
                'size_in_bytes': fact['bytes'], 'digest': 'sha256:' + fact['sha256']}
        def download_artifact(self, artifact_id, output, maximum):
            self.count += 1
            output.write_bytes(b'corrupt' if self.corrupt else seed.read_bytes())
    remote = Download(plan)
    with evidence.locked():
        first = acquire(plan, evidence, remote, 2)
        assert acquire(plan, evidence, remote, 2) == first
        assert remote.count == 1
        # Missing extracted output is reconstructed from the verified outer ZIP.
        Path(next(iter(first[0]['outputs']))).unlink()
        acquire(plan, evidence, remote, 2)
        assert remote.count == 1
        remote.wrong_head = True
        with pytest.raises(ValueError, match='another source'):
            acquire(plan, evidence, remote, 2)
        remote.wrong_head = False
        (evidence.root/'artifact-42.zip').write_bytes(b'corrupted download')
        remote.corrupt = True
        with pytest.raises(ValueError, match='SHA-256 mismatch'):
            acquire(plan, evidence, remote, 2)


def test_archive_changed_between_acquisition_and_signing_is_rejected(release):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    plan, previous, _ = release
    plan['artifacts'] = [{'artifact_id': 42}]
    evidence = Evidence(previous.root, plan)
    key = Ed25519PrivateKey.generate()
    Path(plan['signing']['private_key']).write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    with evidence.locked():
        outputs = {t['archive']: file_fact(t['archive']) for t in plan['targets'].values()}
        evidence.record('artifact-42', 'success', outputs=outputs)
        Path(next(iter(outputs))).write_bytes(b'changed after CI download')
        with pytest.raises(ValueError, match='changed after verified CI acquisition'):
            assets.prepare(plan, evidence)


@pytest.mark.parametrize('name', ['../outside', 'nested/file', 'C:escape', 'duplicate'])
def test_outer_artifact_cannot_escape_or_overwrite_colliding_names(release, name):
    import zipfile
    plan, evidence, _ = release
    archive = evidence.root.parent/'bad.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('valid', b'valid')
        bundle.writestr(name, b'bad')
        if name == 'duplicate': bundle.writestr('DUPLICATE', b'collision')
    with pytest.raises(Exception):
        unpack(archive, evidence.root.parent/'unpack')
    assert not (evidence.root.parent/'outside').exists()
    assert not (evidence.root.parent/'unpack/valid').exists()


def test_init_discovers_matrix_and_refuses_missing_artifact_or_plan_overwrite(release):
    from scripts.release_control.plan import create
    plan, evidence, _ = release
    class Discovery(Remote):
        missing = False
        def pages(self, path, field):
            result = [{'id': n+1, 'expired': False, 'name': 'windows-final-zip' if name.startswith('windows-')
                else 'native-' + name + '-' + plan['release']['commit']}
                for n, name in enumerate(plan['targets'])]
            return result[:-1] if self.missing else result
    remote = Discovery(plan)
    path = evidence.root.parent/'generated-plan.json'
    def generate():
        return create(path, plan['source'], plan['release']['repository'], 10, plan['signing']['key_id'],
            plan['signing']['private_key'], 123, evidence.root.parent, plan['notes'], remote)
    remote.missing = True
    with pytest.raises(ValueError, match='exactly one'):
        generate()
    assert not path.exists()
    remote.missing = False
    generated = generate()
    assert len(load(path)['artifacts']) == len(generated['targets']) == 4
    assert not remote.writes
    with pytest.raises(ValueError, match='overwrite'):
        generate()


@pytest.mark.parametrize('link', ['../../outside', '/absolute', 'missing'])
def test_cross_target_archive_reader_reuses_link_rejection_before_any_materialization(release, link):
    import tarfile
    import io
    from scripts.release_control.inspection import ArchiveEvidence
    plan, evidence, _ = release
    archive = evidence.root.parent/'links.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        root = tarfile.TarInfo('payload'); root.type = tarfile.DIRTYPE; bundle.addfile(root)
        target = tarfile.TarInfo('payload/target'); target.size = 1; bundle.addfile(target, io.BytesIO(b'x'))
        alias = tarfile.TarInfo('payload/alias'); alias.type = tarfile.SYMTYPE; alias.linkname = link
        bundle.addfile(alias)
    with pytest.raises(Exception):
        ArchiveEvidence(archive, evidence.root)
    assert not (evidence.root/'payload').exists()


def test_real_four_target_extraction_inventory_contract_signatures_and_resume(release):
    """Real crypto/contracts on synthetic bundles; no native runtime claim."""
    import base64
    import tarfile
    import zipfile
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    from backend.core.update.manifest import ManifestVerifier
    from scripts.prepare_update_release import github_policy
    from tests.test_update_transaction import build_fixture, refresh_build, compatibility_wire
    from tests.test_update_manifest import manifest_wire, sign_raw
    plan, evidence, _ = release
    key = Ed25519PrivateKey.generate()
    Path(plan['signing']['private_key']).write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    verifier = ManifestVerifier({'ephemeral-test': public})
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for name, target in plan['targets'].items():
        platform, architecture = name.split('-', 1)
        stage = evidence.root.parent/(name + '-stage')
        build_fixture(stage, version=plan['release']['version'], platform=platform, architecture=architecture)
        policy = github_policy(public, 'ephemeral-test', platform, architecture, plan['release']['repository'])
        atomic(stage/'release-trust.json', policy)
        refresh_build(stage, version=plan['release']['version'], platform=platform, architecture=architecture)
        build = json.loads((stage/'build-manifest.json').read_bytes())
        build['source'] = {k: plan['release'][k] for k in ('commit', 'tree')} | {'dirty': False}
        atomic(stage/'build-manifest.json', build)
        archive = Path(target['archive'])
        if platform == 'macos':
            # Virtual tar link: Windows should verify it without symlink rights.
            build['files'].append({'path': 'gitgo-alias', 'link': 'gitgo'})
            atomic(stage/'build-manifest.json', build)
        if platform == 'windows':
            with zipfile.ZipFile(archive, 'w') as bundle:
                for path in stage.rglob('*'):
                    if path.is_file(): bundle.write(path, path.relative_to(stage).as_posix())
        else:
            def modes(info):
                info.mode = 0o700 if info.isdir() or info.name.removeprefix(name + '/') in {
                    'gitgo', 'internal/gitgo-host/gitgo-host', 'install.sh', 'uninstall.sh'} else 0o600
                return info
            with tarfile.open(archive, 'w:gz') as bundle:
                bundle.add(stage, arcname=name, filter=modes)
                if platform == 'macos':
                    link = tarfile.TarInfo(name + '/gitgo-alias'); link.type = tarfile.SYMTYPE
                    link.linkname = 'gitgo'; bundle.addfile(link)
        baseline = manifest_wire(platform=platform, architecture=architecture,
            archive_format='zip' if platform == 'windows' else 'tar.gz', sequence=8,
            version='1.0.0-alpha.8', channel='preview', published_at=(now-timedelta(days=2)).isoformat().replace('+00:00','Z'),
            expires_at=(now-timedelta(days=1)).isoformat().replace('+00:00','Z'),
            minimum_updater_version='0.2.0', data_compatibility=compatibility_wire())
        raw = json.dumps(baseline).encode()
        Path(target['baseline_manifest']).write_bytes(raw)
        Path(target['baseline_signature']).write_bytes(sign_raw(raw, key))
    remote = Remote(plan)
    result = run(plan, evidence, remote)
    assert result == {'phase': 'prepared', 'remote_writes': False}
    first = evidence.read('prepared')
    assert len(first['assets']) == 17
    for target in plan['targets']:
        raw = (evidence.root/'assets'/(target + '.manifest.json')).read_bytes()
        sig = (evidence.root/'assets'/(target + '.manifest.sig')).read_bytes()
        manifest = verifier.verify(raw, sig)
        assert manifest.source_commit == plan['release']['commit']
        assert manifest.sequence == 9
    run(plan, evidence, remote)
    assert evidence.read('prepared') == first
    assert not remote.writes
    with evidence.locked():
        row = delivery.draft(plan, evidence, remote, first)
        delivery.publish(plan, evidence, remote, first, row)
    writes = list(remote.writes)
    run(plan, evidence, remote, through='published')
    assert remote.writes == writes
