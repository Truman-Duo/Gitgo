"""Fixed frozen helper administration and pre-stop validation in a disposable VM.

This deliberately admits local ad-hoc builds; it does not authenticate a real
publisher. The ordinary-user outer controller uses sudo only inside a SIP-enabled
VirtualMac. Faults affect a uniquely marked helper image created by this run,
never an earlier package, receipt, grant or invocation record. The public helper
entry performs every install, upgrade, recovery and uninstall.
"""
from __future__ import annotations

import argparse
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import fcntl
    import pwd

from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_macos_cross_version import command_env, digest, output_json, private_artifacts, require

MARKER = 'gitgo-helper-entry-acceptance.json'
SERVICE = r'entry-[0-9a-f]{20}'
MAX_LOG = 16 * 1024 * 1024
ACL_ERROR = 'Extended ACL entries are unsupported for administrative authority'


def vm_guard(*, root=False):
    require(sys.platform == 'darwin', 'macOS is required')
    require((os.getuid() == os.geteuid() == 0) if root else
            (501 <= os.getuid() < 200000000 and os.getuid() == os.geteuid()),
            'Use the intended disposable VM account')
    model = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.model'], text=True).strip()
    sip = subprocess.check_output(['/usr/bin/csrutil', 'status'], text=True).strip()
    require(model.startswith('VirtualMac') and sip == 'System Integrity Protection status: enabled.',
            'A disposable SIP-enabled VirtualMac is required')
    return model, sip


def marker_bytes(service):
    require(re.fullmatch(SERVICE, service), 'Not a helper-entry fixture service')
    return (json.dumps(dict(local_test_only=True, service=service, intent='unique disposable helper entry fixture'),
                       sort_keys=True) + '\n').encode()


def recovery_fixture(state, old_helper):
    require(state['phase'] == 'active' and state['previous'] == old_helper
            and state['helper'] != old_helper and state['candidate'] is None,
            'Recovery fault requires the immediately preceding disposable helper')
    return {**state, 'phase': 'switching', 'candidate': state['helper']}


def unchanged_service(before, after):
    """A preflight refusal must not stop/relaunch or mutate authority/evidence."""
    require(before['pid'] is not None and before['pid'] == after['pid'],
            'Rejected operation stopped or restarted the healthy helper')
    for field in ('service', 'service_sha256', 'plist_sha256', 'credential_sha256',
                  'grant', 'seen_sha256', 'seen_count', 'invocations', 'lifecycle_records'):
        require(before[field] == after[field], 'Rejected operation changed ' + field)
    require(before['all_clean'] and after['all_clean'], 'Root execution evidence is not clean')


def acl_entries(path):
    """Read the kernel's ACL presentation, without relying on the code under test."""
    result = subprocess.run(['/bin/ls', '-lde', str(path)], capture_output=True, text=True, timeout=10)
    require(result.returncode == 0 and len(result.stdout) < 16384, 'Could not inspect the fixture marker ACL')
    lines = result.stdout.splitlines()
    require(bool(lines), 'Missing ACL observation')
    entries = []
    for line in lines[1:]:
        found = re.fullmatch(r'\s*(\d+):\s+(.*)', line)
        require(found and int(found[1]) == len(entries), 'Unexpected ACL listing')
        entries.append(found[2])
    return entries


def change_marker_acl(path, account, *, add):
    """Change exactly one known ACE; never clear unrelated or inherited ACLs."""
    require(re.fullmatch(r'[A-Za-z0-9_.-]+', account), 'Unexpected local fixture account')
    entry = 'user:' + account + ' allow read,write'
    spellings = acl_spellings(account)
    before = acl_entries(path)
    ours = len(before) == 1 and before[0] in spellings
    require((add and before == []) or (not add and (ours or before == [])),
            'Refuse to change a pre-existing or unexpected marker ACL')
    if add or before:
        result = subprocess.run(['/bin/chmod', '+a' if add else '-a', entry, str(path)],
                                capture_output=True, text=True, timeout=10)
        require(result.returncode == 0, 'Could not change the single fixture ACE: ' + result.stderr[-1000:])
    after = acl_entries(path)
    require((add and len(after) == 1 and after[0] in spellings) or (not add and after == []),
            'Marker ACL differs from the expected single test ACE')
    return dict(before=before, after=after, exact_entry=entry, changed=bool(add or before))


def acl_spellings(account):
    # Directory Services may display a UUID instead of the user short name.
    # Ask it for that exact user's UUID; do not infer an identity from text.
    result = subprocess.run(['/usr/bin/dsmemberutil', 'getuuid', '-U', account],
                            capture_output=True, text=True, timeout=10)
    principal = result.stdout.strip().upper()
    require(result.returncode == 0 and re.fullmatch(r'[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}', principal),
            'Could not resolve the fixture ACL principal')
    return {value + ' allow read,write' for value in ('user:' + account, principal, 'user:' + principal)}


