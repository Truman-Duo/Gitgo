"""Full Pipeline reads from fresh ordinary directories, without native_box setup."""
import os
import subprocess
from pathlib import Path

import pytest

from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.tools import ToolRegistry
from backend.core.tools.catalog import build_workspace_tools
from tests.test_native_sandbox import isolated_python  # noqa: F401
from tests.native_source_runtime import workspace_native_launcher  # noqa: F401
from tests.test_native_sandbox import finished


def setup(root):
    root.mkdir(exist_ok=True)
    tools = build_workspace_tools(root)
    manager = AgentProcessManager()
    process = manager.fork(parent_id=None, role='supervisor', actor_kind='supervisor',
        ring_level=RingLevel.RING_0, capability_profile_id='supervisor.control',
        tool_registry=ToolRegistry(list(tools)), max_steps=8, workspace_path=str(root),
        task_id='read-task', task_kind='answer')
    ctx = ExecutionContext(process, process.session, str(root), cancellation=process.cancellation_event)
    return process, ctx, tools


def execute(ctx, tool, args):
    return ToolPipeline().execute({'name': tool.name, 'args': args}, tool, ctx, 'read-invocation', 0)


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows read-view provisioning')
@pytest.mark.parametrize('handler,args', [('read_file', {'path': 'note.txt'}),
    ('document_open', {'path': 'note.txt'}), ('list_files', {'path': '.'}),
    ('search_text', {'path': '.', 'pattern': 'native-read', 'literal': True})])
def test_fresh_non_git_workspace_can_read_without_manual_profile(tmp_path_factory, workspace_native_launcher, handler, args):
    process, ctx, tools = setup(tmp_path_factory / 'fresh-project')
    (Path(ctx.workspace_path) / 'note.txt').write_text('native-read', encoding='utf-8')
    result = execute(ctx, tools[handler], args)
    assert not result.is_error, (result.error, result.data, result.diagnostics)
    assert result.receipt['sandbox_preparation']['phase'] == 'retired'
    assert result.receipt['runtime_cleanup']['complete'] is True
    if handler in {'read_file', 'document_open'}:
        assert result.data['content'] == 'native-read'
    else:
        assert result.data['count'] == 1


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows approved external read')
def test_exact_approval_enables_real_external_read_and_not_its_sibling(tmp_path_factory, workspace_native_launcher):
    process, ctx, tools = setup(tmp_path_factory / 'project')
    external = tmp_path_factory / 'external'
    external.mkdir()
    target, sibling = external / 'approved.txt', external / 'private.txt'
    target.write_text('approved content', encoding='utf-8')
    sibling.write_text('unapproved content', encoding='utf-8')
    tool = tools['read_file']
    args = {'path': str(target)}
    assert execute(ctx, tool, args).diagnostics['code'] == 'RESOURCE_SCOPE_APPROVAL_REQUIRED'
    create_permission_request(process, {'tool_name': tool.name, 'arguments': args,
        'resource': str(target), 'purpose': 'Read the selected test file'}, {tool.name: tool}, ctx.workspace_path)
    assert grant_from_decision(process, process.pending_decision, 'allow_once')
    process.pending_decision = None
    result = execute(ctx, tool, args)
    assert not result.is_error, (result.error, result.data, result.diagnostics)
    assert result.data['content'] == 'approved content'
    assert execute(ctx, tool, {'path': str(sibling)}).diagnostics['code'] == 'RESOURCE_SCOPE_APPROVAL_REQUIRED'
    assert execute(ctx, tool, args).diagnostics['code'] == 'RESOURCE_SCOPE_APPROVAL_REQUIRED'


