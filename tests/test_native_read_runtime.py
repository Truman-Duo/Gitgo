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
def test_several_line_windows_share_a_single_native_read(tmp_path_factory, workspace_native_launcher, monkeypatch):
    import hashlib

    _, ctx, tools = setup(tmp_path_factory / 'project')
    source = b'first\nsecond\nthird\nfourth\n'
    (Path(ctx.workspace_path) / 'note.txt').write_bytes(source)
    tool = tools['read_file']
    original = ToolPipeline._execute_isolated
    launches = []

    def count(self, *args, **kwargs):
        launches.append(kwargs['native_invocation'].policy)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ToolPipeline, '_execute_isolated', count)
    request = {'path': 'note.txt', 'ranges': [{'offset': 1, 'limit': 1}, {'offset': 3, 'limit': 1}]}
    invalid = execute(ctx, tool, {**request, 'offset': 0})
    assert invalid.diagnostics['code'] == 'INVALID_TOOL_ARGUMENTS'
    assert not launches
    result = execute(ctx, tool, request)
    assert not result.is_error, (result.error, result.data, result.diagnostics)
    assert len(launches) == 1
    assert result.receipt['sandbox_preparation']['phase'] == 'retired'
    assert result.receipt['runtime_cleanup']['complete'] is True
    assert result.data['sha256'] == hashlib.sha256(source).hexdigest()
    assert [(page['offset'], page['content'], page['next_offset']) for page in result.data['pages']] == [
        (1, 'second', 2), (3, 'fourth', None)]


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


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows reader runtime scope')
def test_reader_does_not_expose_unrelated_python_packages(tmp_path_factory, workspace_native_launcher, monkeypatch):
    _, ctx, tools = setup(tmp_path_factory / 'project')
    (Path(ctx.workspace_path) / 'note.txt').write_text('visible', encoding='utf8')
    private = workspace_native_launcher / 'full-packages' / 'unrelated'
    private.mkdir(parents=True)
    secret = private / 'secret.txt'
    secret.write_text('not a reader dependency', encoding='utf8')
    original = ToolPipeline._execute_isolated
    def inspect(self, *args, **kwargs):
        code, out, err = probe(kwargs['native_invocation'].policy, workspace_native_launcher,
            f'open({str(secret)!r}).read()')
        assert code != 0 and 'PermissionError' in err, (code, out, err)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ToolPipeline, '_execute_isolated', inspect)
    result = execute(ctx, tools['read_file'], {'path': 'note.txt'})
    assert not result.is_error and result.data['content'] == 'visible', (result.error, result.data)
    assert result.receipt['sandbox_preparation']['retired']


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows document adapter dependencies')
@pytest.mark.parametrize('extension', ['docx', 'xlsx', 'pdf', 'pptx'])
def test_narrow_reader_preserves_document_adapters(tmp_path_factory, extension):
    _, ctx, tools = setup(tmp_path_factory / 'project')
    target = Path(ctx.workspace_path) / ('sample.' + extension)
    expected = 'native adapter evidence'
    if extension == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph(expected)
        document.save(target)
    elif extension == 'xlsx':
        from openpyxl import Workbook
        book = Workbook()
        book.active['A1'] = expected
        book.save(target)
        book.close()
    elif extension == 'pptx':
        from pptx import Presentation
        from pptx.util import Inches
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1)).text = expected
        deck.save(target)
    else:
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(target))
        canvas.drawString(36, 720, expected)
        canvas.save()
    result = execute(ctx, tools['document_open'], {'path': target.name})
    assert not result.is_error and expected in result.data.get('content', ''), (result.error, result.data, result.diagnostics)
    assert result.receipt['sandbox_preparation']['retired']


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


@pytest.mark.skipif(os.name != 'nt', reason='Windows structured native failure')
@pytest.mark.parametrize('code', ['SANDBOX_CLEANUP_PENDING', 'NATIVE_TEST_EXTENSION_ERROR'])
def test_native_failure_keeps_the_precise_upstream_cleanup_cause(tmp_path_factory, workspace_native_launcher, monkeypatch, code):
    from backend.core.sandbox import SandboxDenied

    root = tmp_path_factory / 'project'
    _, ctx, tools = setup(root)
    (root / 'note.txt').write_text('current evidence', encoding='utf8')
    details = {'phase': 'retirement_pending', 'cause': {'code': 'SANDBOX_ACL_MANAGEMENT_BUSY'}}

    def pending(*_args, **_kwargs):
        raise SandboxDenied(code, 'Earlier access retirement is pending', details=details)

    monkeypatch.setattr(ToolPipeline, '_execute_isolated', pending)
    result = execute(ctx, tools['read_file'], {'path': 'note.txt'})
    assert result.is_error
    assert result.diagnostics['code'] == code
    diagnostic = result.diagnostics.get('error_info', result.diagnostics)
    assert diagnostic['details'] == details
    assert diagnostic['effect_state'] == 'not_committed'
    assert result.receipt['sandbox_preparation']['retired'] is True


@pytest.mark.skipif(os.name != 'nt', reason='Actual concurrent Windows scopes')
@pytest.mark.parametrize('name,args', [
    ('list_files', {}),
    ('search_text', {'pattern': 'visible', 'literal': True}),
    ('search_text', {'pattern': 'visible', 'literal': True, 'include_hidden': True,
                     'respect_ignore': False, 'include': ['**/*.txt']}),
])
def test_directory_reader_does_not_pin_or_expose_excluded_mutable_state(tmp_path_factory, workspace_native_launcher, monkeypatch, name, args):
    import subprocess
    from backend.core.windows_access import AccessTree

    if args.get('include_hidden'):
        from shutil import copyfile
        from backend.core.tools.workspace_search import resolve_ripgrep
        executable = resolve_ripgrep()
        if executable is None:
            pytest.skip('Requires the real ripgrep engine to test hidden-directory pruning')
        target = workspace_native_launcher / 'rg.exe'
        copyfile(executable, target)
        monkeypatch.setenv('GITGO_RIPGREP_PATH', str(target))

    root = tmp_path_factory / 'project'
    _, ctx, tools = setup(root)
    (root / 'visible.txt').write_text('visible business file', encoding='utf8')
    subprocess.run(['git', 'init', '--template=', str(root)], check=True, capture_output=True)
    hidden = root / '.gitgo' / 'context_objects' / 'blobs'
    hidden.mkdir(parents=True, exist_ok=True)
    secret = hidden / 'seed'
    secret.write_text('synthetic internal state', encoding='utf8')
    with AccessTree([hidden], writable=False) as tree:
        before = tree.snapshot()
    original = ToolPipeline._execute_isolated

    def inspect(self, handler, arguments, *rest, **kwargs):
        policy = kwargs['native_invocation'].policy
        with AccessTree([hidden], writable=False) as tree:
            assert tree.snapshot() == before
        (hidden / 'new-object').write_text('concurrent state update', encoding='utf8')
        code, _, error = probe(policy, workspace_native_launcher, f'open({str(secret)!r}).read()')
        assert code != 0 and 'PermissionError' in error
        return original(self, handler, arguments, *rest, **kwargs)

    monkeypatch.setattr(ToolPipeline, '_execute_isolated', inspect)
    result = execute(ctx, tools[name], {'path': '.', **args})
    assert not result.is_error, (result.error, result.data, result.diagnostics)
    assert result.data['count'] >= 1
    if args.get('include_hidden'):
        assert result.data['engine'] == 'ripgrep'
        assert not result.data['warnings']
    assert result.receipt['sandbox_preparation']['retired'] is True


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
