"""No root operations. Real launchd/UID acceptance is an explicit CLI run."""
import sys
import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX identity and file operations', allow_module_level=True)

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from scripts import probe_macos_uid_recovery as recovery


class EvidenceFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gitgo-recovery-test-')
        self.root = Path(self.temp.name)
        (self.root / 'control').mkdir()
        self.state = {'schema': 2, 'clock': recovery.CLOCK_NAME, 'uid': recovery.base.UID_MIN, 'case': 'cancel',
                      'phase': 'prepared', 'generation': 0, 'launch_count': 0, 'faults': 0,
                      'boot_id': [123, 456]}
        self.path = self.root / 'control/state.json'
        recovery.atomic_json(self.path, self.state)

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_record_private_and_roundtrips(self):
        self.assertEqual(recovery.load_state(self.root, os.getuid()), self.state)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_link_fifo_and_oversize_evidence_refused(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.path)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        big = self.root / 'big'
        big.write_bytes(b'x' * (recovery.MAX_FILE + 1))
        for path in (alias, fifo, big):
            with self.subTest(path=path), self.assertRaises((OSError, RuntimeError)):
                recovery.bounded_read(path)

    def test_corrupt_or_replayable_state_refused(self):
        for change in ({'phase': 'unknown'}, {'uid': 501}, {'uid': True},
                       {'launch_count': 2}, {'launch_count': 1}, {'faults': -1}):
            recovery.atomic_json(self.path, {**self.state, **change})
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                recovery.load_state(self.root, os.getuid())
        self.path.write_text('{')
        with self.assertRaises(ValueError):
            recovery.load_state(self.root, os.getuid())

    def test_group_writable_record_refused(self):
        self.path.chmod(0o660)
        with self.assertRaises(RuntimeError):
            recovery.load_state(self.root, os.getuid())

    def test_recovery_never_replays_any_started_phase(self):
        original_load = recovery.load_state
        for phase in ('starting', 'running', 'stopping', 'cleanup_pending'):
            recovery.atomic_json(self.path, {**self.state, 'phase': phase, 'launch_count': 1, 'generation': 1})
            with self.subTest(phase=phase), \
                    mock.patch.object(recovery, 'verify_root', return_value=self.root), \
                    mock.patch.object(recovery, 'boot_id', return_value=[123, 456]), \
                    mock.patch.object(recovery, 'load_state', side_effect=lambda root: original_load(root, os.getuid())), \
                    mock.patch.object(recovery.base, 'low_spawn') as spawn, \
                    mock.patch.object(recovery, 'birth_summary', return_value={}), \
                    mock.patch.object(recovery, 'drain_uid', return_value={'observed_empty': True}):
                self.assertEqual(recovery.service(self.root), 0)
                spawn.assert_not_called()
            state = original_load(self.root, os.getuid())
            self.assertEqual(state['phase'], 'clean')
            self.assertEqual(state['launch_count'], 1)
            self.assertEqual(state['generation'], 2)

    def test_observation_failure_keeps_recovery_pending(self):
        recovery.atomic_json(self.path, {**self.state, 'phase': 'running', 'launch_count': 1})
        original_load = recovery.load_state
        with mock.patch.object(recovery, 'verify_root', return_value=self.root), \
                mock.patch.object(recovery, 'boot_id', return_value=[123, 456]), \
                mock.patch.object(recovery, 'load_state', side_effect=lambda root: original_load(root, os.getuid())), \
                mock.patch.object(recovery, 'birth_summary', return_value={}), \
                mock.patch.object(recovery, 'drain_uid', side_effect=RuntimeError('census unavailable')):
            self.assertEqual(recovery.service(self.root), 75)
        state = original_load(self.root, os.getuid())
        self.assertEqual(state['phase'], 'cleanup_pending')
        self.assertEqual(state['launch_count'], 1)

    def test_emergency_recovery_does_not_launch_prepared_task(self):
        original_load = recovery.load_state
        with mock.patch.object(recovery, 'verify_root', return_value=self.root), \
                mock.patch.object(recovery, 'boot_id', return_value=[123, 456]), \
                mock.patch.object(recovery, 'load_state', side_effect=lambda root: original_load(root, os.getuid())), \
                mock.patch.object(recovery.base, 'low_spawn') as spawn, \
                mock.patch.object(recovery, 'birth_summary', return_value={}), \
                mock.patch.object(recovery, 'drain_uid', return_value={'observed_empty': True}):
            self.assertEqual(recovery.service(self.root, recover_only=True), 0)
            spawn.assert_not_called()

    def test_fault_budget_is_saved_before_sigkill(self):
        class InjectedCrash(BaseException):
            pass
        with mock.patch.object(recovery.os, 'kill', side_effect=InjectedCrash):
            with self.assertRaises(InjectedCrash):
                recovery.crash_once(self.root, self.state, 1)
        state = recovery.load_state(self.root, os.getuid())
        self.assertEqual(state['faults'], 1)
        with mock.patch.object(recovery.os, 'kill') as kill:
            recovery.crash_once(self.root, state, 1)
            kill.assert_not_called()

    def test_stale_boot_never_signals_or_launches(self):
        original_load = recovery.load_state
        with mock.patch.object(recovery, 'verify_root', return_value=self.root), \
                mock.patch.object(recovery, 'load_state', side_effect=lambda root: original_load(root, os.getuid())), \
                mock.patch.object(recovery, 'boot_id', return_value=[999, 0]), \
                mock.patch.object(recovery, 'drain_uid') as cleanup, \
                mock.patch.object(recovery.base, 'low_spawn') as spawn:
            self.assertEqual(recovery.service(self.root, recover_only=True), 75)
            spawn.assert_not_called()
            cleanup.assert_not_called()
        self.assertEqual(original_load(self.root, os.getuid()), self.state)

    def test_start_is_recorded_before_exec_attempt(self):
        original_load = recovery.load_state
        def failing_exec(*args, **kwargs):
            saved = original_load(self.root, os.getuid())
            self.assertEqual(saved['phase'], 'starting')
            self.assertEqual(saved['launch_count'], 1)
            raise RuntimeError('simulated exec failure')
        with mock.patch.object(recovery, 'verify_root', return_value=self.root), \
                mock.patch.object(recovery, 'load_state', side_effect=lambda root: original_load(root, os.getuid())), \
                mock.patch.object(recovery, 'boot_id', return_value=[123, 456]), \
                mock.patch.object(recovery.socket, 'socket') as listener, \
                mock.patch.object(recovery.base, 'read_event', return_value={'start': True}), \
                mock.patch.object(recovery.base, 'low_spawn', side_effect=failing_exec):
            listener.return_value.accept.return_value = (mock.Mock(), None)
            self.assertEqual(recovery.service(self.root), 75)
        saved = original_load(self.root, os.getuid())
        self.assertEqual(saved['phase'], 'cleanup_pending')
        self.assertEqual(saved['launch_count'], 1)

    def test_legacy_record_can_only_recover_not_launch(self):
        legacy = {**self.state, 'schema': 1}
        legacy.pop('clock')
        recovery.atomic_json(self.path, legacy)
        original_load = recovery.load_state
        with mock.patch.object(recovery, 'verify_root', return_value=self.root), \
                mock.patch.object(recovery, 'load_state', side_effect=lambda root: original_load(root, os.getuid())), \
                mock.patch.object(recovery, 'boot_id', return_value=[123, 456]), \
                mock.patch.object(recovery.base, 'low_spawn') as spawn, \
                mock.patch.object(recovery, 'birth_summary', return_value={}), \
                mock.patch.object(recovery, 'drain_uid', return_value={'observed_empty': True}):
            self.assertEqual(recovery.service(self.root), 0)
            spawn.assert_not_called()


