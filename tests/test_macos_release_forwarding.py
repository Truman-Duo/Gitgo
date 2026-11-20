"""Run the real release wrapper in an isolated argument-capture harness.

Only the wrapper is real; Git, test runtimes and the builder are explicit dry
run stubs. This checks forwarding and preserves release preflight behavior. It
does not build, sign, install, change keys or establish publisher trust.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Unix release shell wrapper')


def fixture_release(tmp_path_factory):
    root = tmp_path_factory / 'release tree with spaces'
    (root / 'packaging').mkdir(parents=True)
    (root / 'cli/dashboard').mkdir(parents=True)
    (root / '.venv/bin').mkdir(parents=True)
    commands = root / 'dry run commands'
    commands.mkdir()
    log = root / 'calls.jsonl'
    wrapper = root / 'packaging/release_macos.sh'
    shutil.copyfile(ROOT / 'packaging/release_macos.sh', wrapper)
    stub = '''
import json, os, sys
from pathlib import Path
tool = Path(sys.argv[0]).name
with open(os.environ['RELEASE_DRY_RUN_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps({'tool': tool, 'args': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')
if tool == 'git':
    if sys.argv[1] == 'status':
        if os.environ.get('RELEASE_DRY_RUN_DIRTY') == '1': print(' M fixture.py')
    elif sys.argv[1] == 'log':
        print('test(packaging): dry run fixture')
    elif sys.argv[1] != 'rev-parse':
        raise SystemExit(96)
if tool == 'python' and sys.argv[1:] == ['-B', '-m', 'pytest', 'tests', '-q']:
    if os.environ.get('RELEASE_DRY_RUN_TEST_FAIL') == '1': raise SystemExit(73)
'''
    for path in (commands / 'git', commands / 'bun', root / '.venv/bin/python',
                 root / 'packaging/build_macos.sh'):
        path.write_text('#!' + sys.executable + '\n' + stub, encoding='utf-8')
        path.chmod(0o755)
    env = dict(os.environ)
    env.pop('GITGO_PYTHON', None)
    env['PATH'] = str(commands) + os.pathsep + '/usr/bin:/bin'
    env['RELEASE_DRY_RUN_LOG'] = str(log)
    return root, wrapper, log, env


def run_release(fixture, arguments=(), **extra_env):
    root, wrapper, log, env = fixture
    env.update(extra_env)
    result = subprocess.run(['/bin/bash', str(wrapper), *arguments], cwd=root,
                            env=env, capture_output=True, text=True, timeout=15)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def test_release_forwards_managed_evidence_policy_and_node_as_literal_arguments(tmp_path_factory):
    fixture = fixture_release(tmp_path_factory)
    root = fixture[0]
    # Shell metacharacters are deliberate data; no shell may evaluate them.
    policy = str(root / 'policy $(touch unexpected).json')
    node = str(root / 'node runtime' / 'bin/node')
    options = [
        '--update-evidence', '--update-release-policy', policy, '--node-runtime', node,
        '--output', str(root / 'stage output'), '--archive', str(root / 'release archive.tar.gz'),
        '--codesign-identity', 'fixture identifier with spaces',
        '--ripgrep', str(root / 'rg'), '--ripgrep-notices', str(root / 'rg notices.txt'),
        '--skip-archive',
    ]
    result, calls = run_release(fixture, options)
    assert result.returncode == 0, result.stderr
    assert calls[-1] == {
        'tool': 'build_macos.sh',
        'args': ['--python', str(root / '.venv/bin/python'),
                 '--bun', str(root / 'dry run commands/bun'), *options],
        'cwd': str(root),
    }
    assert [call['tool'] for call in calls] == [
        'git', 'git', 'git', 'python', 'python', 'python', 'bun', 'bun', 'build_macos.sh',
    ]
    assert calls[0]['args'] == ['status', '--porcelain', '--untracked-files=normal']
    assert calls[1]['args'] == ['rev-parse', '--verify', 'upstream/master^{commit}']
    assert calls[2]['args'] == ['log', '--no-merges', '--format=%s', 'upstream/master..HEAD']
    assert calls[3]['args'][0:2] == ['-B', '-c']
    assert calls[4]['args'] == ['-B', 'scripts/verify_release_privacy.py', '--root', str(root)]
    assert calls[5]['args'] == ['-B', '-m', 'pytest', 'tests', '-q']
    assert calls[6]['args'] == ['test']
    assert calls[7]['args'] == ['scripts/build.ts']
    assert calls[6]['cwd'] == calls[7]['cwd'] == str(root / 'cli/dashboard')
    assert not (root / 'unexpected').exists()


def test_release_verify_only_accepts_new_options_but_does_not_build(tmp_path_factory):
    fixture = fixture_release(tmp_path_factory)
    result, calls = run_release(fixture, [
        '--verify-only', '--update-evidence', '--update-release-policy', '/fixture/policy.json',
        '--node-runtime', '/fixture/node',
    ])
    assert result.returncode == 0, result.stderr
    assert all(call['tool'] != 'build_macos.sh' for call in calls)
    assert calls[-1]['tool'] == 'bun' and calls[-1]['args'] == ['scripts/build.ts']


@pytest.mark.parametrize('reason,environment,expected', [
    ('dirty', {'RELEASE_DRY_RUN_DIRTY': '1'}, 1),
    ('test_failure', {'RELEASE_DRY_RUN_TEST_FAIL': '1'}, 73),
])
def test_new_build_options_do_not_skip_clean_tree_or_test_gates(tmp_path_factory, reason, environment, expected):
    fixture = fixture_release(tmp_path_factory)
    result, calls = run_release(fixture, ['--update-evidence', '--node-runtime', '/fixture/node'],
                                **environment)
    assert result.returncode == expected
    assert all(call['tool'] != 'build_macos.sh' for call in calls)
    if reason == 'dirty':
        assert 'worktree is not clean' in result.stderr
        assert [call['tool'] for call in calls] == ['git']
    else:
        assert calls[-1]['args'] == ['-B', '-m', 'pytest', 'tests', '-q']


@pytest.mark.parametrize('arguments', [
    ['--helper-team-identifier', 'TEST123456'],
    ['--production-enabled'],
    ['--update-release-policy'],
    ['--node-runtime'],
])
def test_unrecognized_or_incomplete_options_cannot_start_a_build(tmp_path_factory, arguments):
    result, calls = run_release(fixture_release(tmp_path_factory), arguments)
    assert result.returncode != 0
    assert calls == []
