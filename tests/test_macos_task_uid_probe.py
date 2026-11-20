"""Unprivileged checks for the opt-in UID probe. Not low-identity acceptance."""
import sys
import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX identity and file operations', allow_module_level=True)

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from scripts import probe_macos_task_uid as probe


class ProbeGuards(unittest.TestCase):
    def test_tool_evidence_survives_cleanup_failure(self):
        results = {}
        child = mock.Mock(returncode=0)
        child.communicate.return_value = (b'git init completed', b'')
        tools = {name: '/bin/false' for name in ('node', 'bun', 'npm', 'git')}
        with mock.patch.object(probe, 'low_spawn', return_value=child), \
                mock.patch.object(probe, 'cleanup_uid', side_effect=OSError(22, 'group query failed')):
            with self.assertRaises(OSError):
                probe.tool_smoke(probe.UID_MIN, Path('/unused'), {}, tools, results=results)
        self.assertTrue(results['git_init']['passed'])
        self.assertEqual(results['git_init']['stdout'], 'git init completed')

    def test_kernel_group_query_errors_are_not_treated_as_empty(self):
        namespace = {}
        exec(probe.KERNEL_GROUPS, namespace)
        for returns in ([-1], [1, -1], [1, 2]):
            with self.subTest(returns=returns), \
                    mock.patch('ctypes.CDLL') as library:
                library.return_value.getgroups.side_effect = returns
                with self.assertRaises(OSError):
                    namespace['kernel_groups']()

    def test_nonroot_run_and_ordinary_identity_launch_are_refused(self):
        with mock.patch.object(probe.os, 'getuid', return_value=501), \
                mock.patch.object(probe.os, 'geteuid', return_value=501), \
                mock.patch.object(probe.subprocess, 'Popen') as spawn:
            with self.assertRaises(RuntimeError):
                probe.run_suite({})
            with self.assertRaises(RuntimeError):
                probe.low_spawn(['/bin/true'], 501, '/', {})
            spawn.assert_not_called()

    def test_signal_helper_refuses_ordinary_identity_without_signalling(self):
        result = subprocess.run([probe.PYTHON, '-I', '-c', probe.SIGNAL_HELPER,
                                 json.dumps([os.getuid(), 9, [os.getpid()]])],
                                capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'refusing unsafe', result.stderr)

    def test_census_fails_closed_and_includes_saved_uid(self):
        for text in ('', '0 0 0 1 1 S', 'truncated', f'0 0 0 {os.getpid()} 1 S\nbad row'):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                probe.parse_census(text)
        rows = probe.parse_census(f'0 0 0 {os.getpid()} 1 S\n501 501 200000001 44 44 Z')
        with mock.patch.object(probe, 'census', return_value=rows):
            self.assertEqual(probe.owned_rows(200000001)[0]['pid'], 44)

    def test_unknown_census_cannot_report_cleanup_success(self):
        with mock.patch.object(probe, 'census', side_effect=RuntimeError('denied')):
            with self.assertRaisesRegex(RuntimeError, 'denied'):
                probe.cleanup_uid(probe.UID_MIN, '/', {})

    def test_cleanup_requires_repeated_empty_observations(self):
        with mock.patch.object(probe, 'owned_rows', side_effect=[[], [], RuntimeError('lost')]):
            with self.assertRaisesRegex(RuntimeError, 'lost'):
                probe.cleanup_uid(probe.UID_MIN, '/', {})

    def test_cleanup_deadline_reports_remaining_processes(self):
        remaining = [{'pid': 123, 'state': 'Z'}]
        with mock.patch.object(probe.time, 'monotonic', side_effect=[0, 6, 6]), \
                mock.patch.object(probe, 'owned_rows', return_value=remaining):
            result = probe.cleanup_uid(probe.UID_MIN, '/', {})
            self.assertFalse(result['observed_empty'])
            self.assertEqual(result['remaining'], remaining)

    def test_no_identity_reuse_or_existing_account_selection(self):
        used = {probe.UID_MIN}
        with mock.patch.object(probe, 'census', return_value=[
                {'uid': probe.UID_MIN + 1, 'ruid': 0, 'svuid': 0}]), \
                mock.patch.object(probe.secrets, 'randbelow', side_effect=[0, 1, 2, 3, 4]), \
                mock.patch.object(probe.pwd, 'getpwuid', side_effect=[object(), KeyError(), KeyError()]), \
                mock.patch.object(probe.grp, 'getgrgid', side_effect=[object(), KeyError()]):
            self.assertEqual(probe.choose_uid(used), probe.UID_MIN + 4)
            self.assertIn(probe.UID_MIN + 4, used)

    def test_control_eof_does_not_count_as_ready(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                probe.read_event(read_fd, .1)
        finally:
            os.close(read_fd)


@unittest.skipUnless(os.uname().sysname == 'Darwin' and os.getuid() != 0,
                     'local unprivileged macOS fixture checks')
class LocalFixtures(unittest.TestCase):
    def test_kernel_groups_does_not_depend_on_account_database(self):
        namespace = {}
        exec(probe.KERNEL_GROUPS, namespace)
        with mock.patch.object(os, 'getgroups', side_effect=OSError(22, 'unregistered UID')) as extended:
            groups = namespace['kernel_groups']()
        self.assertIn(os.getegid(), groups)
        extended.assert_not_called()

    def test_all_tool_fixtures_run_without_network(self):
        tools = probe.resolve_tools()
        with tempfile.TemporaryDirectory(prefix='gitgo-uid-fixture-') as raw:
            workspace = Path(raw)
            env = probe.environment(workspace, tools)
            (workspace / 'package.json').write_text(json.dumps({
                'scripts': {'probe': 'node -e "process.stdout.write(\'tool-child-ok\')"'}}))
            subprocess.run([tools['git'], 'init', '-q'], cwd=raw, env=env, check=True, timeout=3)
            hook = workspace / '.git/hooks/pre-commit'
            hook.write_text('#!/bin/sh\nprintf tool-child-ok\n')
            hook.chmod(0o700)
            for name, command in probe.tool_commands(tools).items():
                with self.subTest(tool=name):
                    result = subprocess.run(command, cwd=raw, env=env, capture_output=True, timeout=8)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(b'tool-child-ok', result.stdout + result.stderr)

    def test_fixed_descendants_start_and_leave_supervisor_group(self):
        tools = probe.resolve_tools()
        with tempfile.TemporaryDirectory(prefix='gitgo-uid-children-') as raw:
            forbidden = Path(raw) / 'private'
            forbidden.write_text('fixture')
            forbidden.chmod(0)
            child = subprocess.Popen([probe.PYTHON, '-I', '-c', probe.SUPERVISOR,
                                      json.dumps([tools['node'], probe.PYTHON, probe.NODE_CHILD,
                                                  probe.DOUBLE_FORK, str(forbidden),
                                                  os.getuid(), os.getgid(), 2])],
                                     cwd=raw, env=probe.environment(Path(raw), tools),
                                     start_new_session=True, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready = probe.read_event(child.stdout.fileno(), 5)
                self.assertEqual({c['name'] for c in ready['children']},
                                 {'ordinary', 'setsid', 'double_fork'})
                for entry in ready['children']:
                    self.assertEqual(os.getpgid(entry['pid']) == ready['pgid'],
                                     entry['name'] == 'ordinary')
                child.communicate(input=b'cancel\n', timeout=2)
                self.assertEqual(child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=3)
                # Only fixed two-second children run in this test. No UID
                # census or UID-wide cleanup is allowed for the login user.
                time.sleep(2.2)
                forbidden.chmod(0o600)


if __name__ == '__main__':
    unittest.main()
