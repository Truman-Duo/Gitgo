"""Same canonical read admission on an actual delegated Linux backend."""
import pytest
from tests.test_native_sandbox import linux_box  # noqa: F401
from tests.test_native_read_runtime import setup, execute
from backend.core.loop.permission_broker import create_permission_request, grant_from_decision


@pytest.mark.parametrize('handler,args', [('read_file', {'path': 'note.txt'}),
                                        ('list_files', {'path': '.'})])
def test_fresh_ordinary_read_view(linux_box, tmp_path_factory, handler, args):
    _, ctx, tools = setup(tmp_path_factory / 'fresh-project')
    from pathlib import Path
    (Path(ctx.workspace_path) / 'note.txt').write_text('linux-native-read', encoding='utf8')
    result = execute(ctx, tools[handler], args)
    assert not result.is_error, (result.error, result.data, result.diagnostics)
    assert result.receipt['runtime_cleanup']['complete']
    assert result.receipt['sandbox_preparation']['retired']
    if handler == 'read_file':
        assert result.data['content'] == 'linux-native-read'


def test_exact_external_approval_reads_the_pinned_file(linux_box, tmp_path_factory):
    process, ctx, tools = setup(tmp_path_factory / 'project')
    target = tmp_path_factory / 'external.txt'; target.write_text('approved-linux', encoding='utf8')
    tool = tools['read_file']; args = {'path': str(target)}
    create_permission_request(process, {'tool_name': tool.name, 'arguments': args,
        'resource': str(target), 'purpose': 'Read synthetic Linux evidence'}, {tool.name: tool}, ctx.workspace_path)
    assert grant_from_decision(process, process.pending_decision, 'allow_once')
    process.pending_decision = None
    result = execute(ctx, tool, args)
    assert not result.is_error and result.data['content'] == 'approved-linux', (result.error, result.data)
    assert execute(ctx, tool, args).diagnostics['code'] == 'RESOURCE_SCOPE_APPROVAL_REQUIRED'
