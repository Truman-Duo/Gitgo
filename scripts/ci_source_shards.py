"""File-preserving pytest shards and a fail-closed same-run coverage join.

No historical success is cached. Every shard collects the complete current
suite, runs whole files, and records what reached teardown. The join admits
only complete, disjoint executions of the same collection and source identity.
"""
import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

_RUN = None


def assignment(nodeids, count):
    if not 1 <= count <= 32 or not nodeids or len(nodeids) != len(set(nodeids)):
        raise ValueError('Invalid complete source collection')
    files = Counter(node.split('::', 1)[0] for node in nodeids)
    loads, result = [0] * count, {}
    for name in sorted(files, key=lambda name: (-files[name], name)):
        index = min(range(count), key=lambda index: (loads[index], index))
        result[name] = index
        loads[index] += files[name]
    if any(load == 0 for load in loads):
        raise ValueError('Every configured shard must have tests')
    return result


def pytest_addoption(parser):
    parser.addoption('--source-shard-count', type=int, default=1)
    parser.addoption('--source-shard-index', type=int, default=0)
    parser.addoption('--source-shard-report')


def pytest_sessionstart(session):
    global _RUN
    _RUN = None


def pytest_collection_modifyitems(config, items):
    global _RUN
    count, index = config.getoption('--source-shard-count'), config.getoption('--source-shard-index')
    if not 0 <= index < count or config.args != ['tests'] or config.option.keyword or config.option.markexpr:
        raise ValueError('Source shards require the unfiltered tests directory')
    all_ids = sorted(item.nodeid for item in items)
    plan = assignment(all_ids, count)
    selected = [item for item in items if plan[item.nodeid.split('::', 1)[0]] == index]
    removed = [item for item in items if plan[item.nodeid.split('::', 1)[0]] != index]
    directory = config.invocation_params.dir
    git = lambda *args: subprocess.check_output(['git', *args], cwd=directory, text=True).strip()
    lock = Path(directory) / 'packaging/requirements-build.lock'
    _RUN = {'schema_version': 1, 'count': count, 'index': index,
        'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'),
        'run_id': os.environ.get('GITHUB_RUN_ID', 'local'),
        'attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
        'environment': {'platform': sys.platform, 'python': list(sys.version_info[:3]),
            'pytest': importlib.metadata.version('pytest'),
            'build_lock': hashlib.sha256(lock.read_bytes()).hexdigest()},
        'collected': all_ids, 'selected': sorted(item.nodeid for item in selected), 'executed': set()}
    config.hook.pytest_deselected(items=removed)
    items[:] = selected


def pytest_runtest_logreport(report):
    if _RUN is not None and report.when == 'teardown':
        _RUN['executed'].add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _RUN is None:
        return
    path = session.config.getoption('--source-shard-report')
    if path:
        value = {**_RUN, 'executed': sorted(_RUN['executed']), 'exit_code': int(exitstatus)}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=True), encoding='utf-8')


def verify(records, *, commit, tree, run_id, count):
    latest = {}
    for record in records:
        if (record.get('schema_version') != 1 or record.get('commit') != commit
                or record.get('tree') != tree or record.get('run_id') != run_id
                or record.get('count') != count or type(record.get('index')) is not int
                or not 0 <= record['index'] < count or type(record.get('attempt')) is not int
                or not 1 <= record['attempt'] <= 10000):
            raise ValueError('Source shard identity mismatch')
        prior = latest.get(record['index'])
        if prior and prior['attempt'] == record['attempt']:
            raise ValueError('Ambiguous source shard report')
        if prior is None or record['attempt'] > prior['attempt']:
            latest[record['index']] = record
    if set(latest) != set(range(count)):
        raise ValueError('Missing source shard')
    baseline = latest[0]
    all_ids = baseline['collected']
    plan = assignment(all_ids, count)
    seen = []
    for index, record in sorted(latest.items()):
        expected = sorted(node for node in all_ids if plan[node.split('::', 1)[0]] == index)
        if (record.get('exit_code') != 0 or record['collected'] != all_ids
                or record['environment'] != baseline['environment']
                or record['selected'] != expected or record['executed'] != expected):
            raise ValueError('Source shard failed or coverage differs')
        seen.extend(record['executed'])
    if sorted(seen) != all_ids:
        raise ValueError('Incomplete or duplicated full-source coverage')
    return {'commit': commit, 'tree': tree, 'run_id': run_id,
        'shards': count, 'collected': len(all_ids), 'executed': len(seen),
        'environment': baseline['environment']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--count', type=int, required=True)
    args = parser.parse_args()
    git = lambda *options: subprocess.check_output(['git', *options], text=True).strip()
    paths = list(args.reports.rglob('source-shard.json'))
    if not 1 <= len(paths) <= 128 or any(path.stat().st_size > 16 * 1024 * 1024 for path in paths):
        raise ValueError('Invalid source shard report inventory')
    result = verify([json.loads(path.read_bytes()) for path in paths], commit=git('rev-parse', 'HEAD'),
        tree=git('rev-parse', 'HEAD^{tree}'), run_id=os.environ['GITHUB_RUN_ID'], count=args.count)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
