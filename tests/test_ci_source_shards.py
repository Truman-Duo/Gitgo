"""Full coverage, failed attempt and real pytest shard execution regressions."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_shards', ROOT / 'scripts/ci_source_shards.py')
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)


def examples():
    nodes = ['tests/test_a.py::test_first', 'tests/test_a.py::test_second',
             'tests/test_b.py::test_other', 'tests/test_c.py::test_last']
    plan = shards.assignment(nodes, 3)
    records = []
    for index in range(3):
        selected = [node for node in nodes if plan[node.split('::', 1)[0]] == index]
        records.append({'schema_version': 1, 'commit': 'a' * 40, 'tree': 'b' * 40,
            'run_id': '42', 'attempt': 1, 'count': 3, 'index': index,
            'environment': {'python': [3, 14, 7]}, 'collected': nodes,
            'selected': selected, 'executed': list(selected), 'exit_code': 0})
    return records


def verify(records):
    return shards.verify(records, commit='a' * 40, tree='b' * 40, run_id='42', count=3)


class SourceShardTests(unittest.TestCase):
    def test_full_partition_preserves_files_and_accounts_for_every_test_once(self):
        records = examples()
        self.assertEqual(verify(records)['executed'], 4)
        self.assertEqual(records[0]['selected'], records[0]['collected'][:2])
        self.assertEqual(shards.assignment(list(reversed(records[0]['collected'])), 3),
                         shards.assignment(records[0]['collected'], 3))

    def test_missing_wrong_failed_or_filtered_coverage_is_rejected(self):
        for change in ('missing', 'commit', 'tree', 'run', 'failed', 'collection',
                       'environment', 'selected', 'executed', 'duplicate'):
            records = copy.deepcopy(examples())
            if change == 'missing': records.pop()
            elif change == 'commit': records[0]['commit'] = 'c' * 40
            elif change == 'tree': records[0]['tree'] = 'c' * 40
            elif change == 'run': records[0]['run_id'] = 'old'
            elif change == 'failed': records[0]['exit_code'] = 1
            elif change == 'collection': records[0]['collected'] = records[0]['collected'][:-1]
            elif change == 'environment': records[0]['environment'] = {'python': [3, 12, 10]}
            elif change == 'selected': records[0]['selected'] = []
            elif change == 'executed': records[0]['executed'] = []
            else: records.append(records[0])
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify(records)

    def test_failed_latest_attempt_cannot_hide_behind_an_old_success(self):
        records = examples()
        failed = copy.deepcopy(records[0])
        failed.update(attempt=2, exit_code=1)
        with self.assertRaises(ValueError): verify([*records, failed])
        retry = copy.deepcopy(failed)
        retry.update(attempt=3, exit_code=0)
        self.assertEqual(verify([*records, failed, retry])['executed'], 4)

    @unittest.skipUnless(importlib.util.find_spec('pytest'), 'Requires the actual pytest runtime')
    def test_actual_pytest_runs_whole_ordered_files_and_the_join_admits_complete_reports(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'tests').mkdir()
            (root / 'packaging').mkdir()
            (root / 'packaging/requirements-build.lock').write_text('synthetic fixture\n')
            (root / 'tests/test_a.py').write_text('state=[]\ndef test_first(): state.append(1)\n'
                                                 'def test_second(): assert state == [1]\n')
            (root / 'tests/test_b.py').write_text('def test_other(): assert 2+2 == 4\n')
            (root / 'tests/test_c.py').write_text('import pytest\ndef test_last(): pytest.skip("fixture")\n')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=CI fixture', '-c', 'user.email=ci@local.invalid',
                            'commit', '-q', '-m', 'fixture'], cwd=root, check=True)
            git = lambda *args: subprocess.check_output(['git', *args], cwd=root, text=True).strip()
            env = dict(os.environ, GITHUB_RUN_ID='fixture', GITHUB_RUN_ATTEMPT='1',
                       PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get('PYTHONPATH', ''))
            records = []
            for index in range(3):
                report = root / f'reports/{index}/source-shard.json'
                result = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-p',
                    'scripts.ci_source_shards', 'tests', '-q', '--source-shard-count', '3',
                    '--source-shard-index', str(index), '--source-shard-report', str(report)],
                    cwd=root, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                records.append(json.loads(report.read_bytes()))
            self.assertEqual(shards.verify(records, commit=git('rev-parse', 'HEAD'),
                tree=git('rev-parse', 'HEAD^{tree}'), run_id='fixture', count=3)['executed'], 4)


if __name__ == '__main__':
    unittest.main()
