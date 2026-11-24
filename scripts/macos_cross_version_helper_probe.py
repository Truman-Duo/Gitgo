"""Explicit disposable-fixture probes for independently versioned App/helpers.

Run using the frozen Host's python internal role. The outer harness verifies
the selected package, installs services and controls upgrades. ``audit`` only
reads root evidence. ``client`` uses the ordinary user's real broker protocol
and journals; an optional promotion retry must leave publication effects intact.
No service management, credential renewal, launch replay or production enablement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def runtime():
    from backend.version import __version__
    return dict(version=__version__, frozen=bool(getattr(sys, "frozen", False)),
                executable=sys.executable, uid=os.getuid(), euid=os.geteuid())


def audit(service):
    from backend.core import macos_helper_lifecycle as life, macos_identity as identity
    from backend.core.macos_helper_install import trusted_directory
    from backend.core.sandbox_macos import lease_root, load_state

    require(os.getuid() == os.geteuid() == 0, "Audit requires administrator authority")
    root = life.service_root(service)
    trusted_directory(root)
    state = life.configuration(root)
    before = identity.bounded_read(root / "private/service.json", 0, True)
    ledger_path = root / "private/seen.json"
    raw = identity.bounded_read(ledger_path, 0, True)
    seen = json.loads(raw)
    require(type(seen) is list and len(seen) <= 4096
            and all(type(value) is str and re.fullmatch("[0-9a-f]{32}", value) for value in seen)
            and len(set(seen)) == len(seen), "Invalid invocation replay ledger")
    grant = json.loads(identity.bounded_read(root / "private/grant.json", 0, True))
    # Never serialize the credential token or its stored hash.
    public_grant = {key: grant[key] for key in (
        "owner_uid", "project", "identity", "project_object", "grant_id", "boot",
        "expires", "revoked", "resources",
    ) if key in grant}
    rows = []
    paths = sorted((root / "private").glob("invocation-*.json"))
    require(len(paths) <= 4096, "Invocation evidence exceeds audit bounds")
    for path in paths:
        binding_raw = identity.bounded_read(path, 0, True)
        binding = json.loads(binding_raw)
        identifier = binding.get("invocation_id")
        require(path.name == "invocation-" + str(identifier) + ".json"
                and identifier in seen, "Invocation evidence is not in the replay ledger")
        directory = lease_root(binding)
        lease = load_state(directory)
        require(lease["uid"] == binding["uid"], "Lease UID differs from invocation evidence")
        owned = identity.owned_rows(lease["uid"])
        cleanup = lease.get("cleanup") or {}
        clean = (lease["phase"] == "clean" and not owned
                 and (not lease["launch_count"] or (
                     lease.get("invocation_id") == identifier
                     and cleanup.get("observed_empty") is True and not cleanup.get("issues"))))
        rows.append(dict(invocation_id=identifier, binding_sha256=digest(binding_raw),
                         lease=str(directory), uid=lease["uid"], phase=lease["phase"],
                         launch_count=lease["launch_count"], boot_id=lease.get("boot_id"),
                         cleanup=cleanup, owned_rows=owned, clean=clean))
    idle_error = None
    try:
        life.assert_idle(root)
    except Exception as exc:
        idle_error = type(exc).__name__ + ": " + str(exc)
    after = identity.bounded_read(root / "private/service.json", 0, True)
    require(before == after and raw == identity.bounded_read(ledger_path, 0, True),
            "Service changed during audit; obtain a new observation")
    missing = sorted(set(seen) - {row["invocation_id"] for row in rows})
    return dict(passed=True, mode="audit", runtime=runtime(), service=state,
                service_sha256=digest(before), grant=public_grant,
                seen_count=len(seen), seen_sha256=digest(raw),
                invocation_count=len(rows), invocations=rows,
                missing_invocation_records=missing,
                idle_verified=idle_error is None, idle_error=idle_error,
                all_clean=not missing and idle_error is None and all(row["clean"] for row in rows),
                production_enabled=False)


def refs(store, namespace, prefix=""):
    found, after = [], ""
    while True:
        rows = store.list_state_refs(namespace, prefix=prefix, after=after, limit=100)
        found.extend(rows)
        require(len(found) <= 8192, "Fixture state exceeds inspection bounds")
        if len(rows) < 100:
            return found
        after = rows[-1]["key"]


def file_facts(project, paths):
    from backend.core.sandbox_read import ReadRoot
    result = {}
    require(type(paths) is list and len(paths) <= 128, "Invalid publication paths")
    with ReadRoot(project) as root:
        for name in paths:
            require(type(name) is str and name and not Path(name).is_absolute()
                    and all(part not in ("", ".", "..", ".git", ".gitgo") for part in name.split("/")),
                    "Unsafe publication evidence path")
            path = project / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                result[name] = dict(exists=False)
                continue
            require(stat.S_ISREG(info.st_mode), "Fixture publication output must be a regular file")
            with root.open(name) as opened:
                raw = opened.read_bytes(max_bytes=16 * 1024 * 1024)
            result[name] = dict(exists=True, device=info.st_dev, inode=info.st_ino,
                                mtime_ns=info.st_mtime_ns, mode=stat.S_IMODE(info.st_mode),
                                size=info.st_size, sha256=digest(raw))
    return result


def repeat_publication(store, project, promotion_id):
    from backend.core.loop.worktree import AgentWorktreeManager
    manager = AgentWorktreeManager(project, store)
    rows = refs(store, "agent_promotion")
    heads = [row for row in rows if re.fullmatch("[0-9a-f]{32}", row["key"])]
    require(len(heads) == 1 and heads[0]["key"] == manager._ref_id(promotion_id),
            "Expected the unique fixture publication for this exact promotion ID")
    head = heads[0]
    raw = store.read_blob(head["value_ref"], repair=False)
    previous = json.loads(raw)
    require(previous.get("state") == "committed" and previous.get("native_linear") is True,
            "Only a committed native publication may be inspected for repetition")
    commits = previous.get("applied_commits")
    require(type(commits) is list and 1 <= len(commits) <= 8, "Expected a bounded published fixture")
    before = file_facts(project, previous["paths"])
    result = manager.promote(snapshot_commit=previous["snapshot_commit"],
                             ordered_commits=commits, promotion_id=promotion_id)
    require(result.get("promoted") and result.get("applied_commits") == []
            and result.get("already_promoted") == commits and result.get("changed_files") == [],
            "Committed publication was not recognized without replay")
    require(refs(store, "agent_promotion") == rows
            and store.read_blob(head["value_ref"], repair=False) == raw,
            "Publication ledger changed during repetition")
    after = file_facts(project, previous["paths"])
    require(before == after, "Publication bytes or file identity changed during repetition")
    return dict(passed=True, promotion_key=head["key"], head_ref=head["value_ref"],
                ledger_sha256=digest(raw), files=after, already_promoted=commits,
                applied_commits=[], publication_unchanged=True)


def client(args):
    from backend.core import macos_broker_client as broker
    from backend.core.sandbox_read import ReadRoot
    from backend.core.storage import get_storage, close_owned_storage
    from backend.core.storage.native_invocations import NAMESPACE, pending_native_invocations
    from backend.core.macos_git_checkout import validate_commit

    facts = runtime()
    require(501 <= os.getuid() < 200000000 and os.geteuid() == os.getuid(),
            "Client must run as the actual ordinary project user")
    require(facts["version"] == args.expected_version, "Active App version differs from the expected version")
    project = Path(args.workspace).absolute()
    validate_commit(args.commit)
    store = get_storage(project)
    try:
        require(not pending_native_invocations(store, include_active=True), "Existing native recovery debt")
        before = {row["key"]: row for row in refs(store, NAMESPACE, "history/")}
        with broker.resident_project_session(args.service, project):
            with ReadRoot(project / ".git/objects") as root:
                if args.expect_incompatible_helper:
                    from backend.core.macos_broker_capabilities import BrokerCompatibilityError
                    try:
                        broker.query_git_objects(project, root, ["cat-file", "-t", args.commit])
                    except BrokerCompatibilityError as exc:
                        require(exc.code == "HELPER_UPGRADE_REQUIRED", "Unexpected compatibility refusal code")
                        outcome = dict(incompatible_helper_rejected=True, error_code=exc.code, error=str(exc))
                    else:
                        raise RuntimeError("New App unexpectedly accepted the incompatible helper")
                elif args.expect_unsupported_checkout:
                    from backend.core.loop.git_execution import GitBoundaryError
                    try:
                        broker.checkout_git_commit(project, root, args.commit)
                    except GitBoundaryError as exc:
                        require("Unsupported broker operation or fields" in str(exc),
                                "Old helper failed for a different reason: " + str(exc))
                        outcome = dict(unsupported_checkout_rejected=True, error=str(exc))
                    else:
                        raise RuntimeError("Historical helper unexpectedly accepted checkout")
                else:
                    result = broker.query_git_objects(project, root, ["cat-file", "-t", args.commit])
                    require(result.returncode == 0 and result.stdout.strip() == "commit", "Object query did not verify a commit")
                    outcome = dict(commit=args.commit, object_type=result.stdout.strip())
            if args.promotion_id:
                require(not args.expect_unsupported_checkout and not args.expect_incompatible_helper,
                        "Do not retry publication with an unsupported helper")
                outcome["repeat_publication"] = repeat_publication(store, project, args.promotion_id)
        require(not pending_native_invocations(store, include_active=True), "Probe left native recovery debt")
        histories = []
        after_rows = refs(store, NAMESPACE, "history/")
        after = {row["key"]: row for row in after_rows}
        require(all(after.get(key) == row for key, row in before.items()),
                "An existing native invocation history changed")
        for row in after_rows:
            if row["key"] in before:
                continue
            entry = json.loads(store.read_blob(row["value_ref"], repair=False))
            cleanup = entry.get("runtime_cleanup")
            require(entry["phase"] == "finished", "Native invocation has not finished")
            if args.expect_unsupported_checkout or args.expect_incompatible_helper:
                require(entry["launch_state"] == "not_started", "Unsupported operation was not refused before launch")
            else:
                require(entry["launch_state"] == "started" and cleanup
                        and cleanup.get("complete") is True and not cleanup.get("issues"),
                        "Native invocation cleanup was not confirmed")
            histories.append({key: entry.get(key) for key in (
                "invocation_id", "tool_name", "execution_basis", "phase", "launch_state",
                "runtime_cleanup", "exit_code", "macos_broker_binding",
            )})
        require(histories, "Probe did not create a formal native invocation record")
        if not args.promotion_id:
            require(len(histories) == 1, "Expected one exact probe invocation")
        return dict(passed=True, mode="client", runtime=facts, service=args.service,
                    workspace=str(project), outcome=outcome, native_invocations=histories,
                    existing_native_history_unchanged=True,
                    no_pending_native_invocations=True, production_enabled=False)
    finally:
        close_owned_storage()


def exec_context(args):
    """Real registered ToolPipeline for one inert command in the VM project."""
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.tools.catalog import build_workspace_tools
    from backend.core.storage import get_storage

    facts = runtime()
    require(501 <= os.getuid() < 200000000 and os.getuid() == os.geteuid(),
            "Execution fixture requires an ordinary user")
    require(facts['version'] == args.expected_version, "Unexpected execution client version")
    require(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.model'], text=True).strip().startswith('VirtualMac')
            and subprocess.check_output(['/usr/bin/csrutil', 'status'], text=True).strip()
            == 'System Integrity Protection status: enabled.', "Fault injection requires a SIP-enabled VirtualMac")
    project = Path(args.workspace).resolve(strict=True)
    store = get_storage(project)
    process = AgentProcess('ack-loss-' + uuid.uuid4().hex, 'worker', RingLevel.RING_3,
        active_task_id='ack-loss-fixture', worktree_path=str(project), session=AgentSession(),
        tool_registry=ToolRegistry(['exec_command']))
    ctx = ExecutionContext(process, process.session, str(project), storage=store)
    tools = build_workspace_tools(project)
    ctx.artifacts['tool_catalog'] = tools
    tool = tools['exec_command']
    pipeline = ToolPipeline()

    def call(index):
        return pipeline.execute({'name': 'exec_command', 'args': {
            'argv': ['/usr/bin/printf', 'gitgo-ack-loss-fixture'], 'timeout': 5,
        }}, tool, ctx, 'ack-loss-' + str(index))

    return facts, project, store, process, call


def exec_ack_loss(args):
    """Drop only an actual started frame; all sends and other receives are real."""
    from dataclasses import asdict
    from backend.core import macos_broker_client as broker, macos_broker_protocol as wire
    from backend.core.storage import close_owned_storage
    from backend.core.storage.native_invocations import NAMESPACE, pending_native_invocations

    facts, project, store, process, call = exec_context(args)
    sent, lost = [], []
    receive, send = wire.receive, wire.send
    before_file = file_facts(project, ['fixture.txt'])
    before_history = refs(store, NAMESPACE, 'history/')

    def tracking_send(connection, message):
        if message.get('op') == 'run':
            sent.append({'op': 'run', 'invocation_id': message['invocation_id']})
        return send(connection, message)

    def lose_started(connection):
        response = receive(connection)
        if response.get('event') == 'started' and not lost:
            require(type(response.get('pid')) is int and response['pid'] > 1,
                    'Actual started acknowledgement has no process identity')
            lost.append({key: response[key] for key in ('event', 'invocation_id', 'pid')})
            raise EOFError('Acceptance fixture: actual started acknowledgement lost')
        return response

    try:
        require(not pending_native_invocations(store, include_active=True), 'Preexisting execution debt')
        with broker.resident_project_session(args.service, project):
            wire.receive, wire.send = lose_started, tracking_send
            try:
                first = call(0)
                pending = pending_native_invocations(store, include_active=True)
                require(len(lost) == len(sent) == len(pending) == 1
                        and pending[0]['invocation_id'] == lost[0]['invocation_id'] == sent[0]['invocation_id'],
                        'Expected one actual launch, one lost acknowledgement and its retained record')
                identifier = pending[0]['invocation_id']
                require(first.is_error and pending[0]['launch_state'] == 'unknown'
                        and pending[0]['effect_state'] == 'ambiguous'
                        and (first.receipt.get('runtime_cleanup') or {}).get('complete') is False,
                        'Lost started acknowledgement was misclassified or settled')
                original_ref = store.get_state_ref(NAMESPACE, 'pending/' + identifier)
                original = json.loads(store.read_blob(original_ref, repair=False))
                require(original['launch_state'] == 'unknown' and original['exit_code'] is None
                        and store.get_state_ref(NAMESPACE, 'history/' + identifier) is None,
                        'Unknown remote launch must not be retired as finished history')
                # This uses the same public tool again. Its normal recovery
                # preflight may learn cleanup; it must never send another run.
                repeated = call(1)
                after_pending = pending_native_invocations(store, include_active=True)
                require(repeated.is_error and len(sent) == 1 and len(lost) == 1
                        and len(after_pending) == 1 and after_pending[0]['invocation_id'] == identifier,
                        'A retry replayed or retired the uncertain operation')
                require(refs(store, NAMESPACE, 'history/') == before_history
                        and file_facts(project, ['fixture.txt']) == before_file,
                        'The fault or retry changed existing effects or history')
                return dict(passed=True, mode='exec-ack-loss', runtime=facts, service=args.service,
                    invocation_id=identifier, original_ref=original_ref, original_record=original,
                    lost_started=lost[0], functional_requests=sent, result=asdict(first), retry=asdict(repeated),
                    before_recovery=pending, after_retry=after_pending, expected_outcome_debt=True,
                    project_unchanged=True, existing_history_unchanged=True, production_enabled=False)
            finally:
                wire.receive, wire.send = receive, send
    finally:
        close_owned_storage()


def exec_recover(args):
    """Normal authenticated cleanup reconciliation; unknown outcome stays pending."""
    from dataclasses import asdict
    from backend.core import macos_broker_client as broker, macos_broker_protocol as wire
    from backend.core.storage import close_owned_storage
    from backend.core.storage.native_invocations import (
        NAMESPACE, pending_native_invocations, reconcile_native_invocations,
    )

    require(re.fullmatch('[0-9a-f]{32}', args.invocation_id)
            and re.fullmatch('sha256:[0-9a-f]{64}', args.original_ref), 'Invalid retained invocation identity')
    facts, project, store, process, call = exec_context(args)
    identifier, original_ref = args.invocation_id, args.original_ref
    send = wire.send
    operations = []
    before_file = file_facts(project, ['fixture.txt'])
    before_history = refs(store, NAMESPACE, 'history/')

    def observe_send(connection, message):
        operations.append(str(message.get('op')))
        return send(connection, message)

    try:
        original_bytes = store.read_blob(original_ref, repair=False)
        original = json.loads(original_bytes)
        require(original['invocation_id'] == identifier and original['launch_state'] == 'unknown'
                and original.get('recovered_outcome') is None, 'Original uncertainty evidence differs')
        before = pending_native_invocations(store, include_active=True)
        require(len(before) == 1 and before[0]['invocation_id'] == identifier, 'Expected the exact retained debt')
        with broker.resident_project_session(args.service, project):
            wire.send = observe_send
            try:
                deadline = time.monotonic() + 25
                changed = []
                while True:
                    changed.extend(reconcile_native_invocations(store))
                    pending = pending_native_invocations(store, include_active=True)
                    require(len(pending) == 1 and pending[0]['invocation_id'] == identifier,
                            'Recovery retired or replaced the uncertain invocation')
                    if pending[0]['code'] == 'NATIVE_OUTCOME_UNCONFIRMED':
                        break
                    require(time.monotonic() < deadline, 'Authenticated helper cleanup did not become available')
                    time.sleep(.2)
                head = store.get_state_ref(NAMESPACE, 'pending/' + identifier)
                item = json.loads(store.read_blob(head, repair=False))
                require(item.get('recovery_previous_ref') == original_ref and item.get('recovered_outcome') == 'unknown'
                        and item.get('runtime_cleanup', {}).get('complete') is True
                        and not item['runtime_cleanup'].get('issues')
                        and store.get_state_ref(NAMESPACE, 'recovery/' + identifier) == head,
                        'Recovery receipt did not preserve the original unknown result')
                require(reconcile_native_invocations(store) == []
                        and store.get_state_ref(NAMESPACE, 'pending/' + identifier) == head,
                        'Repeated recovery changed an already reconciled record')
                outcomes = [call(2), call(3)]
                require(all(result.is_error and result.diagnostics.get('code') == 'NATIVE_OUTCOME_UNCONFIRMED'
                            for result in outcomes), 'Recovered unknown effects were allowed to replay')
                require('run' not in operations and set(operations) <= {'status'},
                        'Recovery sent a functional request instead of a status query')
                require(store.read_blob(original_ref, repair=False) == original_bytes
                        and store.get_state_ref(NAMESPACE, 'pending/' + identifier) == head
                        and store.get_state_ref(NAMESPACE, 'history/' + identifier) is None
                        and refs(store, NAMESPACE, 'history/') == before_history,
                        'Recovery lost old evidence, invented history or retired pending debt')
                require(file_facts(project, ['fixture.txt']) == before_file, 'Recovery rewrote a project file')
                return dict(passed=True, mode='exec-recover', runtime=facts, service=args.service,
                    invocation_id=identifier, original_ref=original_ref, recovery_ref=head,
                    before=before, pending=pending_native_invocations(store, include_active=True),
                    receipt=item['recovery_receipt'], changed=changed, operations=operations,
                    retries=[asdict(value) for value in outcomes], expected_outcome_debt=True,
                    original_retained=True, history_not_invented=True, cleanup_confirmed=True,
                    no_replay=True, project_unchanged=True, production_enabled=False)
            finally:
                wire.send = send
    finally:
        close_owned_storage()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--allow-local-adhoc", action="store_true")
    commands = parser.add_subparsers(dest="mode", required=True)
    admin = commands.add_parser("audit")
    admin.add_argument("--service", required=True)
    user = commands.add_parser("client")
    user.add_argument("--service", required=True)
    user.add_argument("--workspace", required=True)
    user.add_argument("--commit", required=True)
    user.add_argument("--expected-version", required=True)
    user.add_argument("--promotion-id")
    refusal = user.add_mutually_exclusive_group()
    refusal.add_argument("--expect-unsupported-checkout", action="store_true")
    refusal.add_argument("--expect-incompatible-helper", action="store_true")
    for name in ('exec-ack-loss', 'exec-recover'):
        execution = commands.add_parser(name)
        execution.add_argument('--service', required=True)
        execution.add_argument('--workspace', required=True)
        execution.add_argument('--expected-version', required=True)
        if name == 'exec-recover':
            execution.add_argument('--invocation-id', required=True)
            execution.add_argument('--original-ref', required=True)
    args = parser.parse_args()
    require(args.run and args.allow_local_adhoc, "Explicit local acceptance flags are required")
    require(sys.platform == "darwin" and getattr(sys, "frozen", False), "Use the actual frozen macOS Host")
    actions = {'audit': lambda value: audit(value.service), 'client': client,
               'exec-ack-loss': exec_ack_loss, 'exec-recover': exec_recover}
    result = actions[args.mode](args)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
