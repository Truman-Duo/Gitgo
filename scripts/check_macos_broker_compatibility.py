"""Real old/new App-helper compatibility in a disposable SIP-enabled VM.

Both packages are local ad-hoc acceptance builds, not published releases. The
new App uses its actual managed command; the old App's frozen Host exercises
the retained old client. Root authority is used only inside the VM for helper
lifecycle and read-only evidence. No updater success matrix is repeated here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_macos_cross_version import (
    command_env, digest, exclusive_runtime_lock, output_json, private_artifacts, require,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--allow-local-adhoc', action='store_true')
    parser.add_argument('--payload-root', type=Path, required=True)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    args = parser.parse_args()
    require(args.run and args.allow_local_adhoc and sys.platform == 'darwin'
            and 501 <= os.getuid() < 200000000 and os.getuid() == os.geteuid(),
            'Use an ordinary disposable VM login account')
    require(not getattr(sys, 'frozen', False), 'Outer harness must hold no App runtime lease')
    model = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.model'], text=True).strip()
    sip = subprocess.check_output(['/usr/bin/csrutil', 'status'], text=True).strip()
    require(model.startswith('VirtualMac') and sip == 'System Integrity Protection status: enabled.',
            'A disposable SIP-enabled VirtualMac is required')
    available = shutil.disk_usage('/private/var/tmp').free
    require(available >= 2 * 1024**3, 'Reserve at least 2 GiB for the disposable installation and evidence')
    payload = args.payload_root.resolve(strict=True)
    plan = json.loads((payload / 'plan.json').read_bytes())
    require(plan.get('local_test_only') is True, 'Explicit local acceptance plan required')
    packages = {key: payload / key / 'payload' for key in ('a', 'new')}
    versions = {key: plan[key]['version'] for key in packages}
    require(versions['a'] != versions['new'], 'Two independently versioned builds are required')
    artifacts = private_artifacts(args.artifact_dir)
    require(not artifacts.is_relative_to(payload) and not payload.is_relative_to(artifacts),
            'Keep reports separate from package inputs')
    base = Path(tempfile.mkdtemp(prefix='gitgo-broker-compatibility-', dir='/private/var/tmp'))
    profile, temporary, project = (base / name for name in ('profile', 'tmp', 'project'))
    for path in (profile, temporary, project):
        path.mkdir(mode=0o700)
    app, bins = base / 'app', base / 'bin'
    environment = command_env(profile, temporary)
    public_command = bins / 'gitgo'
    scripts = Path(__file__).resolve().parent
    helper_probe = scripts / 'macos_cross_version_helper_probe.py'
    update_probe = scripts / 'macos_cross_version_update_probe.py'
    dashboard_probe = scripts / 'macos_dashboard_approval_probe.py'
    independent_host = packages['new'] / 'internal/gitgo-host/gitgo-host'
    admin = packages['new'] / 'sandbox-helper.sh'
    service = 'compat-' + uuid.uuid4().hex[:20]
    label = 'org.gitgo.sandbox.' + service
    registry = Path('/private/var/db/gitgo-sandbox-v1') / service
    plist = Path('/Library/LaunchDaemons') / (label + '.plist')
    boot_before = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True).strip()
    report = dict(passed=False, local_test_only=True, production_enabled=False,
                  publisher_trust_verified=False, public_network_tested=False,
                  virtual_machine=model, sip=sip, preflight_free_bytes=available,
                  fixture_root=str(base), artifacts=str(artifacts), service=service, plan=plan,
                  commands=[], matrix=[], checks={}, cleanup={})
    report['scripts'] = {p.name: digest(p) for p in (
        Path(__file__), helper_probe, update_probe, dashboard_probe,
        scripts / 'check_macos_cross_version.py', scripts / 'check_macos_managed_dashboard.py',
    )}
    attempted = completed = False
    failure = None
    command_index = 0
    promotion_id = None
    grant = credential_digest = None
    helper_paths = {}

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('Compatibility acceptance interrupted')

    signal.signal(signal.SIGTERM, interrupted)

    def run(name, command, timeout=120, allow_failure=False, cwd=base):
        nonlocal command_index
        command_index += 1
        stem = f'{command_index:02d}-{name}'
        outpath, errpath = artifacts / (stem + '.stdout'), artifacts / (stem + '.stderr')
        print('Broker compatibility: ' + name, file=sys.stderr, flush=True)
        with outpath.open('xb') as output, errpath.open('xb') as errors:
            proc = subprocess.Popen([str(value) for value in command], cwd=cwd, env=environment,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                    start_new_session=True)
            try:
                code = proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
                report['commands'].append(dict(name=name, log=stem, returncode=proc.returncode,
                                               interrupted=True, timeout=isinstance(exc, subprocess.TimeoutExpired)))
                raise RuntimeError(name + ' interrupted or timed out') from None
        report['commands'].append(dict(name=name, log=stem, returncode=code))
        require(allow_failure or code == 0, name + ' failed; see ' + stem + '.stderr')
        return code, outpath, errpath

    def host_command(host, script, *values):
        return [host, '--gitgo-internal-role', 'python', '-B', '-I', script, *values]

    def inspect(name):
        _, output, _ = run(name, host_command(independent_host, update_probe,
            '--run', '--allow-local-adhoc', '--root', app, '--command', public_command,
            '--artifact-dir', artifacts / name, 'inspect'))
        value = output_json(output)
        require(value.get('passed') is True, 'Managed App inspection failed')
        return value

    def audit(name):
        _, output, _ = run(name, ['/usr/bin/sudo', '-n', *host_command(independent_host,
            helper_probe, '--run', '--allow-local-adhoc', 'audit', '--service', service)])
        value = output_json(output)
        require(value.get('passed') is True and value.get('all_clean') is True,
                'Helper is not clean and idle: ' + name)
        return value

    def file_identity():
        path = project / 'fixture.txt'
        info = path.stat()
        return dict(sha256=digest(path), device=info.st_dev, inode=info.st_ino,
                    mtime_ns=info.st_mtime_ns, size=info.st_size)

    def stable_entry(name):
        _, output, _ = run(name + '-public-entry', [public_command, '--version'])
        require(output.read_text().strip() == 'Gitgo ' + versions['new'], 'Original command opened a different App')
        return exclusive_runtime_lock(app)

    def unchanged_authority(state):
        require(state['grant'] == grant and digest(registry / 'client.json') == credential_digest,
                'Helper transition changed the existing project authorization or credential')

    def cell(name, client_key, helper_key, *, incompatible=False):
        before = audit(name + '-before')
        unchanged_authority(before)
        before_file = file_identity()
        host = (app / 'active/internal/gitgo-host/gitgo-host' if client_key == 'new'
                else packages['a'] / 'internal/gitgo-host/gitgo-host')
        values = ['--run', '--allow-local-adhoc', 'client', '--service', service,
                  '--workspace', project, '--commit', commit, '--expected-version', versions[client_key]]
        if incompatible:
            values.append('--expect-incompatible-helper')
        elif promotion_id:
            values.extend(['--promotion-id', promotion_id])
        _, output, _ = run(name + '-client', host_command(host, helper_probe, *values), timeout=180)
        result = output_json(output)
        require(result.get('passed') is True and result.get('existing_native_history_unchanged') is True
                and result.get('no_pending_native_invocations') is True, 'Client observation is incomplete')
        after = audit(name + '-after')
        unchanged_authority(after)
        require(before['service']['helper'] == after['service']['helper'] == helper_paths[helper_key],
                'Unexpected helper identity')
        require(file_identity() == before_file, 'Compatibility probe changed a project file')
        if incompatible:
            require(result['outcome'].get('error_code') == 'HELPER_UPGRADE_REQUIRED'
                    and result['outcome'].get('incompatible_helper_rejected') is True,
                    'Missing explicit upgrade-required refusal')
            require(len(result['native_invocations']) == 1
                    and result['native_invocations'][0]['launch_state'] == 'not_started',
                    'Incompatible helper must be rejected before launch')
            require(before['seen_sha256'] == after['seen_sha256']
                    and before['seen_count'] == after['seen_count']
                    and before['invocations'] == after['invocations']
                    and before['service_sha256'] == after['service_sha256'],
                    'Rejected compatibility probe changed root replay or invocation evidence')
        report['matrix'].append(dict(name=name, app_version=versions[client_key],
            helper=helper_paths[helper_key], expected_refusal=incompatible, client=result,
            before=before, after=after, project_unchanged=True, runtime_lock=stable_entry(name)))

    def switch(name, candidate, *, install=False):
        values = ['/usr/bin/sudo', '-n', admin, 'install' if install else 'upgrade',
                  '--name', service, '--bundle', packages[candidate] / 'internal/gitgo-host']
        if install:
            values.extend(['--project', project, '--uid', str(os.getuid()), '--lifetime', '3600'])
        values.append('--allow-local-adhoc')
        _, output, _ = run(name, values, timeout=180)
        value = output_json(output)
        path = value['result']['helper']
        if candidate in helper_paths:
            require(path == helper_paths[candidate], 'Restored helper identity differs from retained build')
        helper_paths[candidate] = path
        report.setdefault('transitions', []).append(dict(name=name, result=value))

    try:
        run('sudo-authority', ['/usr/bin/sudo', '-n', '/usr/bin/true'])
        require(not os.path.lexists(registry) and not os.path.lexists(plist), 'Service name collision')
        for key, package in packages.items():
            expected = plan[key]
            require(digest(payload / key / 'package.tar.gz') == expected['archive_sha256'], 'Archive identity mismatch')
            _, output, _ = run(key + '-package-contract', host_command(package / 'internal/gitgo-host/gitgo-host',
                scripts / 'check_macos_managed_dashboard.py', '--run', '--allow-local-adhoc', '--verify-package',
                '--package', package, '--source-commit', expected['commit'], '--source-tree', expected['tree']))
            require(output_json(output)['version'] == versions[key], 'Package version mismatch')
        _, output, _ = run('install-managed-new-app', ['/bin/bash', packages['new'] / 'install.sh', '--managed',
            '--no-path-update', '--install-root', app, '--bin-dir', bins], timeout=180)
        report['installation'] = output_json(output)
        require(report['installation'].get('managed') is True, 'Managed installation not created')
        # Normal preference API, not a control-file edit. No live publisher is
        # contacted while testing only the helper protocol and Dashboard.
        configure = ('import sys;from pathlib import Path;'
            'from backend.core.update.installation import open_managed;'
            'from backend.core.update.automatic import preferences;'
            'from backend.core.update.preparation_service import UpdatePreparationService;'
            'root=Path(sys.argv[1]);store=open_managed(root);settings=preferences(store);'
            'service=UpdatePreparationService(store_loader=lambda:open_managed(root));'
            'service.configure("preview",False,False,False,settings["revision"]);'
            'service.close();print("automatic update checks disabled for local fixture")')
        run('local-update-preferences', host_command(independent_host, '-c', configure, app))
        initial = inspect('inspect-initial-app')
        report['initial_app'] = initial
        stable_entry('initial')
        run('fixture-init', ['/usr/bin/git', 'init', '-q'], cwd=project)
        (project / 'fixture.txt').write_bytes(b'sealed fixture\n')
        run('fixture-add', ['/usr/bin/git', 'add', 'fixture.txt'], cwd=project)
        run('fixture-commit', ['/usr/bin/git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'Broker compatibility fixture'], cwd=project)
        _, output, _ = run('fixture-head', ['/usr/bin/git', 'rev-parse', 'HEAD'], cwd=project)
        commit = output.read_text().strip()
        require(re.fullmatch('[0-9a-f]{40}', commit), 'Missing immutable fixture snapshot')
        attempted = True
        switch('install-old-helper', 'a', install=True)
        state = audit('initial-helper-authorization')
        grant = state['grant']
        credential_digest = digest(registry / 'client.json')
        report['checks']['credential_sha256'] = credential_digest
        cell('new-app-old-helper-refused', 'new', 'a', incompatible=True)
        switch('upgrade-helper-to-new', 'new')
        require(helper_paths['new'] != helper_paths['a'], 'Helper versions must have distinct sealed identities')
        cell('old-app-new-helper', 'a', 'new')
        cell('new-app-new-helper', 'new', 'new')
        approval_dir = artifacts / 'dashboard-approval'
        approval_dir.mkdir(mode=0o700)
        _, output, _ = run('dashboard-new-app-new-helper', host_command(independent_host, dashboard_probe,
            '--service', service, '--workspace', project, '--commit', commit, '--command', public_command,
            '--artifact-dir', approval_dir), timeout=300)
        report['approval'] = output_json(output)
        require(report['approval'].get('passed') is True, 'Real Dashboard approval did not pass')
        promotion_id = report['approval']['promotion_id']
        require(promotion_id and 'repeat verified without a second write' in report['approval']['events'],
                'Missing actual Dashboard approval and repeat evidence')
        report['checks']['approved_file'] = file_identity()
        cell('new-app-new-helper-after-approval', 'new', 'new')
        # A candidate that really cannot start must retain the live service.
        broken = base / 'inert-helper'
        broken.mkdir()
        shutil.copyfile('/usr/bin/false', broken / 'gitgo-host')
        (broken / 'gitgo-host').chmod(0o755)
        run('sign-inert-helper', ['/usr/bin/codesign', '--force', '--sign', '-', broken / 'gitgo-host'])
        before_failure = audit('before-helper-startup-failure')
        code, _, errors = run('helper-startup-failure', ['/usr/bin/sudo', '-n', admin, 'upgrade', '--name', service,
            '--bundle', broken, '--allow-local-adhoc'], timeout=180, allow_failure=True)
        require(code != 0 and 'prior service restored' in errors.read_text(), 'Failed helper was not safely restored')
        after_failure = audit('after-helper-startup-failure')
        unchanged_authority(after_failure)
        require(after_failure['service']['helper'] == helper_paths['new']
                and before_failure['seen_sha256'] == after_failure['seen_sha256']
                and before_failure['invocations'] == after_failure['invocations'],
                'Failed helper replacement changed the active version or replayed a request')
        report['checks']['failed_helper_restored'] = dict(before=before_failure, after=after_failure)
        cell('new-app-new-helper-after-failure', 'new', 'new')
        switch('rollback-helper-to-old', 'a')
        cell('new-app-old-helper-after-rollback-refused', 'new', 'a', incompatible=True)
        switch('restore-new-helper', 'new')
        cell('new-app-new-helper-restored', 'new', 'new')
        final = inspect('inspect-final-app')
        report['final_app'] = final
        require(all(initial[key] == final[key] for key in
            ('current', 'rollback', 'transaction', 'active_tree_sha256', 'pending')),
            'Helper transitions changed managed App identity or updater state')
        require(file_identity() == report['checks']['approved_file'], 'A later transition rewrote the approved file')
        report['checks']['managed_app_and_update_ledger_unchanged'] = True
        report['helper_versions'] = helper_paths
        # Deliberately keep the unknown business outcome in this disposable
        # profile. Run last: normal admission must block all subsequent native
        # work, even after status proves that the helper has cleaned the UID.
        fault_before = audit('before-real-started-ack-loss')
        _, output, _ = run('real-started-ack-loss', host_command(app / 'active/internal/gitgo-host/gitgo-host',
            helper_probe, '--run', '--allow-local-adhoc', 'exec-ack-loss', '--service', service,
            '--workspace', project, '--expected-version', versions['new']), timeout=90)
        fault = output_json(output)
        require(fault.get('passed') is True and fault.get('expected_outcome_debt') is True,
                'Lost actual launch acknowledgement did not retain its unknown outcome')
        report['exec_ack_loss'] = fault
        # A fresh frozen Host uses the normal status reconciler. It must keep
        # the original CAS evidence and pending debt; it cannot forge success.
        _, output, _ = run('real-started-ack-loss-recovery', host_command(app / 'active/internal/gitgo-host/gitgo-host',
            helper_probe, '--run', '--allow-local-adhoc', 'exec-recover', '--service', service,
            '--workspace', project, '--expected-version', versions['new'],
            '--invocation-id', fault['invocation_id'], '--original-ref', fault['original_ref']), timeout=90)
        recovery = output_json(output)
        require(recovery.get('passed') is True and recovery.get('cleanup_confirmed') is True
                and recovery.get('expected_outcome_debt') is True and recovery.get('no_replay') is True,
                'Normal recovery did not preserve the uncertainty and block replay')
        report['exec_ack_loss_recovery'] = recovery
        fault_after = audit('after-real-started-ack-loss-recovery')
        unchanged_authority(fault_after)
        before_rows = {row['invocation_id']: row for row in fault_before['invocations']}
        after_rows = {row['invocation_id']: row for row in fault_after['invocations']}
        identifier = fault['invocation_id']
        require(set(after_rows) == set(before_rows) | {identifier} and identifier not in before_rows
                and all(after_rows[key] == row for key, row in before_rows.items())
                and fault_after['seen_count'] == fault_before['seen_count'] + 1,
                'The actual fault/recovery ran more than the one original invocation')
        require(after_rows[identifier]['clean'] and after_rows[identifier]['launch_count'] == 1
                and not after_rows[identifier]['owned_rows'], 'Actual fault execution did not clean its dedicated UID')
        require(file_identity() == report['checks']['approved_file'], 'Fault recovery changed approved project output')
        report['checks']['ack_loss_one_launch_no_replay'] = dict(before=fault_before, after=fault_after,
            retained_outcome='NATIVE_OUTCOME_UNCONFIRMED', original_ref=fault['original_ref'],
            recovery_ref=recovery['recovery_ref'], runtime_lock=stable_entry('after-ack-loss'))
        completed = True
    except (Exception, KeyboardInterrupt) as exc:
        import traceback
        failure = type(exc).__name__ + ': ' + str(exc)
        report['traceback'] = traceback.format_exc()[-12000:]
    finally:
        if attempted:
            try:
                report['cleanup']['audit'] = audit('final-helper-audit')
            except Exception as exc:
                report['cleanup']['audit_error'] = str(exc)
                failure = failure or 'Final helper audit failed'
            try:
                code, _, _ = run('helper-uninstall', ['/usr/bin/sudo', '-n', admin, 'uninstall', '--name', service],
                                 timeout=180, allow_failure=True)
                gone, _, _ = run('helper-unloaded', ['/usr/bin/sudo', '-n', '/bin/launchctl', 'print', 'system/' + label],
                                 allow_failure=True)
                report['cleanup'].update(uninstall_returncode=code, launchd_unloaded=gone == 113,
                    credential_removed=not os.path.lexists(registry / 'client.json'),
                    socket_removed=not os.path.lexists(registry / 'broker.sock'),
                    launch_plist_removed=not os.path.lexists(plist))
                require(code == 0 and gone == 113 and all(report['cleanup'][key] for key in
                    ('credential_removed', 'socket_removed', 'launch_plist_removed')), 'Helper cleanup incomplete')
                report['cleanup']['verified'] = True
            except Exception as exc:
                report['cleanup']['error'] = str(exc)
                failure = failure or 'Cleanup failed'
        else:
            report['cleanup']['helper_not_attempted'] = True
        boot_after = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True).strip()
        report['boot'] = dict(before=boot_before, after=boot_after, unchanged=boot_before == boot_after)
        if failure:
            report['error'] = failure
        report['passed'] = completed and failure is None and report['cleanup'].get('verified') is True and boot_before == boot_after
        (artifacts / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
