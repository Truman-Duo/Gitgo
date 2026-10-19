"""Minimum Windows acceptance through an actual frozen Host and ToolPipeline.

Reuse the native fixture's temporary workspace/runtime ACLs. No host runner
substitution, transport-only fixture, model, or external API is involved.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap

import pytest

from tests.test_native_sandbox import isolated_python, native_box  # noqa: F401


@pytest.mark.skipif(sys.platform != 'win32' or not os.environ.get('GITGO_SANDBOX_TEST_HOST'),
                    reason='Requires an actual frozen Host on Windows')
@pytest.mark.parametrize('mode', ['normal', 'cancel', 'timeout'])
def test_packaged_pipeline_minimum_boundary(native_box, mode):
    configured = os.environ.get('GITGO_SANDBOX_TEST_HOST')
    if not configured:
        pytest.skip('Requires the actual built Host, not a simulated frozen flag')
    host = Path(configured).resolve(strict=True)
    workspace, _, _ = native_box
    private = workspace.parent / 'host-private.txt'
    private.write_text('synthetic-host-secret', encoding='utf8')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        address = listener.getsockname()
        # Reachability control is outside the AppContainer, never a substitute
        # for the tool that must really execute inside it.
        with socket.create_connection(address, timeout=2):
            peer, _ = listener.accept()
            peer.close()
        child = "import time;from pathlib import Path;Path('child-ready').write_text('ready');time.sleep(120)"
        probe = textwrap.dedent(f'''
            import json, os, socket, subprocess, sys, time
            from pathlib import Path
            from backend.core.child_process import python_command
            Path('allowed.txt').write_text('allowed', encoding='utf8')
            assert Path('allowed.txt').read_text(encoding='utf8') == 'allowed'
            assert 'GITGO_MINIMUM_OPAQUE' not in os.environ
            denied = []
            for access in ('r', 'w'):
                try:
                    with open({str(private)!r}, access) as stream:
                        if access == 'r': stream.read()
                except PermissionError:
                    denied.append(access)
                else:
                    raise AssertionError('Host private file was accessible')
            try:
                connection = socket.create_connection({address!r}, timeout=1)
            except (PermissionError, TimeoutError):
                denied.append('network')
            else:
                connection.close()
                raise AssertionError('Network was accessible')
            with open('child-output.log', 'wb') as sink:
                subprocess.Popen(python_command(['-I', '-c', {child!r}]), stdout=sink, stderr=sink)
            deadline = time.monotonic() + 10
            while not Path('child-ready').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert Path('child-ready').read_text() == 'ready'
            Path('probe-ready').write_text('ready')
            if {mode!r} != 'normal': time.sleep(120)
            print(json.dumps({{'denied': denied, 'pid': os.getpid()}}))
        ''')
        driver = textwrap.dedent(f'''
            import json, threading, time
            from dataclasses import asdict, replace
            from pathlib import Path
            from backend.core.host_profile_lease import HostProfileLease
            from backend.core.loop.agent_tool import ApprovalMode
            from backend.core.loop.execution_context import ExecutionContext
            from backend.core.loop.manager import AgentProcessManager
            from backend.core.loop.models import RingLevel
            from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
            from backend.core.loop.tool_pipeline import ToolPipeline
            from backend.core.loop.tools import ToolRegistry
            from backend.core.storage import get_storage, close_owned_storage
            from backend.core.tools.catalog import build_workspace_tools
            workspace = Path({str(workspace)!r})
            state = Path(__import__('os').environ['GITGO_STATE_HOME'])
            process = AgentProcessManager().fork(parent_id=None, role='worker',
                tool_registry=ToolRegistry(['exec_command']), max_steps=2,
                ring_level=RingLevel.RING_3, workspace_path=str(workspace), task_id='minimum')
            # A stricter Host registration exercises the real existing one-use
            # approval mechanism. It does not change handler or execution type.
            tool = replace(build_workspace_tools(workspace)['exec_command'],
                approval=ApprovalMode.ASK, approval_per_invocation=True)
            args = {{'argv': ['python', '-c', {probe!r}], 'timeout': 4 if {mode!r} == 'timeout' else 30}}
            ctx = ExecutionContext(process=process, session=process.session,
                workspace_path=str(workspace), cancellation=process.cancellation_event,
                storage=get_storage(workspace))
            pipeline = ToolPipeline()
            call = {{'name': tool.name, 'args': args}}
            blocked = pipeline.execute(call, tool, ctx, 'unapproved', 0)
            assert blocked.is_error and not (workspace / 'allowed.txt').exists(), asdict(blocked)
            pending = create_permission_request(process, {{'tool_name': tool.name, 'arguments': args,
                'resource': 'process:workspace', 'purpose': 'Verify minimum native boundary'}},
                {{tool.name: tool}}, str(workspace))
            grant = grant_from_decision(process, pending, 'allow_once')
            assert grant is not None
            process.pending_decision = None
            cancellation_seen = []
            stop = threading.Event()
            def cancel_when_running():
                deadline = time.monotonic() + 25
                while not stop.is_set() and time.monotonic() < deadline:
                    if (workspace / 'probe-ready').exists():
                        cancellation_seen.append(True)
                        process.cancellation_event.set()
                        return
                    stop.wait(.01)
            monitor = threading.Thread(target=cancel_when_running, daemon=True) if {mode!r} == 'cancel' else None
            try:
                with HostProfileLease(state):
                    if monitor: monitor.start()
                    result = pipeline.execute(call, tool, ctx, 'approved', 0)
                assert grant['remaining_uses'] == 0, grant
                receipt = result.receipt
                assert receipt['launch_state'] == 'started', asdict(result)
                assert len(receipt['execution_basis']) == 64, receipt
                assert receipt['evidence_state'] == 'committed', receipt
                assert receipt['runtime_cleanup'] == {{'complete': True, 'mode': 'windows-job', 'issues': []}}, receipt
                assert result.is_error == ({mode!r} != 'normal'), asdict(result)
                journal = json.loads(Path(receipt['invocation_path']).read_text(encoding='utf8'))
                assert journal['process_id'] == process.process_id, journal
                if {mode!r} == 'normal':
                    assert result.data['success'] and result.data['exit_code'] == 0, asdict(result)
                    observed = json.loads(result.data['stdout'])
                    assert observed['denied'] == ['r', 'w', 'network'], observed
                    retry = pipeline.execute(call, tool, ctx, 'spent-grant', 0)
                    assert retry.is_error and 'launch_state' not in retry.receipt, asdict(retry)
                elif {mode!r} == 'cancel':
                    assert cancellation_seen and 'cancel' in result.error.lower(), asdict(result)
                else:
                    assert 'timeout' in result.error.lower() or 'timed out' in result.error.lower(), asdict(result)
                print(json.dumps({{'result': asdict(result), 'journal': journal,
                    'remaining_uses': grant['remaining_uses'], 'mode': {mode!r}}}, default=str))
            finally:
                stop.set()
                if monitor: monitor.join(timeout=2)
                close_owned_storage()
        ''')
        result = subprocess.run([str(host), '--gitgo-internal-role', 'python', '-c', driver],
            cwd=host.parent, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=75,
            env=dict(os.environ, GITGO_MINIMUM_OPAQUE='synthetic-credential'))
        assert result.returncode == 0, result.stdout + '\n' + result.stderr
        report = json.loads(result.stdout)
        assert report['mode'] == mode and report['remaining_uses'] == 0
        assert (workspace / 'allowed.txt').read_text() == 'allowed'
        assert (workspace / 'child-ready').read_text() == 'ready'
        assert private.read_text(encoding='utf8') == 'synthetic-host-secret'
        listener.settimeout(.1)
        with pytest.raises(socket.timeout):
            listener.accept()
