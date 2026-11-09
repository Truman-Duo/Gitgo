"""Parameterised release preparation/delivery; defaults to local preparation only."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_control import assets, delivery
from scripts.release_control.checks import run_checks
from scripts.release_control.downloads import acquire
from scripts.release_control.evidence import Evidence, atomic
from scripts.release_control.github import GitHub
from scripts.release_control.plan import check_source, load, create


def run(plan, evidence, remote, through='prepared', workers=4):
    if through not in ('prepared', 'draft', 'published', 'verified') or not 1 <= workers <= 16:
        raise ValueError('Invalid release phase or concurrency')
    with evidence.locked():
        started = time.monotonic()
        evidence.record('run', 'running', through=through)
        try:
            result = advance(plan, evidence, remote, through, workers)
        except BaseException as exc:
            evidence.record('run', 'failed', through=through, error=type(exc).__name__, seconds=time.monotonic()-started)
            raise
        evidence.record('run', 'success', result=result, seconds=time.monotonic()-started)
        return result


def advance(plan, evidence, remote, through, workers):
    check_source(plan)
    delivery.ci_gate(plan, evidence, remote)
    acquire(plan, evidence, remote, workers)
    run_checks(plan, evidence, workers)
    prepared = assets.prepare(plan, evidence)
    delivery.guard(plan, prepared)
    if through == 'prepared':
        return {'phase': 'prepared', 'remote_writes': False}
    row = delivery.draft(plan, evidence, remote, prepared)
    if through == 'draft':
        return {'phase': 'draft', 'release_id': row['id']}
    delivery.publish(plan, evidence, remote, prepared, row)
    if through == 'verified':
        delivery.verify_public(plan, evidence, prepared)
    return {'phase': through, 'release_id': row['id'],
            'native_managed_install_verified': False, 'formal_terminal_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'plan', 'status', 'preflight', 'run'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--through', choices=('prepared', 'draft', 'published', 'verified'), default='prepared')
    parser.add_argument('--workers', type=int, default=4)
    # A JSON argv prefix supports an existing authenticated gh launcher without
    # putting credentials, shell quoting or platform-specific code in the plan.
    parser.add_argument('--gh-command', default='["gh"]')
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--repository')
    parser.add_argument('--sequence', type=int)
    parser.add_argument('--key-id')
    parser.add_argument('--private-key', type=Path)
    parser.add_argument('--ci-run', type=int)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--notes', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        command = json.loads(args.gh_command)
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError('Expected a GitHub CLI argv prefix')
        if args.command == 'init':
            if any(getattr(args, key) is None for key in ('repository', 'sequence', 'key_id', 'private_key', 'ci_run', 'baseline', 'notes')):
                raise ValueError('init requires repository, sequence, key-id, private-key, ci-run, baseline and notes')
            create(args.plan, args.source, args.repository, args.sequence, args.key_id, args.private_key,
                args.ci_run, args.baseline, args.notes, GitHub(args.repository, command))
            print('Release plan created. No remote changes made.')
            return 0
        plan = load(args.plan)
        if args.command == 'preflight':
            if args.report is None:
                raise ValueError('preflight requires --report')
            from scripts.verify_release_privacy import scan_repository
            from scripts.release_artifacts import validate_versions
            check_source(plan)
            validate_versions(Path(plan['source']))
            findings = scan_repository(Path(plan['source']))
            if findings:
                raise ValueError('Release privacy check failed; inspect the privacy scanner locally')
            atomic(args.report, {'release': plan['release'], 'versions_verified': True, 'privacy_verified': True})
            return 0
        if args.command == 'plan':
            print(json.dumps({'release': plan['release'], 'targets': sorted(plan['targets']),
                'checks': [{k: s[k] for k in ('id', 'needs') if k in s} for s in plan['checks']],
                'default_phase': 'prepared', 'remote_writes': False}, indent=2))
            return 0
        if args.evidence is None:
            raise ValueError('--evidence is required for status/run')
        if args.command == 'status':
            # Read-only: do not create evidence or present a stored receipt as a
            # fresh external observation. run always re-observes CI and delivery.
            values = {p.stem: json.loads(p.read_bytes()) for p in args.evidence.glob('*.json') if p.name != 'plan.json'}
            print(json.dumps({'recorded_only': True, 'receipts': values}, indent=2))
            return 0
        print(json.dumps(run(plan, Evidence(args.evidence, plan),
            GitHub(plan['release']['repository'], command), args.through, args.workers), indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        print('Release controller stopped: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
