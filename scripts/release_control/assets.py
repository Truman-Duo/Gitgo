"""Reuse the updater's extraction, inventory, contract and signing validators."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from .evidence import atomic, atomic_bytes, file_fact, digest


def inputs(plan):
    return {plan['notes']: file_fact(plan['notes'])} | {path: file_fact(path) for target in plan['targets'].values()
            for path in (target['archive'], target['baseline_manifest'], target['baseline_signature'])}


def prepare(plan, evidence):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key, Encoding, PublicFormat
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from .inspection import ArchiveEvidence
    from scripts.prepare_update_release import prepare as sign
    key = load_pem_private_key(Path(plan['signing']['private_key']).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('Expected Ed25519 publisher key')
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    before = inputs(plan)
    admitted = {}
    for artifact in plan.get('artifacts', []):
        receipt = evidence.read('artifact-' + str(artifact['artifact_id']))
        if not receipt or receipt['state'] != 'success':
            raise ValueError('CI artifact acquisition evidence missing')
        admitted.update(receipt['outputs'])
    if plan.get('artifacts') and any(admitted.get(t['archive']) != before[t['archive']]
                                    for t in plan['targets'].values()):
        raise ValueError('Release archive changed after verified CI acquisition')
    binding = digest({'inputs': before, 'public_key': public.hex(), 'notes': file_fact(plan['notes'])})
    previous = evidence.read('prepared')
    if previous and previous.get('binding') == binding and previous['state'] == 'success':
        try:
            intact = all(file_fact(p) == fact for p, fact in previous['assets'].items())
        except ValueError:
            intact = False
        if intact:
            validate_fresh(previous)
            evidence.record('reuse-prepared', 'reused', binding=binding)
            return previous
    # A published/drafted artifact set is immutable, even if local input changed.
    if evidence.read('remote-draft') or evidence.read('remote-publish'):
        raise ValueError('Prepared inputs changed after delivery began; refusing to replace signed assets')
    evidence.record('prepared', 'running', binding=binding)
    generated = evidence.root / 'assets'
    generated.mkdir(exist_ok=True)
    assets, manifests = {}, []
    for name, target in sorted(plan['targets'].items()):
        archive = Path(target['archive'])
        if not archive.name.endswith('.zip' if name.startswith('windows-') else '.tar.gz'):
            raise ValueError('Archive format does not match release target')
        with tempfile.TemporaryDirectory(prefix='extract-', dir=evidence.root) as raw:
            work = Path(raw)
            inspected = ArchiveEvidence(archive, work)
            build = inspected.metadata('build-manifest.json')
            product = inspected.metadata('product.json')
            identity = plan['release']
            if (build['source'] != {k: identity[k] for k in ('commit', 'tree')} | {'dirty': False}
                    or product['version'] != identity['version']
                    or product['platform'] + '-' + product['architecture'] != name):
                raise ValueError('Archive source, target or version mismatch: ' + name)
            if any(Path(f['path']).suffix.lower() in {'.bat', '.cmd'} for f in build['files']):
                raise ValueError('Development batch launchers cannot enter release archives')
            manifest, signature, _ = sign(None, archive, key, plan['signing']['key_id'],
                identity['channel'], identity['sequence'], identity['repository'], identity['tag'],
                baseline_manifest=Path(target['baseline_manifest']).read_bytes(),
                baseline_signature=Path(target['baseline_signature']).read_bytes(),
                upgrade_route=target.get('upgrade_route', 'direct'), archive_evidence=inspected)
            for suffix, data in (('.manifest.json', manifest), ('.manifest.sig', signature)):
                path = generated/(name + suffix)
                # Atomic writes also for exact signed wire bytes (not re-encoded).
                atomic_bytes(path, data)
                assets[str(path)] = file_fact(path)
            assets[str(archive)] = file_fact(archive)
            checksum = generated/(archive.name + '.sha256')
            atomic_bytes(checksum, (assets[str(archive)]['sha256'] + '  ' + archive.name + '\n').encode('ascii'))
            assets[str(checksum)] = file_fact(checksum)
            manifests.append(json.loads(manifest))
    if inputs(plan) != before:
        raise ValueError('Archive/baseline changed during signing')
    summary = generated/'validation-summary.json'
    atomic(summary, {'schema_version': 1, 'release': plan['release'],
        'targets': sorted(plan['targets']), 'inventory_and_contract_verified': True,
        'ci_artifact_transport_verified': bool(plan.get('artifacts')),
        'formal_terminal_verified': False, 'future_upgrade_verified': False,
        'ci': evidence.read('ci')['runs'] if evidence.read('ci') else []})
    assets[str(summary)] = file_fact(summary)
    if len({Path(p).name for p in assets}) != len(assets):
        raise ValueError('Release assets have colliding names')
    value = evidence.record('prepared', 'success', binding=binding, assets=assets,
        manifests=manifests, inputs=before)
    validate_fresh(value)
    return value


def validate_fresh(prepared):
    for manifest in prepared['manifests']:
        if datetime.fromisoformat(manifest['expires_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            raise ValueError('Signed manifest expired; prepare a reviewed new release plan, never reuse stale success')
