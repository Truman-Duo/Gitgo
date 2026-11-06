"""Resolve one explicit source, version, channel, target matrix and recipe DAG."""
import json
from pathlib import Path
import re
import subprocess
import sys


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()


def check_source(plan):
    source = Path(plan['source'])
    identity = plan['release']
    if (git(source, 'rev-parse', 'HEAD') != identity['commit'] or
            git(source, 'rev-parse', 'HEAD^{tree}') != identity['tree'] or
            git(source, 'status', '--porcelain', '--untracked-files=normal')):
        raise ValueError('Release source changed or is dirty')
    product = json.loads((source / 'packaging/product.json').read_bytes())
    if product['version'] != identity['version']:
        raise ValueError('Release version differs from committed product')


def load(path):
    path = Path(path).resolve(strict=True)
    plan = json.loads(path.read_bytes())
    if plan.get('schema_version') != 1:
        raise ValueError('Unsupported release plan')
    def resolve(raw):
        return str((path.parent / raw).resolve())
    plan['source'] = resolve(plan['source'])
    release = plan['release']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', release['repository']):
        raise ValueError('Expected explicit GitHub owner/repository')
    for field in ('commit', 'tree'):
        if not re.fullmatch('[0-9a-f]{40}', release[field]):
            raise ValueError('Expected exact source commit and tree')
    from backend.core.update.version import Version
    version = Version.parse(release['version'])
    if (release['tag'] != 'v' + release['version'] or release['channel'] not in ('stable', 'preview')
            or (release['channel'] == 'preview') != bool(version.prerelease)
            or type(release['sequence']) is not int or release['sequence'] < 1):
        raise ValueError('Version, tag, channel or sequence mismatch')
    plan['notes'] = resolve(plan['notes'])
    plan['signing']['private_key'] = resolve(plan['signing']['private_key'])
    if Path(plan['signing']['private_key']).is_relative_to(plan['source']):
        raise ValueError('Private signing key must remain outside source')
    outputs = set()
    for artifact in plan.get('artifacts', []):
        if type(artifact['artifact_id']) is not int or artifact['artifact_id'] < 1:
            raise ValueError('Expected immutable CI artifact id')
        artifact['output'] = resolve(artifact['output'])
        if artifact['output'] in outputs or Path(artifact['output']).is_relative_to(plan['source']):
            raise ValueError('CI artifacts need distinct directories outside the source checkout')
        outputs.add(artifact['output'])
    from backend.core.update.manifest import TARGETS
    targets = plan['targets']
    if len(targets) != len(set(targets)) or set(targets) != {p + '-' + a for p, a in TARGETS}:
        raise ValueError('Plan must cover the complete supported release target matrix')
    for target in targets.values():
        for key in ('archive', 'baseline_manifest', 'baseline_signature'):
            target[key] = resolve(target[key])
        if target.get('upgrade_route', 'direct') not in ('direct', 'legacy-repair'):
            raise ValueError('Unknown update route')
    names = set()
    check_outputs = set()
    for step in plan['checks']:
        name = step['id']
        if not re.fullmatch('[a-z][a-z0-9-]{0,63}', name) or name in names:
            raise ValueError('Invalid or duplicate check id')
        names.add(name)
        step['cwd'] = resolve(step.get('cwd', plan['source']))
        if not step.get('argv') or not all(isinstance(x, str) and x for x in step['argv']):
            raise ValueError('Check needs an argv array; shell strings are not supported')
        if type(step.get('timeout', 1800)) is not int or not 1 <= step.get('timeout', 1800) <= 7200:
            raise ValueError('Checks require bounded timeouts')
        for field in ('inputs', 'outputs'):
            step[field] = [resolve(x) for x in step.get(field, [])]
        if not step['outputs']:
            raise ValueError('Checks need durable output evidence')
        if check_outputs.intersection(step['outputs']):
            raise ValueError('Checks cannot own the same output')
        check_outputs.update(step['outputs'])
    if not names or not plan.get('ci'):
        raise ValueError('Release requires local checks and exact-source CI evidence')
    complete = set()
    while len(complete) < len(names):
        ready = {s['id'] for s in plan['checks'] if s['id'] not in complete and set(s.get('needs', [])) <= complete}
        if not ready:
            raise ValueError('Check dependency cycle or missing dependency')
        complete |= ready
    for ci in plan['ci']:
        if type(ci['run_id']) is not int or ci['run_id'] < 1 or not ci.get('required_jobs'):
            raise ValueError('CI evidence requires run id and explicit required job names')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+\.yml', ci['workflow']):
            raise ValueError('Expected workflow filename')
    return plan


def create(path, source, repository, sequence, key_id, private_key, ci_run, baseline, notes, remote):
    """Read-only discovery produces a ready-to-review plan, not a release."""
    from backend.core.update.manifest import TARGETS
    from .evidence import atomic
    source, path = Path(source).resolve(), Path(path).resolve()
    if path.exists():
        raise ValueError('Refusing to overwrite an existing release plan')
    product = json.loads((source/'packaging/product.json').read_bytes())
    commit, tree = git(source, 'rev-parse', 'HEAD'), git(source, 'rev-parse', 'HEAD^{tree}')
    from backend.core.update.version import Version
    channel = 'preview' if Version.parse(product['version']).prerelease else 'stable'
    value = {'schema_version': 1, 'source': str(source), 'notes': str(Path(notes).resolve()),
        'release': {'repository': repository, 'version': product['version'], 'tag': 'v' + product['version'],
            'commit': commit, 'tree': tree, 'channel': channel, 'sequence': sequence},
        'signing': {'private_key': str(Path(private_key).resolve()), 'key_id': key_id},
        'ci': [{'run_id': ci_run, 'workflow': 'ci.yml', 'required_jobs': ['CI gate']}],
        'checks': [{'id': 'local-preflight', 'cwd': str(source),
            'argv': [sys.executable, '-B', str(source/'scripts/release_controller.py'), 'preflight',
                '--plan', str(path), '--report', str(path.parent/'local-preflight.json')],
            'inputs': [str(Path(notes).resolve()), str(source/'packaging/requirements-build.lock')],
            'outputs': [str(path.parent/'local-preflight.json')]}],
        'targets': {}, 'artifacts': []}
    check_source(value)
    remote.ci(value['ci'][0], value['release'])
    remote.source(value['release'])
    found = remote.pages(f'actions/runs/{ci_run}/artifacts', 'artifacts')
    for platform, architecture in sorted(TARGETS):
        target = platform + '-' + architecture
        expected = 'windows-final-zip' if platform == 'windows' else f'native-{target}-{commit}'
        matches = [r for r in found if r['name'] == expected and not r['expired']]
        if len(matches) != 1:
            raise ValueError('Expected exactly one current CI artifact: ' + expected)
        output = path.parent/'downloads'/target
        name = (f'gitgo-{product["version"]}-win-x64-{commit[:8]}.zip' if platform == 'windows'
                else 'gitgo-' + target + '.tar.gz')
        value['artifacts'].append({'artifact_id': matches[0]['id'], 'name': expected,
            'run_id': ci_run, 'output': str(output)})
        value['targets'][target] = {'archive': str(output/name),
            'baseline_manifest': str(Path(baseline).resolve()/(target + '.manifest.json')),
            'baseline_signature': str(Path(baseline).resolve()/(target + '.manifest.sig'))}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic(path, value)
    return value