class Cleanup(unittest.TestCase):
    def test_elapsed_gate_rejects_legacy_reset_expired_and_invalid_clocks(self):
        valid = {'clock': recovery.CLOCK_NAME, 'started': 100, 'finished': 103}
        self.assertTrue(recovery.within_task_deadline(valid))
        for change in ({'clock': None}, {'finished': 0.7}, {'finished': 120},
                       {'finished': float('nan')}, {'started': True}):
            with self.subTest(change=change):
                self.assertFalse(recovery.within_task_deadline({**valid, **change}))

    def test_new_birth_during_freeze_is_included_in_next_sweep(self):
        one = [{'pid': 41, 'state': 'S'}]
        two = one + [{'pid': 42, 'state': 'S'}]
        stopped = [{'pid': 41, 'state': 'T'}, {'pid': 42, 'state': 'T'}]
        with mock.patch.object(recovery.base, 'owned_rows', side_effect=[one, two, two, stopped, stopped, [], [], []]), \
                mock.patch.object(recovery, 'send_uid_signal') as send, \
                mock.patch.object(recovery.time, 'sleep'):
            result = recovery.drain_uid(recovery.base.UID_MIN)
        self.assertTrue(result['observed_empty'])
        self.assertTrue(result['quiescence_observed'])
        self.assertEqual(result['stop_rounds'], 2)
        self.assertEqual([c.args[2] for c in send.call_args_list],
                         [signal.SIGSTOP, signal.SIGSTOP, signal.SIGKILL])
        self.assertEqual(send.call_args_list[-1].args[1], stopped)

    def test_freeze_error_still_attempts_kill(self):
        rows = [{'pid': 41, 'state': 'S'}]
        with mock.patch.object(recovery.base, 'owned_rows', side_effect=[rows, rows, [], [], []]), \
                mock.patch.object(recovery, 'send_uid_signal', side_effect=[RuntimeError('freeze failed'), None]) as send, \
                mock.patch.object(recovery.time, 'sleep'):
            result = recovery.drain_uid(recovery.base.UID_MIN)
        self.assertTrue(result['observed_empty'])
        self.assertEqual(result['issues'], ['freeze failed'])
        self.assertEqual(send.call_args_list[-1].args[2], signal.SIGKILL)

    def test_census_failure_never_reports_clean(self):
        with mock.patch.object(recovery.base, 'owned_rows', side_effect=RuntimeError('denied')):
            with self.assertRaisesRegex(RuntimeError, 'denied'):
                recovery.drain_uid(recovery.base.UID_MIN)

    def test_signal_helper_refuses_login_identity(self):
        result = subprocess.run([recovery.base.PYTHON, '-I', '-c', recovery.SIGNALS,
                                 json.dumps([os.getuid(), int(signal.SIGSTOP), [os.getpid()]])],
                                capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'refusing unsafe', result.stderr)

    def test_launch_policy_restarts_failures_and_stops_on_success(self):
        value = recovery.plist('test', '/script', [], Path('/logs'))
        self.assertEqual(value['KeepAlive'], {'SuccessfulExit': False})
        self.assertEqual(value['ThrottleInterval'], 1)
        self.assertEqual(value['ProgramArguments'][:2], [recovery.base.PYTHON, '-I'])