def marker_content(path):
    """Read the deliberately ACL-bearing marker without production trust admission."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o644 and before.st_size <= 4096,
                'Unexpected root-owned fixture marker')
        raw = os.read(fd, 4097)
        after = os.fstat(fd)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and len(raw) == before.st_size, 'Fixture marker changed while reading')
        return raw
    finally:
        os.close(fd)


def wait_for_boot_recovery(recover, *, recovery_busy=None, clock=time.monotonic, sleep=time.sleep, budget=120):
    """Instrument setup contention only; never modify product service timeouts."""
    started = clock()
    waits = []
    budgets = []
    attempts = 0
    while True:
        remaining = min(120, budget - (clock() - started))
        if remaining <= 0:
            return dict(passed=False, mode='boot-recovery', attempts=attempts,
                        elapsed_seconds=clock() - started, waits=waits, call_budgets=budgets,
                        error='Allocation lock remained busy for the bounded preflight', timed_out=True)
        attempts += 1
        arguments = dict(seconds=remaining, lock_seconds=min(30, remaining))
        budgets.append(dict(attempt=attempts, **arguments))
        try:
            changed = recover(**arguments)
            require(type(changed) is list and len(changed) <= 10000
                    and all(type(value) is str and re.fullmatch('[0-9a-f]{32}', value) for value in changed),
                    'Unexpected normal boot recovery result')
            return dict(passed=True, mode='boot-recovery', attempts=attempts,
                        elapsed_seconds=clock() - started, waits=waits,
                        recovered_count=len(changed), recovered_leases=changed,
                        recovered_count_scope='successful_final_attempt_only', call_budgets=budgets,
                        normal_recovery_completed=True, product_startup_timeout_unchanged=True)
        except Exception as exc:
            elapsed = clock() - started
            retryable = ((recovery_busy is not None and type(exc) is recovery_busy)
                         or (isinstance(exc, BlockingIOError) and exc.errno == 35))
            if not retryable:
                return dict(passed=False, mode='boot-recovery', attempts=attempts,
                            elapsed_seconds=elapsed, waits=waits, call_budgets=budgets,
                            error=type(exc).__name__ + ': ' + str(exc), retryable=False)
            waits.append(dict(attempt=attempts, elapsed_seconds=elapsed,
                              error_type=type(exc).__name__, errno=getattr(exc, 'errno', None)))
            if elapsed >= budget:
                return dict(passed=False, mode='boot-recovery', attempts=attempts,
                            elapsed_seconds=elapsed, waits=waits, call_budgets=budgets,
                            error='Allocation lock remained busy for the bounded preflight', timed_out=True)
            sleep(min(1.0, budget - elapsed))


def initial_failure_without_execution(state, *, ledger_present, invocations, active_present, launch_code):
    require(state['phase'] == 'blocked' and state['helper'] is None
            and state['previous'] is None and state['candidate'] is None,
            'Missing execution ledger is only acceptable after initial blocked installation')
    require(not ledger_present and not invocations and not active_present and launch_code == 113,
            'Initial-install cleanup cannot establish absence of execution evidence')


def matching_busy_startup(record, state, *, previous_nonce, old_helper, boot):
    return (type(record) is dict and record.get('schema') == 1
            and type(record.get('nonce')) is str and re.fullmatch('[0-9a-f]{32}', record['nonce']) is not None
            and record['nonce'] != previous_nonce and record.get('boot') == boot
            and state.get('phase') == 'switching' and state.get('helper') == old_helper
            and state.get('candidate') != old_helper and record.get('helper') == state.get('candidate')
            and type(record.get('pid')) is int and record['pid'] > 1
            and type(record.get('starts')) is int and record['starts'] == 1
            and record.get('phase') == 'recover_busy' and record.get('error') == 'RECOVERY_LOCK_BUSY'
            and type(record.get('lock_waits')) is int and record['lock_waits'] > 0)


def same_startup_became_ready(busy, ready, pid, helper):
    require(type(ready) is dict and ready.get('schema') == 1 and ready.get('phase') == 'ready'
            and ready.get('error') is None and type(ready.get('starts')) is int and ready['starts'] == 1
            and ready.get('helper') == helper and ready.get('pid') == pid
            and all(ready.get(key) == busy.get(key) for key in ('nonce', 'pid', 'boot', 'helper'))
            and type(ready.get('lock_waits')) is int and ready['lock_waits'] >= busy['lock_waits'] > 0,
            'Busy recovery did not reach ready in the same single service start')


def parse_lock_holder_output(raw):
    """One flushed ready line, followed by one complete (possibly pretty) JSON."""
    require(type(raw) is str and len(raw.encode('utf-8')) <= MAX_LOG, 'Lock-holder output exceeds bound')
    first, separator, remainder = raw.partition('\n')
    require(separator and first.strip() and remainder.strip(), 'Incomplete lock-holder output')
    ready = json.loads(first)
    result = json.loads(remainder)
    require(type(ready) is dict and ready.get('event') == 'allocation_lock_held'
            and type(result) is dict and result.get('event') == 'allocation_lock_released',
            'Unexpected lock-holder output protocol')
    require(all(ready.get(key) == result.get(key) for key in ('service', 'lock_path', 'boot')),
            'Lock-holder release differs from its original readiness')
    return ready, result


def _root_probe(args):
    vm_guard(root=True)
    require(getattr(sys, 'frozen', False) and re.fullmatch(SERVICE, args.service),
            'Root probe requires the selected frozen Host and unique test service')
    from backend.core import macos_helper_lifecycle as life, macos_identity as identity
    from backend.core.macos_helper_install import BASE, verify_bundle_path
    from macos_cross_version_helper_probe import audit

    root = life.service_root(args.service)
    if args.probe == 'boot-recovery':
        from backend.core.macos_boot_recovery import recover_store, RecoveryBusy
        boot_command = ['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid']
        before_raw = subprocess.check_output(boot_command, text=True, timeout=3)
        require(identity.valid_boot_id(before_raw.strip().lower()), 'Invalid initial kernel boot identity')
        def timeout(_signal, _frame):
            raise TimeoutError('Normal boot recovery exceeded the 120 second preflight budget')
        previous = signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 120)
        try:
            result = wait_for_boot_recovery(recover_store, recovery_busy=RecoveryBusy)
        except TimeoutError as exc:
            result = dict(passed=False, mode='boot-recovery', error=str(exc), timed_out=True)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        after_raw = subprocess.check_output(boot_command, text=True, timeout=3)
        same = before_raw.strip().lower() == after_raw.strip().lower()
        result.update(total_budget_seconds=120, boot_before_raw=before_raw, boot_after_raw=after_raw,
                      boot_unchanged=same, passed=bool(result['passed'] and same))
        return result
    if args.probe == 'cleanup-observe':
        status = life.ctl('print', 'system/' + life.PREFIX + args.service)
        if not os.path.lexists(root):
            require(status.returncode == 113 and not os.path.lexists(life.job_path(root)),
                    'Missing registry has an unexpected launchd job')
            return dict(passed=True, mode=args.probe, registration_absent=True,
                        launchctl_returncode=113, all_clean=True, no_records_created=True)
        from backend.core.macos_helper_install import trusted_directory
        trusted_directory(root)
        state = life.configuration(root)
        ledger = root / 'private/seen.json'
        if not os.path.lexists(ledger):
            rows = list((root / 'private').glob('invocation-*'))
            initial_failure_without_execution(state, ledger_present=False, invocations=rows,
                active_present=os.path.lexists(root / 'private/active.json'), launch_code=status.returncode)
            life.assert_idle(root)
            return dict(passed=True, mode=args.probe, registration_absent=False,
                        service=state, all_clean=True, idle_verified=True,
                        launchctl_returncode=status.returncode, seen_present=False,
                        invocation_count=0, initial_install_never_reached_listener=True,
                        evidence_not_created_or_repaired=True)
        value = audit(args.service)
        value.update(mode=args.probe, registration_absent=False, launchctl_returncode=status.returncode)
        return value
    if args.probe == 'observe':
        value = audit(args.service)
        status = life.ctl('print', 'system/' + life.PREFIX + args.service)
        found = re.search(r'^\s*pid = (\d+)\s*$', status.stdout, re.M)
        require(status.returncode == 0 and found, 'Expected the live test helper')
        records = sorted((root / 'private').glob('lifecycle-*.json'))
        require(len(records) < 256, 'Lifecycle evidence exceeds test bounds')
        value.update(pid=int(found[1]),
                     plist_sha256=digest(life.job_path(root)),
                     credential_sha256=digest(root / 'client.json'),
                     lifecycle_records={p.name: digest(p) for p in records})
        startup = root / 'private/startup.json'
        value['startup'] = (json.loads(identity.bounded_read(startup, 0, True))
                            if os.path.lexists(startup) else None)
        return value
    require(args.helper, 'Fault fixture requires an exact sealed helper path')
    helper = Path(args.helper)
    require(helper.name == 'gitgo-host' and helper.parent.name == 'runtime'
            and helper.parent.parent.parent == BASE
            and re.fullmatch('[0-9a-f]{64}', helper.parent.parent.name), 'Unsafe fault image path')
    marker = helper.parent / '_internal' / MARKER
    original = marker_bytes(args.service)
    damaged = original + b'deliberately damaged local acceptance marker\n'
    receipt = helper.parent.parent / 'install.json'
    receipt_before = digest(receipt)
    if args.probe == 'hold-startup-lock':
        from backend.core.sandbox_macos import LOCK
        from backend.core.macos_file_authority import require_no_extended_acl
        require(str(LOCK) == '/private/tmp/gitgo-task-uid-probe-v1.lock', 'Unexpected allocation lock path')
        require(marker_content(marker) == original, 'Lock test is not bound to this run\'s unique old image')
        initial = life.configuration(root)
        require(initial['phase'] == 'active' and initial['helper'] == str(helper), 'Expected the active old helper')
        verify_bundle_path(helper)
        startup = root / 'private/startup.json'
        previous = json.loads(identity.bounded_read(startup, 0, True)) if os.path.lexists(startup) else {}
        previous_nonce = previous.get('nonce')
        boot = identity.boot_id()
        fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        busy = None
        started = time.monotonic()
        def expired(_signal, _frame):
            raise TimeoutError('Fixture allocation lock reached its 10 second lifetime')
        old_handler = signal.signal(signal.SIGALRM, expired)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600, 'Unexpected existing allocation lock')
            require_no_extended_acl(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, 10)
            print(json.dumps(dict(event='allocation_lock_held', holder_pid=os.getpid(), service=args.service,
                lock_path=str(LOCK), lock_device=info.st_dev, lock_inode=info.st_ino,
                boot=boot, previous_nonce=previous_nonce, max_hold_seconds=10)), flush=True)
            while time.monotonic() - started < 10:
                if os.path.lexists(startup):
                    record = json.loads(identity.bounded_read(startup, 0, True))
                    state = life.configuration(root)
                    if matching_busy_startup(record, state, previous_nonce=previous_nonce,
                                             old_helper=str(helper), boot=boot):
                        busy = record
                        break
                time.sleep(.02)
        except TimeoutError:
            pass
        finally:
            os.close(fd)
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
        return dict(passed=busy is not None, mode=args.probe, event='allocation_lock_released',
                    service=args.service, lock_path=str(LOCK), boot=boot,
                    held_seconds=time.monotonic() - started, max_hold_seconds=10,
                    observed_busy=busy, released=True, lock_not_deleted=True,
                    error=None if busy is not None else 'No bound new-helper recovery contention was observed')
    if args.probe in ('add-marker-acl', 'remove-marker-acl'):
        from backend.core.macos_helper_install import trusted_directory
        trusted_directory(marker.parent)
        with life.locked(root, 'admin.lock'), life.locked(root, 'admission.lock'):
            state = life.configuration(root)
            require(str(helper) in (state['helper'], state['previous']), 'Image is not owned by this fixture state')
            require(marker_content(marker) == original, 'Refuse ACL changes on a non-fixture marker')
            grant = json.loads(identity.bounded_read(root / 'private/grant.json', 0, True))
            uid = grant['owner_uid']
            require(type(uid) is int and 501 <= uid < 200000000, 'Expected the fixture login identity')
            add = args.probe == 'add-marker-acl'
            account = pwd.getpwuid(uid).pw_name
            proof_path = root / 'private/helper-entry-acl-fixture.json'
            proof = dict(local_test_only=True, service=args.service, helper=str(helper), marker=str(marker),
                         marker_sha256=digest(marker), install_receipt_sha256=receipt_before,
                         entry='user:' + account + ' allow read,write', original_acl=[])
            if add:
                verify_bundle_path(helper)
                require(acl_entries(marker) == [], 'ACL injection requires an originally ACL-free marker')
                require(not os.path.lexists(proof_path), 'ACL fixture already has retained evidence')
                # Save ownership before changing authority. If the syscall
                # succeeds but this process loses its response, cleanup can
                # distinguish our ACE from a pre-existing one. This is a new
                # explicitly labelled test record, never an install receipt.
                identity.atomic_json(proof_path, {**proof, 'phase': 'prepared'})
            elif os.path.lexists(proof_path):
                saved = json.loads(identity.bounded_read(proof_path, 0, True))
                require({key: value for key, value in saved.items() if key != 'phase'} == proof
                        and saved.get('phase') in ('prepared', 'added', 'removed'),
                        'ACL restoration is not bound to this exact fixture marker')
            else:
                require(acl_entries(marker) == [], 'No evidence that this test owns the marker ACL')
            details = change_marker_acl(marker, account, add=add)
            require(marker_content(marker) == original, 'ACL fixture changed marker bytes')
            if not add:
                verify_bundle_path(helper)
            if os.path.lexists(proof_path):
                identity.atomic_json(proof_path, {**proof, 'phase': 'added' if add else 'removed'})
        require(digest(receipt) == receipt_before, 'ACL fixture modified an install receipt')
        return dict(passed=True, mode=args.probe, helper=str(helper), acl=details,
                    marker_sha256=digest(marker), marker_bytes_unchanged=True,
                    receipt_sha256=receipt_before, receipt_unchanged=True, local_test_only=True)
    if args.probe in ('damage-marker', 'restore-marker'):
        from backend.core.macos_helper_install import trusted_directory
        trusted_directory(marker.parent)
        with life.locked(root, 'admin.lock'), life.locked(root, 'admission.lock'):
            state = life.configuration(root)
            require(str(helper) in (state['helper'], state['previous']), 'Image is not owned by this fixture state')
            old, new = (original, damaged) if args.probe == 'damage-marker' else (damaged, original)
            require(identity.bounded_read(marker, 0) == old, 'Refuse to alter a non-fixture marker')
            if args.probe == 'damage-marker':
                verify_bundle_path(helper)
            fd = os.open(marker, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                require(os.write(fd, new) == len(new), 'Short marker write')
                os.fsync(fd)
            finally:
                os.close(fd)
            if args.probe == 'restore-marker':
                verify_bundle_path(helper)
        require(digest(receipt) == receipt_before, 'Fault fixture modified an install receipt')
        return dict(passed=True, mode=args.probe, helper=str(helper), marker_sha256=digest(marker),
                    receipt_sha256=receipt_before, receipt_unchanged=True, local_test_only=True)
    require(args.probe == 'stage-recovery', 'Unexpected root probe')
    require(identity.bounded_read(marker, 0) == original, 'Only this run\'s healthy old helper may be a recovery target')
    verify_bundle_path(helper)
    with life.locked(root, 'admin.lock'), life.locked(root, 'admission.lock'):
        life.assert_idle(root)
        state = life.configuration(root)
        target = recovery_fixture(state, str(helper))
        # Explicit injection of the durable maintenance boundary. The running
        # new helper remains alive, allowing proof that failed target checking
        # does not stop it. Original lifecycle records remain append-only.
        life.save(root, target)
    return dict(passed=True, mode=args.probe, before=state, after=target,
                receipt_sha256=receipt_before, local_test_only=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--allow-local-adhoc', action='store_true')
    parser.add_argument('--payload-root', type=Path)
    parser.add_argument('--artifact-dir', type=Path)
    parser.add_argument('--probe', choices=('observe', 'damage-marker', 'restore-marker', 'stage-recovery',
                                           'add-marker-acl', 'remove-marker-acl', 'boot-recovery', 'cleanup-observe',
                                           'hold-startup-lock'))
    parser.add_argument('--service')
    parser.add_argument('--helper')
    args = parser.parse_args()
    require(args.run and args.allow_local_adhoc, 'Explicit local acceptance consent is required')
    if args.probe:
        print(json.dumps(_root_probe(args), indent=2), flush=True)
        return
    model, sip = vm_guard()
    require(not getattr(sys, 'frozen', False), 'Use an ordinary Python controller outside App runtime locks')
    require(args.payload_root and args.artifact_dir, 'Package input and private report directories are required')
    payload = args.payload_root.resolve(strict=True)
    artifacts = private_artifacts(args.artifact_dir)
    require(not artifacts.is_relative_to(payload) and not payload.is_relative_to(artifacts),
            'Keep reports separate from package inputs')
    available = shutil.disk_usage('/private/var/tmp').free
    require(available >= 2 * 1024**3, 'Reserve at least 2 GiB for the disposable helper images')
    plan = json.loads((payload / 'plan.json').read_bytes())
    require(plan.get('local_test_only') is True, 'Explicit local acceptance plan required')
    packages = {key: payload / key / 'payload' for key in ('a', 'new')}
    require(plan['a']['version'] != plan['new']['version'], 'Two independently versioned builds required')
    scripts = Path(__file__).resolve().parent
    host = packages['new'] / 'internal/gitgo-host/gitgo-host'
    helper_probe = scripts / 'macos_cross_version_helper_probe.py'
    base = Path(tempfile.mkdtemp(prefix='gitgo-helper-entry-', dir='/private/var/tmp'))
    profile, temporary, project = (base / name for name in ('profile', 'tmp', 'project'))
    for path in (profile, temporary, project):
        path.mkdir(mode=0o700)
    environment = command_env(profile, temporary)
    service = 'entry-' + uuid.uuid4().hex[:20]
    registry = Path('/private/var/db/gitgo-sandbox-v1') / service
    label = 'org.gitgo.sandbox.' + service
    plist = Path('/Library/LaunchDaemons') / (label + '.plist')
    boot = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True).strip()
    report = dict(passed=False, local_test_only=True, production_enabled=False, publisher_trust_verified=False,
                  public_network_tested=False, virtual_machine=model, sip=sip, preflight_free_bytes=available,
                  fixture_root=str(base), artifacts=str(artifacts), service=service, plan=plan,
                  commands=[], matrix=[], cleanup={}, scripts={p.name: digest(p) for p in
                      (Path(__file__), helper_probe, scripts / 'check_macos_cross_version.py',
                       scripts / 'check_macos_managed_dashboard.py')})
    sequence = 0
    attempted = False
    old_helper = None
    damaged = False
    acl_may_be_added = False
    lock_holder = None
    failure = None

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt('Helper-entry acceptance interrupted')

    signal.signal(signal.SIGTERM, interrupted)

    def run(name, command, *, allow_failure=False, timeout=120, cwd=base):
        nonlocal sequence
        sequence += 1
        stem = f'{sequence:02d}-{name}'
        out, err = artifacts / (stem + '.stdout'), artifacts / (stem + '.stderr')
        print('Helper entry: ' + name, file=sys.stderr, flush=True)
        with out.open('xb') as stdout, err.open('xb') as stderr:
            proc = subprocess.Popen([str(value) for value in command], cwd=cwd, env=environment,
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            end = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    require(time.monotonic() < end, name + ' timed out')
                    require(out.stat().st_size + err.stat().st_size <= MAX_LOG, name + ' exceeded log bound')
                    time.sleep(.1)
            except BaseException:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
                report['commands'].append(dict(name=name, log=stem, returncode=proc.returncode, interrupted=True))
                raise
        report['commands'].append(dict(name=name, log=stem, returncode=proc.returncode))
        require(allow_failure or proc.returncode == 0, name + ' failed; see ' + stem + '.stderr')
        return proc.returncode, out, err

    def python_command(script, *values):
        return [host, '--gitgo-internal-role', 'python', '-B', '-I', script, *values]

    def probe(name, mode, helper=None):
        values = [*python_command(Path(__file__)), '--run', '--allow-local-adhoc', '--probe', mode, '--service', service]
        if helper:
            values.extend(['--helper', helper])
        _, out, _ = run(name, ['/usr/bin/sudo', '-n', *values], timeout=180 if mode == 'boot-recovery' else 120)
        result = output_json(out)
        require(result.get('passed') is True, 'Root fixture probe did not complete')
        return result

    def admin(name, operation, *values, allow_failure=False, root=True):
        command = [admin_entry, operation, '--name', service, *values]
        if root:
            command = ['/usr/bin/sudo', '-n', *command]
        # Product readiness has its own 180-second hard ceiling. Preserve
        # bounded room for staging, unload and a failed candidate's rollback.
        return run(name, command, allow_failure=allow_failure, timeout=360)

    def healthy_client(name):
        _, out, _ = run(name, python_command(helper_probe, '--run', '--allow-local-adhoc', 'client',
            '--service', service, '--workspace', project, '--commit', commit,
            '--expected-version', plan['new']['version']), timeout=180)
        value = output_json(out)
        require(value.get('passed') is True and value.get('no_pending_native_invocations') is True,
                'Ordinary frozen client could not use the healthy helper')
        return value

    def start_lock_holder():
        nonlocal sequence, lock_holder
        sequence += 1
        stem = f'{sequence:02d}-allocation-lock-holder'
        out, err = artifacts / (stem + '.stdout'), artifacts / (stem + '.stderr')
        command = ['/usr/bin/sudo', '-n', *python_command(Path(__file__)), '--run', '--allow-local-adhoc',
                   '--probe', 'hold-startup-lock', '--service', service, '--helper', old_helper]
        with out.open('xb') as stdout, err.open('xb') as stderr:
            proc = subprocess.Popen([str(v) for v in command], cwd=base, env=environment,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        evidence = dict(name='allocation-lock-holder', log=stem, background=True, returncode=None)
        report['commands'].append(evidence)
        lock_holder = dict(process=proc, stdout=out, stderr=err, evidence=evidence)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(out.stat().st_size + err.stat().st_size <= MAX_LOG, 'Lock holder exceeded log bound')
            raw = out.read_text()
            if '\n' in raw:
                first = json.loads(raw.splitlines()[0])
                require(first.get('event') == 'allocation_lock_held' and first.get('service') == service,
                        'Lock holder did not acquire the intended existing lock')
                report['startup_lock_held'] = first
                return
            require(proc.poll() is None, 'Lock holder exited before readiness; see ' + stem + '.stderr')
            time.sleep(.02)
        raise TimeoutError('Lock holder did not start within 30 seconds')

    def finish_lock_holder():
        require(lock_holder is not None, 'Missing lock-holder process')
        proc = lock_holder['process']
        proc.wait(timeout=15)
        lock_holder['evidence']['returncode'] = proc.returncode
        require(lock_holder['stdout'].stat().st_size <= MAX_LOG, 'Lock-holder result exceeds bound')
        ready, result = parse_lock_holder_output(lock_holder['stdout'].read_text())
        require(proc.returncode == 0 and ready == report['startup_lock_held']
                and result.get('passed') is True and result.get('released') is True,
                'Real bounded allocation-lock contention was not verified')
        return result

    def refusal(name, operation, *values, expected_error):
        before = probe(name + '-before', 'observe')
        code, out, err = admin(name, operation, *values, allow_failure=True)
        require(code != 0 and code != 91, 'Malformed candidate must be refused by frozen administration')
        diagnostic = (out.read_text() + err.read_text())[-8000:]
        require(expected_error in diagnostic, 'Refusal was not the intended integrity check: ' + name)
        after = probe(name + '-after', 'observe')
        unchanged_service(before, after)
        value = dict(name=name, before=before, after=after, returncode=code,
                     error=diagnostic, helper_not_stopped=True,
                     authority_and_evidence_unchanged=True)
        report['matrix'].append(value)
        return value

    # Thin entry fixture points to the verified real frozen runtime. Only the
    # historical external Python management script is hostile; the wrapper and
    # Host bytes remain those of the new independently built package.
    entry = base / 'entry'
    entry.mkdir()
    shutil.copyfile(packages['new'] / 'sandbox-helper.sh', entry / 'sandbox-helper.sh')
    (entry / 'sandbox-helper.sh').chmod(0o755)
    (entry / 'internal').symlink_to(packages['new'] / 'internal', target_is_directory=True)
    admin_entry = entry / 'sandbox-helper.sh'
    canary = base / 'EXTERNAL_MANAGEMENT_SCRIPT_EXECUTED'
    (entry / 'manage_macos_sandbox_service.py').write_text(
        'from pathlib import Path\nPath(' + repr(str(canary)) + ').write_text("unexpected execution")\nraise SystemExit(91)\n')
    old_bundle = base / 'unique-old-bundle'
    try:
        run('sudo-authority', ['/usr/bin/sudo', '-n', '/usr/bin/true'])
        require(not os.path.lexists(registry) and not os.path.lexists(plist), 'Service collision')
        for key, package in packages.items():
            expected = plan[key]
            require(digest(payload / key / 'package.tar.gz') == expected['archive_sha256'], 'Archive identity mismatch')
            _, out, _ = run(key + '-package-contract', [package / 'internal/gitgo-host/gitgo-host',
                '--gitgo-internal-role', 'python', '-B', '-I', scripts / 'check_macos_managed_dashboard.py',
                '--run', '--allow-local-adhoc', '--verify-package', '--package', package,
                '--source-commit', expected['commit'], '--source-tree', expected['tree']])
            require(output_json(out)['version'] == expected['version'], 'Package version mismatch')
        code, out, err = admin('nonroot-refused', 'install', '--bundle', packages['a'] / 'internal/gitgo-host',
            '--project', project, '--uid', str(os.getuid()), '--allow-local-adhoc', root=False, allow_failure=True)
        diagnostic = (out.read_text() + err.read_text())[-8000:]
        require(code != 0 and code != 91 and ('administrator' in diagnostic.lower()
                or 'root authority' in diagnostic.lower()), 'Missing administrator refusal')
        require(not canary.exists() and not os.path.lexists(registry) and not os.path.lexists(plist),
                'Non-root attempt executed external script or created a service')
        report['matrix'].append(dict(name='nonroot-refused', returncode=code, error=diagnostic, registration_absent=True))
        shutil.copytree(packages['a'] / 'internal/gitgo-host', old_bundle, symlinks=True)
        (old_bundle / '_internal' / MARKER).write_bytes(marker_bytes(service))
        run('fixture-init', ['/usr/bin/git', 'init', '-q'], cwd=project)
        (project / 'fixture.txt').write_text('helper entry fixture\n')
        run('fixture-add', ['/usr/bin/git', 'add', 'fixture.txt'], cwd=project)
        run('fixture-commit', ['/usr/bin/git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'Helper entry fixture'], cwd=project)
        _, out, _ = run('fixture-head', ['/usr/bin/git', 'rev-parse', 'HEAD'], cwd=project)
        commit = out.read_text().strip()
        require(re.fullmatch('[0-9a-f]{40}', commit), 'Missing fixed Git snapshot')
        report['boot_recovery_preflight'] = probe('normal-boot-recovery-preflight', 'boot-recovery')
        attempted = True
        _, out, _ = admin('fixed-role-install', 'install', '--bundle', old_bundle, '--project', project,
            '--uid', str(os.getuid()), '--lifetime', '3600', '--allow-local-adhoc')
        initial = output_json(out)
        old_helper = initial['result']['helper']
        require(initial['result']['phase'] == 'active' and not canary.exists(), 'External manager script was executed')
        report['matrix'].append(dict(name='fixed-role-ignores-external-manager', installation=initial,
            canary_absent=True, client=healthy_client('initial-client')))
        bad = base / 'bad-candidate'
        bad.mkdir()
        shutil.copyfile('/usr/bin/false', bad / 'gitgo-host')
        (bad / 'gitgo-host').chmod(0o755)
        (bad / 'external-link').symlink_to('/usr/bin/false')
        refusal('bad-candidate-preflight', 'upgrade', '--bundle', bad, '--allow-local-adhoc',
                expected_error='Helper contains an external symlink')
        healthy_client('after-bad-candidate-client')
        report['damage_current'] = probe('damage-current-fallback', 'damage-marker', old_helper)
        damaged = True
        refusal('damaged-current-fallback-preflight', 'upgrade', '--bundle', packages['new'] / 'internal/gitgo-host',
                '--allow-local-adhoc', expected_error='Installed helper bundle differs from its operator-approved snapshot')
        report['restore_current'] = probe('restore-current-fallback', 'restore-marker', old_helper)
        damaged = False
        healthy_client('after-current-restored-client')
        # Set cleanup responsibility before the syscall so an interrupted
        # response cannot leave the deliberate authority change behind.
        acl_may_be_added = True
        report['acl_added'] = probe('add-current-fallback-acl', 'add-marker-acl', old_helper)
        refusal('acl-current-fallback-preflight', 'upgrade', '--bundle', packages['new'] / 'internal/gitgo-host',
                '--allow-local-adhoc', expected_error=ACL_ERROR)
        report['acl_removed'] = probe('remove-current-fallback-acl', 'remove-marker-acl', old_helper)
        require(report['acl_removed']['acl']['changed'] is True
                and report['acl_added']['marker_sha256'] == report['acl_removed']['marker_sha256']
                and report['acl_added']['receipt_sha256'] == report['acl_removed']['receipt_sha256'],
                'ACL restoration did not preserve the original marker and receipt')
        acl_may_be_added = False
        healthy_client('after-acl-restored-client')
        startup_before = probe('before-startup-contention', 'observe')
        start_lock_holder()
        _, out, _ = admin('upgrade-to-new', 'upgrade', '--bundle', packages['new'] / 'internal/gitgo-host', '--allow-local-adhoc')
        report['upgraded'] = output_json(out)
        require(report['upgraded']['result']['previous'] == old_helper, 'Upgrade did not retain this fixture\'s old image')
        report['startup_lock_released'] = finish_lock_holder()
        ready_state = probe('new-helper-after-startup-contention', 'observe')
        same_startup_became_ready(report['startup_lock_released']['observed_busy'], ready_state.get('startup'),
                                 ready_state['pid'], report['upgraded']['result']['helper'])
        for field in ('grant', 'credential_sha256', 'seen_sha256', 'seen_count', 'invocations'):
            require(startup_before[field] == ready_state[field], 'Startup recovery changed authority or replay evidence: ' + field)
        report['matrix'].append(dict(name='new-helper-startup-allocation-contention',
            held=report['startup_lock_held'], released=report['startup_lock_released'],
            before=startup_before, ready=ready_state, single_start=True, normal_recovery=True,
            authority_and_invocations_unchanged=True))
        healthy_client('new-helper-client')
        report['recovery_fault'] = probe('stage-interrupted-recovery', 'stage-recovery', old_helper)
        report['damage_recovery'] = probe('damage-recovery-target', 'damage-marker', old_helper)
        damaged = True
        refusal('damaged-recovery-target-preflight', 'recover',
                expected_error='Installed helper bundle differs from its operator-approved snapshot')
        report['restore_recovery'] = probe('restore-recovery-target', 'restore-marker', old_helper)
        damaged = False
        _, out, _ = admin('normal-recover-old', 'recover')
        recovered = output_json(out)
        require(recovered['result']['helper'] == old_helper and recovered['result']['phase'] == 'active',
                'Recovery did not restore the retained old helper')
        report['recovered'] = recovered
        healthy_client('recovered-old-client')
        _, out, _ = admin('restore-new-helper', 'upgrade', '--bundle', packages['new'] / 'internal/gitgo-host', '--allow-local-adhoc')
        report['restored_new'] = output_json(out)
        healthy_client('restored-new-client')
        require(not canary.exists(), 'An external management script executed')
        report['external_manager_never_executed'] = True
        report['passed'] = True
    except BaseException as exc:
        failure = exc
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        import traceback
        report['traceback'] = traceback.format_exc()[-16000:]
    finally:
        if lock_holder is not None:
            try:
                lock_holder['process'].wait(timeout=15)
                lock_holder['evidence']['returncode'] = lock_holder['process'].returncode
                report['cleanup']['lock_holder_exited'] = True
            except subprocess.TimeoutExpired:
                try:
                    lock_holder['process'].terminate()
                    lock_holder['process'].wait(timeout=5)
                except Exception as exc:
                    report['cleanup']['lock_holder_termination_error'] = type(exc).__name__ + ': ' + str(exc)
                report['cleanup']['lock_holder_error'] = 'Fixture exceeded its own bounded lifetime'
                report['passed'] = False
        if attempted:
            try:
                if acl_may_be_added and old_helper:
                    report['cleanup']['removed_fixture_acl'] = probe('cleanup-remove-marker-acl', 'remove-marker-acl', old_helper)
                if damaged and old_helper:
                    report['cleanup']['restored_fixture_marker'] = probe('cleanup-restore-marker', 'restore-marker', old_helper)
                report['cleanup']['audit'] = probe('final-root-audit', 'cleanup-observe')
                require(report['cleanup']['audit']['all_clean'], 'Retain service while cleanup is uncertain')
                if not report['cleanup']['audit'].get('registration_absent'):
                    _, out, _ = admin('uninstall-fixture-helper', 'uninstall')
                    require(output_json(out)['result']['phase'] == 'removed', 'Service did not reach removed state')
                code, _, _ = run('verify-launchd-removed', ['/bin/launchctl', 'print', 'system/' + label], allow_failure=True)
                report['cleanup'].update(launchd_unloaded=code == 113,
                    credential_removed=not os.path.lexists(registry / 'client.json'),
                    socket_removed=not os.path.lexists(registry / 'broker.sock'),
                    launch_plist_removed=not os.path.lexists(plist))
                report['cleanup']['verified'] = all(report['cleanup'][key] for key in
                    ('launchd_unloaded', 'credential_removed', 'socket_removed', 'launch_plist_removed'))
                require(report['cleanup']['verified'], 'Helper cleanup incomplete')
            except BaseException as exc:
                report['cleanup']['error'] = type(exc).__name__ + ': ' + str(exc)
                report['passed'] = False
        after = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True).strip()
        report['boot'] = dict(before=boot, after=after, unchanged=boot == after)
        report['passed'] = bool(report['passed'] and report['cleanup'].get('verified') and boot == after)
        (artifacts / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    if failure or not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