def probe(policy, runtime, source):
    from backend.core.sandbox import sandbox_popen, sandbox_environment
    return finished(sandbox_popen([str(runtime / 'python.exe'), '-I', '-X', 'utf8', '-c', source],
        policy, cwd=str(runtime), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf8', env=sandbox_environment(os.environ)))


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows native isolation')
def test_read_scope_denies_sibling_bytes_and_target_write_in_the_os(tmp_path_factory, workspace_native_launcher, monkeypatch):
    _, ctx, tools = setup(tmp_path_factory / 'project')
    root = Path(ctx.workspace_path)
    (root / 'approved.txt').write_text('read-only', encoding='utf8')
    (root / 'private.txt').write_text('secret', encoding='utf8')
    original = ToolPipeline._execute_isolated
    def inspect(self, *args, **kwargs):
        policy = kwargs['native_invocation'].policy
        code, out, err = probe(policy, workspace_native_launcher,
            f"from pathlib import Path;print(Path({str(root / 'approved.txt')!r}).read_text())")
        assert code == 0 and 'read-only' in out, (code, out, err)
        for source in (f"open({str(root / 'private.txt')!r}).read()",
                       f"open({str(root / 'approved.txt')!r},'w').write('escaped')"):
            code, out, err = probe(policy, workspace_native_launcher, source)
            assert code != 0 and 'PermissionError' in err, (code, out, err)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ToolPipeline, '_execute_isolated', inspect)
    result = execute(ctx, tools['read_file'], {'path': 'approved.txt'})
    assert not result.is_error, (result.error, result.data)
    assert (root / 'approved.txt').read_text() == 'read-only'


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows external directory read')
@pytest.mark.parametrize('name,args', [('list_files', {}), ('search_text', {'pattern': 'external-content', 'literal': True})])
def test_approved_external_directory_uses_the_same_native_path(tmp_path_factory, workspace_native_launcher, name, args):
    process, ctx, tools = setup(tmp_path_factory / 'project')
    external = tmp_path_factory / 'external'; external.mkdir()
    (external / 'note.txt').write_text('external-content', encoding='utf8')
    tool = tools[name]; args = {**args, 'path': str(external)}
    create_permission_request(process, {'tool_name': name, 'arguments': args,
        'resource': str(external), 'purpose': 'Inspect the selected directory'}, {name: tool}, ctx.workspace_path)
    assert grant_from_decision(process, process.pending_decision, 'allow_once')
    process.pending_decision = None
    result = execute(ctx, tool, args)
    assert not result.is_error and result.data['count'] == 1, (result.error, result.data, result.diagnostics)


@pytest.mark.skipif(os.name != 'nt', reason='Actual concurrent Windows scopes')
def test_concurrent_reads_do_not_borrow_each_others_resource_scope(tmp_path_factory, workspace_native_launcher, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    root = tmp_path_factory / 'shared-project'
    contexts = [setup(root) for _ in range(2)]
    for name in ('one.txt', 'two.txt'):
        (root / name).write_text(name, encoding='utf8')
    barrier = Barrier(2, timeout=20)
    original = ToolPipeline._execute_isolated
    def inspect(self, handler, args, *rest, **kwargs):
        policy = kwargs['native_invocation'].policy
        barrier.wait()
        other = root / ('two.txt' if args['path'] == 'one.txt' else 'one.txt')
        code, out, err = probe(policy, workspace_native_launcher, f'open({str(other)!r}).read()')
        assert code != 0 and 'PermissionError' in err, (code, out, err)
        return original(self, handler, args, *rest, **kwargs)
    monkeypatch.setattr(ToolPipeline, '_execute_isolated', inspect)
    def run(pair):
        i, (_, ctx, tools) = pair
        return execute(ctx, tools['read_file'], {'path': 'one.txt' if i == 0 else 'two.txt'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, enumerate(contexts)))
    assert all(not r.is_error for r in results), [(r.error, r.data) for r in results]
    assert all(r.receipt['sandbox_preparation']['phase'] == 'retired' for r in results)


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows read-scope retirement')
@pytest.mark.parametrize('change', ['cancel', 'revoke'])
def test_authority_change_after_preparation_never_launches_the_reader(tmp_path_factory, workspace_native_launcher, monkeypatch, change):
    process, ctx, tools = setup(tmp_path_factory / 'project')
    target = tmp_path_factory / 'external.txt'; target.write_text('retained secret', encoding='utf8')
    tool = tools['read_file']; args = {'path': str(target)}
    create_permission_request(process, {'tool_name': tool.name, 'arguments': args,
        'resource': str(target), 'purpose': 'Synthetic revocation test'}, {tool.name: tool}, ctx.workspace_path)
    assert grant_from_decision(process, process.pending_decision, 'allow_task')
    process.pending_decision = None
    original = ToolPipeline._execute_isolated
    def changed(self, *items, **kwargs):
        if change == 'cancel':
            ctx.cancel()
        else:
            from backend.core.loop.read_approval import revoke_read_requests
            revoke_read_requests(process)
            process.approval_grants.clear()
        return original(self, *items, **kwargs)
    monkeypatch.setattr(ToolPipeline, '_execute_isolated', changed)
    result = execute(ctx, tool, args)
    assert result.is_error and 'content' not in (result.data or {})
    assert result.receipt['launch_state'] == 'not_started'
    assert result.receipt['sandbox_preparation']['retired']


@pytest.mark.skipif(os.name != 'nt' or not os.environ.get('GITGO_SANDBOX_TEST_HOST'),
                    reason='Requires the actual Windows frozen deliverable')
def test_packaged_pipeline_prepares_fresh_workspace_and_consumes_exact_read_approval(tmp_path_factory):
    """The Pipeline AND reader both execute the unmodified frozen backend."""
    import json
    from backend.core.child_process import INTERNAL_ROLE_FLAG, PYTHON_ROLE
    root = tmp_path_factory / 'fresh'; root.mkdir()
    (root / 'note.txt').write_text('ordinary-frozen', encoding='utf8')
    target = tmp_path_factory / 'approved.txt'; target.write_text('external-frozen', encoding='utf8')
    sibling = tmp_path_factory / 'private.txt'; sibling.write_text('not-approved', encoding='utf8')
    driver = f'''
import json
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.execution_context import ExecutionContext
from backend.core.loop.tool_pipeline import ToolPipeline
from backend.core.loop.permission_broker import create_permission_request,grant_from_decision
from backend.core.storage import close_owned_storage
from backend.core.tools.catalog import build_workspace_tools
root={str(root)!r}
tools=build_workspace_tools(root)
p=AgentProcessManager().fork(parent_id=None,role='supervisor',actor_kind='supervisor',
    ring_level=RingLevel.RING_0,tool_registry=ToolRegistry(list(tools)),workspace_path=root,
    task_id='frozen-read',task_kind='answer',capability_profile_id='supervisor.control',max_steps=8)
ctx=ExecutionContext(p,p.session,root,cancellation=p.cancellation_event)
tool=tools['read_file']
def run(path):
    return ToolPipeline().execute({{'name':'read_file','args':{{'path':path}}}},tool,ctx,'frozen-read',0)
try:
    first=run('note.txt');assert not first.is_error,(first.error,first.data)
    assert first.data['content']=='ordinary-frozen'
    target={str(target)!r};args={{'path':target}}
    assert run(target).diagnostics['code']=='RESOURCE_SCOPE_APPROVAL_REQUIRED'
    create_permission_request(p,{{'tool_name':'read_file','arguments':args,'resource':target,
        'purpose':'Synthetic frozen read acceptance'}},{{'read_file':tool}},root)
    assert grant_from_decision(p,p.pending_decision,'allow_once')
    p.pending_decision=None
    selected=run(target);assert not selected.is_error,(selected.error,selected.data)
    assert selected.data['content']=='external-frozen'
    assert run({str(sibling)!r}).diagnostics['code']=='RESOURCE_SCOPE_APPROVAL_REQUIRED'
    assert run(target).diagnostics['code']=='RESOURCE_SCOPE_APPROVAL_REQUIRED'
    assert first.receipt['sandbox_preparation']['retired'] and selected.receipt['sandbox_preparation']['retired']
    print(json.dumps({{'frozen_pipeline':True,'ordinary_read':True,'approved_read':True,'sibling_denied':True,'limit_consumed':True}}))
finally:
    close_owned_storage()
'''
    env = {**os.environ, 'GITGO_STATE_HOME': str(tmp_path_factory / 'state'),
           'GITGO_CONFIG_PATH': str(tmp_path_factory / 'config.json')}
    result = subprocess.run([os.environ['GITGO_SANDBOX_TEST_HOST'], INTERNAL_ROLE_FLAG, PYTHON_ROLE,
                             '-I', '-c', driver], env=env, capture_output=True, text=True,
                            encoding='utf8', errors='replace', timeout=100)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)['frozen_pipeline']