@unittest.skipUnless(os.uname().sysname == 'Darwin', 'Darwin fixture')
class ChurnFixture(unittest.TestCase):
    def test_system_python_clock_is_shared_across_exec(self):
        before = recovery.system_time()
        result = subprocess.run([recovery.base.PYTHON, '-I', '-c',
                                'import time; print(time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)/1000000000)'],
                               capture_output=True, text=True, timeout=3, check=True)
        after = recovery.system_time()
        self.assertLessEqual(before, float(result.stdout))
        self.assertLessEqual(float(result.stdout), after)

    def test_bounded_detached_producers_keep_spawning(self):
        with tempfile.TemporaryDirectory(prefix='gitgo-churn-fixture-') as raw:
            root = Path(raw)
            (root / 'work').mkdir()
            child = subprocess.Popen([recovery.base.PYTHON, '-I', '-c', recovery.CHURN, '1.5'],
                                     cwd=root / 'work', start_new_session=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready = recovery.base.read_event(child.stdout.fileno(), 2)
                self.assertEqual(ready['uid'], os.getuid())
                first = recovery.birth_summary(root, os.getuid())
                time.sleep(.25)
                second = recovery.birth_summary(root, os.getuid())
                self.assertGreater(second['births'], first['births'])
                self.assertEqual(second['producers'], 2)
                child.communicate(timeout=3)
                self.assertEqual(child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=3)
                time.sleep(1.6)  # fixed lifetime only; never signal the login UID


if __name__ == '__main__':
    unittest.main()
