"""The helper-entry harness must not accept a restart as pre-stop refusal."""
from copy import deepcopy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location('helper_entry_acceptance',
    Path(__file__).parents[1] / 'scripts/check_macos_helper_entry.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def observation():
    return dict(pid=4101, service={'phase': 'active', 'helper': 'old'},
                service_sha256='service', plist_sha256='plist', credential_sha256='credential',
                grant={'grant_id': 'grant'}, seen_sha256='seen', seen_count=1,
                invocations=[{'invocation_id': 'original', 'phase': 'clean', 'launch_count': 1}],
                lifecycle_records={'lifecycle-original.json': 'original'}, all_clean=True)


@pytest.mark.parametrize('field,value', [
    ('pid', 4102),
    ('pid', None),
    ('service', {'phase': 'active', 'helper': 'new'}),
    ('service_sha256', 'rewritten'),
    ('plist_sha256', 'rewritten'),
    ('credential_sha256', 'renewed'),
    ('grant', {'grant_id': 'renewed'}),
    ('seen_sha256', 'new-request'),
    ('seen_count', 2),
    ('invocations', [{'invocation_id': 'original', 'phase': 'clean', 'launch_count': 2}]),
    ('lifecycle_records', {'lifecycle-original.json': 'original', 'lifecycle-new.json': 'new'}),
    ('all_clean', False),
])
def test_preflight_refusal_requires_same_running_service_and_unchanged_evidence(field, value):
    before = observation()
    probe.unchanged_service(before, deepcopy(before))
    after = deepcopy(before)
    after[field] = value
    with pytest.raises(RuntimeError):
        probe.unchanged_service(before, after)


def test_recovery_fault_preserves_actual_active_and_previous_identities():
    before = dict(schema=1, name='fixture', phase='active', helper='new', previous='old', candidate=None)
    target = probe.recovery_fixture(before, 'old')
    assert target == {**before, 'phase': 'switching', 'candidate': 'new'}
    assert before['phase'] == 'active'
    for changed in ({**before, 'previous': 'unrelated'}, {**before, 'phase': 'switching'},
                    {**before, 'candidate': 'pending'}, {**before, 'helper': 'old'}):
        with pytest.raises(RuntimeError, match='immediately preceding'):
            probe.recovery_fixture(changed, 'old')


def test_unique_fault_marker_cannot_select_an_existing_named_service():
    service = 'entry-' + 'a' * 20
    marker = json.loads(probe.marker_bytes(service))
    assert marker['service'] == service and marker['local_test_only'] is True
    with pytest.raises(RuntimeError, match='fixture service'):
        probe.marker_bytes('existing-user-service')


def test_acl_restoration_refuses_extra_or_unrelated_aces(monkeypatch, tmp_path_factory):
    marker = tmp_path_factory / 'marker'
    marker.write_bytes(b'fixture')
    calls = []
    monkeypatch.setattr(probe.subprocess, 'run', lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(probe, 'acl_spellings', lambda _account: {'user:fixture allow read,write'})
    for entries in (['user:another allow read,write'],
                    ['user:fixture allow read,write', 'group:everyone allow read']):
        monkeypatch.setattr(probe, 'acl_entries', lambda _path: entries)
        with pytest.raises(RuntimeError, match='pre-existing or unexpected'):
            probe.change_marker_acl(marker, 'fixture', add=False)
    monkeypatch.setattr(probe, 'acl_entries', lambda _path: ['user:fixture allow read,write'])
    with pytest.raises(RuntimeError, match='pre-existing or unexpected'):
        probe.change_marker_acl(marker, 'fixture', add=True)
    assert calls == []


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real macOS ACL fixture')
def test_real_marker_acl_is_added_and_only_its_exact_entry_removed(tmp_path_factory):
    import pwd
    marker = tmp_path_factory / 'real-acl-marker'
    marker.write_bytes(b'local acceptance marker\n')
    original = marker.read_bytes()
    account = pwd.getpwuid(os.getuid()).pw_name
    assert probe.acl_entries(marker) == []
    entry = 'user:' + account + ' allow read,write'
    spellings = probe.acl_spellings(account)
    try:
        added = probe.change_marker_acl(marker, account, add=True)
        assert added['before'] == [] and len(added['after']) == 1 and added['after'][0] in spellings
        removed = probe.change_marker_acl(marker, account, add=False)
        assert removed['before'] == added['after'] and removed['after'] == []
        assert probe.change_marker_acl(marker, account, add=False)['changed'] is False
        assert marker.read_bytes() == original
    finally:
        # Do not erase anything except the ACE that this test introduced.
        remaining = probe.acl_entries(marker)
        if len(remaining) == 1 and remaining[0] in spellings:
            subprocess.run(['/bin/chmod', '-a', entry, str(marker)], check=True, capture_output=True)


def test_initial_install_cleanup_does_not_require_a_fabricated_seen_ledger():
    state = dict(phase='blocked', helper=None, previous=None, candidate=None)
    facts = dict(ledger_present=False, invocations=[], active_present=False, launch_code=113)
    probe.initial_failure_without_execution(state, **facts)
    for key, value in [('ledger_present', True), ('invocations', ['invocation-something.json']),
                       ('active_present', True), ('launch_code', 0), ('launch_code', 1)]:
        with pytest.raises(RuntimeError, match='absence of execution evidence'):
            probe.initial_failure_without_execution(state, **{**facts, key: value})
    for key, value in [('phase', 'active'), ('helper', 'old'), ('previous', 'old'), ('candidate', 'new')]:
        with pytest.raises(RuntimeError, match='initial blocked installation'):
            probe.initial_failure_without_execution({**state, key: value}, **facts)


def test_boot_recovery_preflight_retries_only_allocation_contention_with_a_bound():
    now = [0.0]
    calls = []
    def wait(seconds):
        now[0] += seconds
    def recovery(**budgets):
        calls.append(True)
        if len(calls) < 3:
            raise BlockingIOError(35, 'busy')
        return ['a' * 32]
    result = probe.wait_for_boot_recovery(recovery, clock=lambda: now[0], sleep=wait)
    assert result['passed'] and result['attempts'] == 3 and len(result['waits']) == 2
    assert result['elapsed_seconds'] == 2 and result['recovered_count'] == 1
    def busy(**budgets):
        raise BlockingIOError(35, 'still busy')
    result = probe.wait_for_boot_recovery(busy, clock=lambda: now[0], sleep=wait, budget=3)
    assert result['passed'] is False and result['timed_out'] is True
    assert result['elapsed_seconds'] == 3 and result['attempts'] == 3


@pytest.mark.parametrize('error', [BlockingIOError(11, 'other platform/code'), PermissionError('denied'), RuntimeError('busy text is not authority'),
                                  ValueError('invalid evidence')])
def test_boot_recovery_preflight_does_not_retry_other_failures(error):
    calls = []
    def recovery(**budgets):
        calls.append(True)
        raise error
    result = probe.wait_for_boot_recovery(recovery, sleep=lambda _: pytest.fail('Unexpected retry'))
    assert result['passed'] is False and result['retryable'] is False and result['attempts'] == 1
    assert calls == [True]


def test_typed_recovery_busy_consumes_one_total_budget_and_does_not_reset_it():
    from backend.core.macos_boot_recovery import RecoveryBusy
    now = [0.0]
    calls = []
    def recover(**budgets):
        calls.append(budgets)
        if len(calls) < 3:
            now[0] += 30
            raise RecoveryBusy('actual allocation contention')
        return ['b' * 32]
    result = probe.wait_for_boot_recovery(recover, recovery_busy=RecoveryBusy,
        clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert result['passed'] and result['attempts'] == 3
    assert calls == [dict(seconds=120, lock_seconds=30), dict(seconds=89, lock_seconds=30),
                     dict(seconds=58, lock_seconds=30)]
    assert result['recovered_count_scope'] == 'successful_final_attempt_only'
    assert [row['error_type'] for row in result['waits']] == ['RecoveryBusy', 'RecoveryBusy']


def test_short_remaining_budget_limits_both_product_deadlines():
    from backend.core.macos_boot_recovery import RecoveryBusy, RecoveryTimeout
    calls = []
    def recover(**budgets):
        calls.append(budgets)
        raise RecoveryTimeout('scan did not finish')
    result = probe.wait_for_boot_recovery(recover, recovery_busy=RecoveryBusy, budget=7,
        clock=lambda: 0, sleep=lambda _: pytest.fail('Do not retry scan failure'))
    assert not result['passed'] and result['retryable'] is False
    assert calls == [dict(seconds=7, lock_seconds=7)]


def startup_fixture():
    busy = dict(schema=1, nonce='b' * 32, boot='boot-identity', helper='new', pid=4900,
                starts=1, phase='recover_busy', lock_waits=1, error='RECOVERY_LOCK_BUSY')
    state = dict(phase='switching', helper='old', candidate='new')
    return busy, state, dict(previous_nonce='a' * 32, old_helper='old', boot='boot-identity')


@pytest.mark.parametrize('field,value', [
    ('nonce', 'a' * 32), ('nonce', 'malformed'), ('boot', 'other-boot'), ('helper', 'old'),
    ('pid', 0), ('pid', True), ('starts', 2), ('starts', True), ('phase', 'ready'),
    ('error', None), ('lock_waits', 0),
])
def test_busy_observation_cannot_be_stale_or_another_attempt(field, value):
    busy, state, expected = startup_fixture()
    assert probe.matching_busy_startup(busy, state, **expected)
    assert not probe.matching_busy_startup({**busy, field: value}, state, **expected)


def test_busy_observation_must_match_current_switch_target():
    busy, state, expected = startup_fixture()
    for changed in ({**state, 'phase': 'active'}, {**state, 'helper': 'unrelated'},
                    {**state, 'candidate': 'unrelated'}, {**state, 'candidate': 'old'}):
        assert not probe.matching_busy_startup(busy, changed, **expected)


@pytest.mark.parametrize('field,value', [
    ('nonce', 'c' * 32), ('boot', 'different-boot'), ('helper', 'other'), ('pid', 4901),
    ('starts', 2), ('starts', True), ('phase', 'recovering'), ('error', 'RECOVERY_TIMEOUT'),
    ('lock_waits', 0),
])
def test_ready_cannot_hide_a_restarted_or_unrelated_helper(field, value):
    busy, _, _ = startup_fixture()
    ready = {**busy, 'phase': 'ready', 'error': None}
    probe.same_startup_became_ready(busy, ready, 4900, 'new')
    with pytest.raises(RuntimeError, match='same single service start'):
        probe.same_startup_became_ready(busy, {**ready, field: value}, 4900, 'new')


def lock_holder_print_output():
    common = dict(service='entry-' + 'a' * 20, lock_path='/private/tmp/gitgo-task-uid-probe-v1.lock', boot='fixture-boot')
    ready = dict(event='allocation_lock_held', holder_pid=6796, **common)
    result = dict(event='allocation_lock_released', passed=True, released=True,
                  observed_busy=dict(phase='recover_busy', pid=6821, lock_waits=1, starts=1), **common)
    output = io.StringIO()
    with redirect_stdout(output):
        # The root probe flushes the handshake, and main pretty-prints its
        # returned result. This is the exact two-print shape used in the VM.
        print(json.dumps(ready), flush=True)
        print(json.dumps(result, indent=2), flush=True)
    return output.getvalue(), ready, result


def test_lock_holder_accepts_actual_flushed_line_and_pretty_result_format():
    raw, ready, result = lock_holder_print_output()
    assert probe.parse_lock_holder_output(raw) == (ready, result)


@pytest.mark.parametrize('suffix', ['trailing log noise', '{}\n', '{"event":"allocation_lock_released"}\n'])
def test_lock_holder_rejects_extra_output_after_complete_result(suffix):
    raw, _, _ = lock_holder_print_output()
    with pytest.raises(json.JSONDecodeError):
        probe.parse_lock_holder_output(raw + suffix)


def test_lock_holder_requires_complete_matching_release():
    raw, ready, result = lock_holder_print_output()
    with pytest.raises(json.JSONDecodeError):
        probe.parse_lock_holder_output(raw[:-3])
    with pytest.raises(RuntimeError, match='original readiness'):
        probe.parse_lock_holder_output(json.dumps(ready) + '\n' + json.dumps({**result, 'boot': 'another-boot'}))
