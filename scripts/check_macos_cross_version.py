"""App/helper cross-version acceptance in a disposable SIP-enabled macOS VM.

The supplied plan identifies independently built local A/B packages and a third
source-built deliberate startup failure. Only transport is injected by the update
probe; actual Dashboard confirmation, stable launcher, signature checks, runtime
locks, health checks, retained rollback and root-owned helper switching are real.
No production publisher or arbitrary future protocol compatibility is claimed.
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
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_macos_managed_dashboard import (
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
            and 501 <= os.getuid() < 200000000 and os.getuid() == os.geteuid(), 'Use an ordinary VM login account')
    require(not getattr(sys, 'frozen', False), 'Outer harness must not hold an App runtime lease')
    model = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.model'], text=True).strip()
    sip = subprocess.check_output(['/usr/bin/csrutil', 'status'], text=True).strip()
    require(model.startswith('VirtualMac') and sip == 'System Integrity Protection status: enabled.',
            'This acceptance requires a disposable SIP-enabled VM')
    available = shutil.disk_usage('/private/var/tmp').free
    require(available >= 3 * 1024**3,
            'Reserve at least 3 GiB of free VM space for retained update candidates and failure evidence')
    payload = args.payload_root.resolve(strict=True)
    plan = json.loads((payload / 'plan.json').read_bytes())
    require(plan.get('local_test_only') is True and plan.get('sandbox_protocol_unchanged') is True,
            'Explicit local compatibility fixture plan required')
    packages = {key: payload / key / 'payload' for key in ('a', 'b', 'failure')}
    versions = {key: plan[key]['version'] for key in packages}
    artifacts = private_artifacts(args.artifact_dir)
    base = Path(tempfile.mkdtemp(prefix='gitgo-cross-version-', dir='/private/var/tmp'))
    profile, temporary, project = [base / key for key in ('profile', 'tmp', 'project')]
    for directory in (profile, temporary, project): directory.mkdir(mode=0o700)
    app, bins = base / 'app', base / 'bin'
    environment = command_env(profile, temporary)
    service = 'versions-' + uuid.uuid4().hex[:20]
    label = 'org.gitgo.sandbox.' + service
    registry = Path('/private/var/db/gitgo-sandbox-v1') / service
    plist = Path('/Library/LaunchDaemons') / (label + '.plist')
    scripts = Path(__file__).resolve().parent
    update_probe = scripts / 'macos_cross_version_update_probe.py'
    helper_probe = scripts / 'macos_cross_version_helper_probe.py'
    dashboard_probe = scripts / 'macos_dashboard_approval_probe.py'
    independent_host = packages['b'] / 'internal/gitgo-host/gitgo-host'
    admin = packages['b'] / 'sandbox-helper.sh'
    public_command = bins / 'gitgo'
    report = dict(passed=False, production_enabled=False, publisher_trust_verified=False,
                  local_test_only=True, public_network_tested=False, sandbox_protocol_unchanged=True,
                  fixture_root=str(base), artifacts=str(artifacts), sip=sip, virtual_machine=model,
                  preflight_free_bytes=available,
                  plan=plan, service=service, commands=[], matrix=[], checks={}, cleanup={})
    report['scripts'] = {p.name: digest(p) for p in (Path(__file__), update_probe, helper_probe, dashboard_probe)}
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('Cross-version acceptance interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    boot_before = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True).strip()
    attempted = completed = False
    failure = None
    command_index = 0
    published = None
    promotion_id = None
    helper_paths = {}
    grant_before = None
    credential_sha256 = None

    def run(name, command, timeout=120, allow_failure=False, cwd=base):
        nonlocal command_index
        command_index += 1
        stem = f'{command_index:02d}-{name}'
        outpath, errpath = artifacts / (stem + '.stdout'), artifacts / (stem + '.stderr')
        print('Cross-version acceptance: ' + name, file=sys.stderr, flush=True)
        with outpath.open('xb') as output, errpath.open('xb') as errors:
            proc = subprocess.Popen([str(x) for x in command], cwd=cwd, env=environment,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True)
            try: code = proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as interrupted:
                os.killpg(proc.pid, signal.SIGTERM)
                try: proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL); proc.wait(timeout=10)
                report['commands'].append(dict(name=name, returncode=proc.returncode,
                    timeout=isinstance(interrupted, subprocess.TimeoutExpired), interrupted=True, log=stem))
                raise RuntimeError(name + ' interrupted or timed out') from None
        report['commands'].append(dict(name=name, returncode=code, log=stem))
        require(allow_failure or code == 0, name + ' failed; see ' + stem + '.stderr')
        return code, outpath, errpath

    def host_command(host, script, *values):
        return [host, '--gitgo-internal-role', 'python', '-B', '-I', script, *values]

    def update(name, mode, *values):
        subdir = artifacts / name
        _, output, _ = run(name, host_command(independent_host, update_probe,
            '--run', '--allow-local-adhoc', '--root', app, '--command', public_command,
            '--artifact-dir', subdir, mode, *values), timeout=240)
        value = output_json(output)
        require(value.get('passed') is True, 'Update probe did not pass: ' + name)
        return value

    def audit(name):
        _, output, _ = run(name, ['/usr/bin/sudo', '-n', *host_command(independent_host,
            helper_probe, '--run', '--allow-local-adhoc', 'audit', '--service', service)])
        value = output_json(output)
        require(value.get('passed') is True and value.get('all_clean') is True, 'Helper audit did not pass: ' + name)
        return value

    def file_identity():
        target = project / 'fixture.txt'; info = target.stat()
        return dict(sha256=digest(target), inode=info.st_ino, mtime_ns=info.st_mtime_ns)

    def cell(name, app_version, helper_path):
        _, output, _ = run(name + '-client', host_command(app / 'active/internal/gitgo-host/gitgo-host',
            helper_probe, '--run', '--allow-local-adhoc', 'client', '--service', service,
            '--workspace', project, '--commit', commit, '--expected-version', app_version,
            '--promotion-id', promotion_id), timeout=180)
        client = output_json(output)
        require(client.get('passed') is True, 'Mixed-version client did not pass')
        state = audit(name + '-audit')
        require(state['service']['helper'] == helper_path, 'Helper changed unexpectedly')
        require(state['grant'] == grant_before and digest(registry / 'client.json') == credential_sha256,
                'Version transition changed the existing project authorization or credential')
        require(file_identity() == published, 'A version transition rewrote the approved project file')
        _, output, _ = run(name + '-public-entry', [public_command, '--version'])
        require(output.read_text().strip() == 'Gitgo ' + app_version, 'Original entry opened the wrong version')
        lock = exclusive_runtime_lock(app)
        report['matrix'].append(dict(name=name, app_version=app_version, helper=helper_path,
            client=client, audit=state, file_unchanged=True, runtime_lock=lock))

    def prepare(name, candidate):
        values = ['--manifest', payload / candidate / 'manifest.json', '--signature', payload / candidate / 'manifest.sig',
                  '--archive', payload / candidate / 'package.tar.gz']
        return update(name, 'prepare', *values)

    def ui(name, action, status, version):
        return update(name, 'ui', '--action', action, '--expect-status', status,
                      '--expect-version', version, '--terminal-reader', payload / 'terminal_screen.py')

    try:
        run('sudo-authority', ['/usr/bin/sudo', '-n', '/usr/bin/true'])
        require(not os.path.lexists(registry) and not os.path.lexists(plist), 'Service name collision')
        for key, package in packages.items():
            expected = plan[key]
            require(digest(payload / key / 'package.tar.gz') == expected['archive_sha256'], 'Archive identity mismatch')
            _, output, _ = run(key + '-package-contract', host_command(package / 'internal/gitgo-host/gitgo-host',
                scripts / 'check_macos_managed_dashboard.py', '--run', '--allow-local-adhoc', '--verify-package',
                '--package', package, '--source-commit', expected['commit'], '--source-tree', expected['tree']))
            require(output_json(output)['version'] == expected['version'], 'Package version mismatch')
        _, output, _ = run('install-managed-a', ['/bin/bash', packages['a'] / 'install.sh', '--managed',
            '--no-path-update', '--install-root', app, '--bin-dir', bins], timeout=180)
        report['installation'] = output_json(output)
        require(report['installation'].get('managed') is True, 'Managed installation not created')
        initial = update('inspect-initial', 'inspect')
        report['initial_app'] = initial
        require(initial['bootstrap_api'] == 12 and initial['strategy'] == 'immediate',
                'Combination must use the already-completed quit-update implementation')
        run('fixture-init', ['/usr/bin/git', 'init', '-q'], cwd=project)
        (project / 'fixture.txt').write_bytes(b'sealed fixture\n')
        run('fixture-add', ['/usr/bin/git', 'add', 'fixture.txt'], cwd=project)
        run('fixture-commit', ['/usr/bin/git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'Cross-version fixture'], cwd=project)
        _, output, _ = run('fixture-head', ['/usr/bin/git', 'rev-parse', 'HEAD'], cwd=project)
        commit = output.read_text().strip(); require(re.fullmatch('[0-9a-f]{40}', commit), 'Missing snapshot')
        attempted = True
        _, output, _ = run('helper-install-a', ['/usr/bin/sudo', '-n', admin, 'install', '--name', service,
            '--bundle', packages['a'] / 'internal/gitgo-host', '--project', project, '--uid', str(os.getuid()),
            '--lifetime', '3600', '--allow-local-adhoc'], timeout=180)
        helper_paths['a'] = output_json(output)['result']['helper']
        approval_dir = artifacts / 'dashboard-approval'; approval_dir.mkdir(mode=0o700)
        _, output, _ = run('dashboard-approval-a-a', host_command(independent_host, dashboard_probe,
            '--service', service, '--workspace', project, '--commit', commit, '--command', public_command,
            '--artifact-dir', approval_dir), timeout=300)
        report['approval'] = output_json(output)
        require(report['approval']['passed'], 'Initial user approval failed')
        promotion_id = report['approval']['promotion_id']; require(promotion_id, 'Missing actual promotion task identity')
        published = file_identity()
        grant_before = audit('initial-authorization')['grant']
        credential_sha256 = digest(registry / 'client.json')
        report['checks']['original_credential_sha256'] = credential_sha256
        cell('a-a-initial', versions['a'], helper_paths['a'])
        prepare('prepare-b', 'b')
        # A real admitted Host holds the App lease while another UI confirms.
        # Replacement must fail closed until that exact process has exited.
        ready = base / 'live-app-ready'
        held = subprocess.Popen(host_command(app / 'active/internal/gitgo-host/gitgo-host', '-c',
            'import sys;from pathlib import Path;Path(sys.argv[1]).write_text("ready");sys.stdin.read()', ready),
            cwd=base, env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                require(held.poll() is None and time.monotonic() < deadline, 'Live App lease fixture failed to start')
                time.sleep(.05)
            report['app_busy_refusal'] = ui('app-busy-refusal', 'apply', 'blocked', versions['a'])
            require(file_identity() == published, 'Busy update changed project contents')
        finally:
            if held.stdin: held.stdin.close()
            try: held.wait(timeout=15)
            except subprocess.TimeoutExpired:
                held.terminate()
                try: held.wait(timeout=10)
                except subprocess.TimeoutExpired: held.kill(); held.wait(timeout=5)
        require(held.returncode == 0, 'Lease fixture did not exit normally')
        exclusive_runtime_lock(app)
        report['app_upgrade'] = ui('app-a-to-b', 'apply', 'committed', versions['b'])
        cell('b-a', versions['b'], helper_paths['a'])
        _, output, _ = run('helper-a-to-b', ['/usr/bin/sudo', '-n', admin, 'upgrade', '--name', service,
            '--bundle', packages['b'] / 'internal/gitgo-host', '--allow-local-adhoc'], timeout=180)
        helper_paths['b'] = output_json(output)['result']['helper']
        require(helper_paths['b'] != helper_paths['a'], 'Two builds must have distinct sealed helper identities')
        cell('b-b', versions['b'], helper_paths['b'])
        report['app_rollback'] = ui('app-b-to-a', 'rollback', 'rolled_back', versions['a'])
        cell('a-b', versions['a'], helper_paths['b'])
        # Real launch failure fixture; system binary is copied, never modified.
        broken = base / 'inert-helper'; broken.mkdir()
        shutil.copyfile('/usr/bin/false', broken / 'gitgo-host'); (broken / 'gitgo-host').chmod(0o755)
        run('sign-inert-helper', ['/usr/bin/codesign', '--force', '--sign', '-', broken / 'gitgo-host'])
        prior = audit('before-helper-failure')
        code, _, errors = run('helper-startup-failure', ['/usr/bin/sudo', '-n', admin, 'upgrade', '--name', service,
            '--bundle', broken, '--allow-local-adhoc'], timeout=180, allow_failure=True)
        require(code != 0 and 'prior service restored' in errors.read_text(), 'Helper failure did not report safe restoration')
        restored = audit('after-helper-failure')
        require(restored['service']['helper'] == helper_paths['b'], 'Failed helper replaced prior version')
        require(restored['seen_sha256'] == prior['seen_sha256'], 'Helper rollback replayed a request')
        report['checks']['helper_failed_candidate_restored_prior'] = True
        cell('a-b-after-helper-failure', versions['a'], helper_paths['b'])
        run('helper-b-to-a', ['/usr/bin/sudo', '-n', admin, 'upgrade', '--name', service,
            '--bundle', packages['a'] / 'internal/gitgo-host', '--allow-local-adhoc'], timeout=180)
        cell('a-a-restored', versions['a'], helper_paths['a'])
        before_failure = update('inspect-before-app-failure', 'inspect')
        prepare('prepare-startup-failure', 'failure')
        report['failed_app_upgrade'] = ui('app-startup-failure', 'apply', 'rolled_back', versions['a'])
        failed_state = report['failed_app_upgrade']['after']
        require(failed_state['transaction']['failure_code'] == 'UPDATE_HEALTH_FAILED',
                'Candidate rollback did not reach the expected health failure')
        detail = failed_state.get('health_failure') or {}
        require(detail.get('stage') == 'CHECK_NEW' and detail.get('probe') == 'dashboard-version'
                and detail.get('reason') == 'exit_code' and detail.get('exit_code') == 73,
                'Missing candidate startup-check evidence')
        after_failure = update('inspect-after-app-failure', 'inspect')
        require(before_failure['active_tree_sha256'] == after_failure['active_tree_sha256'],
                'Failed App candidate did not restore exactly the prior program')
        cell('a-a-after-app-failure', versions['a'], helper_paths['a'])
        report['checks']['failed_app_restored_exact_tree'] = True
        # Optional historical helper exercises an actual protocol difference.
        # It is a retained experimental build, not a signed published release.
        if (payload / 'legacy-helper').is_dir():
            require(digest(payload / 'legacy-helper/gitgo-host') == plan['historical_helper']['executable_sha256'],
                    'Historical helper does not match retained evidence')
            _, output, _ = run('install-historical-helper', ['/usr/bin/sudo', '-n', admin, 'upgrade', '--name', service,
                '--bundle', payload / 'legacy-helper', '--allow-local-adhoc'], timeout=180)
            report['historical_helper'] = output_json(output)
            prior = audit('before-unsupported-operation')
            _, output, _ = run('historical-helper-refusal', host_command(app / 'active/internal/gitgo-host/gitgo-host',
                helper_probe, '--run', '--allow-local-adhoc', 'client', '--service', service,
                '--workspace', project, '--commit', commit, '--expected-version', versions['a'],
                '--expect-unsupported-checkout'), timeout=180)
            report['historical_refusal'] = output_json(output)
            require(report['historical_refusal']['passed'], 'Historical helper did not safely reject the unsupported operation')
            after = audit('after-unsupported-operation')
            require(after['seen_sha256'] == prior['seen_sha256'] and file_identity() == published,
                    'Unsupported request started or changed the project')
            run('restore-current-helper', ['/usr/bin/sudo', '-n', admin, 'upgrade', '--name', service,
                '--bundle', packages['a'] / 'internal/gitgo-host', '--allow-local-adhoc'], timeout=180)
            cell('a-a-after-unsupported-helper', versions['a'], helper_paths['a'])
        report['checks']['final_file_identity'] = published
        report['helper_versions'] = helper_paths
        completed = True
    except (Exception, KeyboardInterrupt) as exc:
        import traceback
        failure = type(exc).__name__ + ': ' + str(exc)
        report['traceback'] = traceback.format_exc()[-12000:]
    finally:
        if attempted:
            try:
                final_audit = audit('final-helper-audit'); report['cleanup']['audit'] = final_audit
            except Exception as exc:
                report['cleanup']['audit_error'] = str(exc)
                failure = failure or 'Final cleanup audit failed'
            # A failed audit must not skip the lifecycle API's own safe cleanup.
            # Its assert_idle retains the service if unresolved work still needs it.
            try:
                code, _, _ = run('helper-uninstall', ['/usr/bin/sudo', '-n', admin, 'uninstall', '--name', service],
                                 timeout=180, allow_failure=True)
                gone, _, _ = run('helper-unloaded', ['/usr/bin/sudo', '-n', '/bin/launchctl', 'print', 'system/' + label],
                                 allow_failure=True)
                report['cleanup'].update(uninstall_returncode=code, launchd_unloaded=gone == 113,
                    credential_removed=not os.path.lexists(registry / 'client.json'),
                    socket_removed=not os.path.lexists(registry / 'broker.sock'), launch_plist_removed=not os.path.lexists(plist))
                require(code == 0 and gone == 113 and all(report['cleanup'][key] for key in
                    ('credential_removed', 'socket_removed', 'launch_plist_removed')), 'Helper cleanup incomplete')
                report['cleanup']['verified'] = True
            except Exception as exc:
                report['cleanup']['error'] = str(exc); failure = failure or 'Cleanup failed'
        else: report['cleanup']['helper_not_attempted'] = True
        boot_after = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True).strip()
        report['boot'] = dict(before=boot_before, after=boot_after, unchanged=boot_before == boot_after)
        if failure: report['error'] = failure
        report['passed'] = completed and failure is None and report['cleanup'].get('verified') is True and boot_before == boot_after
        (artifacts / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
