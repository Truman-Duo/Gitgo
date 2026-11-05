"""Scheduling and identity regressions; runnable before installing the product."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'))


class PipelineTests(unittest.TestCase):
    def test_scheduling_regressions_run_before_product_dependencies_are_installed(self):
        steps = workflow('pr-contract.yml')['jobs']['contract']['steps']
        install = next(step for step in steps if 'requirements-ci.lock' in step.get('run', ''))
        self.assertIn('--require-hashes --no-deps', install['run'])
        check = next(step for step in steps if 'tests/test_ci_pipeline.py' in step.get('run', ''))
        self.assertIn('tests/test_ci_pr_contract.py', check['run'])
        self.assertTrue(any('scripts/check_workflow_syntax.py' in step.get('run', '') for step in steps))
        lock = (ROOT / 'packaging/requirements-ci.lock').read_text(encoding='utf-8')
        entry = lock[lock.index('PyYAML=='):].strip()
        self.assertIn(entry, (ROOT / 'packaging/requirements-build.lock').read_text(encoding='utf-8'))

    def test_wrong_or_missing_upstream_identity_never_becomes_a_reused_success(self):
        prerequisite = module('ci_prerequisites')
        self.assertTrue(prerequisite.required('', 'a' * 40))
        self.assertFalse(prerequisite.required('a' * 40, 'a' * 40))
        for value in ('b' * 40, 'master', 'true', 'a' * 39, 'a' * 40 + '\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prerequisite.required(value, 'a' * 40)

    def test_prerequisite_command_records_no_partial_success_after_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / 'checkout'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            subprocess.run(['git', '-c', 'user.name=CI fixture', '-c', 'user.email=ci@local.invalid',
                            'commit', '-q', '--allow-empty', '-m', 'fixture'], cwd=repo, check=True)
            output = root / 'outputs'
            env = dict(os.environ, SOURCE_VERIFIED_COMMIT='', DASHBOARD_VERIFIED_COMMIT='b' * 40,
                       GITHUB_OUTPUT=str(output))
            result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'scripts/ci_prerequisites.py')],
                cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
            env['DASHBOARD_VERIFIED_COMMIT'] = head
            result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'scripts/ci_prerequisites.py')],
                cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), 'source=true\ndashboard=false\n')

    def test_heavy_jobs_cannot_start_when_contract_or_privacy_fails(self):
        jobs = workflow('ci.yml')['jobs']
        for name, job in jobs.items():
            if name in {'contract', 'privacy', 'gate'}:
                continue
            with self.subTest(job=name):
                self.assertTrue({'contract', 'privacy'} <= set(job['needs']))
                self.assertNotIn('always()', job.get('if', ''))

    def test_reuse_is_bound_to_successful_dependencies_in_the_same_run(self):
        jobs = workflow('ci.yml')['jobs']
        update = jobs['managed-update-windows']
        self.assertTrue({'python-windows', 'dashboard'} <= set(update['needs']))
        for kind, name in [('source', 'python-windows'), ('dashboard', 'dashboard')]:
            self.assertEqual(update['with'][kind + '_verified_commit'], '${{ needs.' + name + '.outputs.commit }}')
            verified = next(step for step in jobs[name]['steps'] if step.get('id') == 'verified')
            self.assertNotIn('if', verified)
            self.assertIn('git rev-parse HEAD', verified['run'])
            self.assertEqual(jobs[name]['outputs']['commit'], '${{ steps.verified.outputs.commit }}')
        update_steps = workflow('windows-update.yml')['jobs']['frozen']['steps']
        prerequisite = next(step for step in update_steps if step.get('id') == 'prerequisites')
        self.assertIn('ci_prerequisites.py', prerequisite['run'])
        on = workflow('windows-update.yml').get('on', workflow('windows-update.yml').get(True))
        for declaration in on['workflow_call']['inputs'].values():
            self.assertEqual(declaration['default'], '')
        for name, key in [('Full Windows source regression', 'source'),
                          ('Dashboard Windows tests and production build', 'dashboard')]:
            step = next(step for step in update_steps if step.get('name') == name)
            self.assertEqual(step['if'], "steps.prerequisites.outputs." + key + " == 'true'")

    def test_reused_suites_have_the_same_baseline_and_pinned_runtimes(self):
        jobs = workflow('ci.yml')['jobs']
        frozen = workflow('windows-update.yml')['jobs']['frozen']
        def runtime(job, name, key):
            return next(step['with'][key] for step in job['steps']
                        if step.get('uses', '').startswith(name + '@'))
        for name in ('python-windows-shards', 'dashboard'):
            job = jobs[name]
            self.assertEqual(job['runs-on'], frozen['runs-on'])
            for action, key in [('actions/setup-python', 'python-version'),
                                ('actions/setup-node', 'node-version'),
                                ('oven-sh/setup-bun', 'bun-version')]:
                self.assertEqual(runtime(job, action, key), runtime(frozen, action, key))
        for job in (jobs['python-windows-shards'], frozen):
            self.assertTrue(any('--require-hashes -r packaging/requirements-build.lock' in step.get('run', '')
                                for step in job['steps']))

    def test_independent_build_does_not_wait_for_source_but_final_gate_requires_both(self):
        jobs = workflow('ci.yml')['jobs']
        self.assertEqual(set(jobs['windows-package']['needs']), {'contract', 'privacy', 'dashboard'})
        self.assertTrue({'python-windows', 'windows-package'} <= set(jobs['gate']['needs']))
        self.assertIn('python-windows', jobs['managed-update-windows']['needs'])

    def test_full_source_output_requires_every_shard_and_a_complete_coverage_join(self):
        jobs = workflow('ci.yml')['jobs']
        pieces = jobs['python-windows-shards']
        self.assertEqual(pieces['strategy']['matrix']['shard'], [0, 1, 2, 3])
        self.assertFalse(pieces['strategy']['fail-fast'])
        run = next(step['run'] for step in pieces['steps'] if step.get('name') == 'Full Python suite')
        self.assertIn('-p scripts.ci_source_shards tests -q', run)
        self.assertIn('--source-shard-count 4', run)
        self.assertIn('--source-collection-expected dev-artifacts/ci-source/collection-probe.json', run)
        self.assertIn('python-windows-shards', jobs['python-windows']['needs'])
        verified = next(step for step in jobs['python-windows']['steps'] if step.get('id') == 'verified')
        self.assertIn('scripts/ci_source_shards.py --reports incoming --count 4', verified['run'])

    def test_metadata_edits_cannot_restart_packaging_or_replace_the_product_gate(self):
        ci = workflow('ci.yml')
        self.assertNotIn('edited', ci.get('on', ci.get(True))['pull_request']['types'])
        metadata = workflow('pr-contract.yml')
        self.assertEqual(metadata.get('on', metadata.get(True))['pull_request']['types'], ['edited'])
        self.assertEqual(set(metadata['jobs']), {'contract'})
        native = workflow('native-sandbox.yml')
        self.assertNotIn('pull_request', native.get('on', native.get(True)))
        jobs = ci['jobs']
        self.assertIn('macos-audit', jobs['gate']['needs'])
        self.assertTrue(jobs['macos-audit']['with']['audit_only'])

    def test_unix_acceptance_waits_only_for_its_own_native_build(self):
        caller = workflow('linux-package.yml')['jobs']['target']
        self.assertFalse(caller['strategy']['fail-fast'])
        self.assertEqual({item['target'] for item in caller['strategy']['matrix']['include']},
                         {'linux-x86_64', 'linux-aarch64', 'macos-arm64'})
        target = workflow('unix-target.yml')['jobs']
        self.assertEqual(target['accept']['needs'], 'build')
        self.assertNotIn('strategy', target['accept'])
        self.assertEqual(target['build']['runs-on'], target['accept']['runs-on'])
        for name in ('build', 'accept', 'published'):
            checkout = target[name]['steps'][0]
            self.assertEqual(checkout['with']['ref'], '${{ inputs.commit }}')

    def test_native_context_keeps_source_boundaries_and_new_boundary_tests_are_discovered(self):
        runner = module('run_sandbox_acceptance')
        self.assertEqual(runner.source_test_paths(ROOT, 'full'), ['tests'])
        selected = runner.source_test_paths(ROOT, 'boundaries')
        self.assertIn('tests/test_native_sandbox.py', selected)
        self.assertIn('tests/test_alpha3_sandbox_integration.py', selected)
        self.assertIn('tests/test_read_execution_plan.py', selected)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'tests').mkdir()
            with self.assertRaises(ValueError): runner.source_test_paths(root, 'boundaries')
            (root / 'tests/test_sandbox_new_boundary.py').touch()
            self.assertEqual(runner.source_test_paths(root, 'boundaries'), ['tests/test_sandbox_new_boundary.py'])
        for job in workflow('native-acceptance.yml')['jobs'].values():
            for step in job['steps']:
                if 'scripts/run_sandbox_acceptance.py' in step.get('run', ''):
                    self.assertIn('--source-suite boundaries', step['run'])


if __name__ == '__main__':
    unittest.main()
