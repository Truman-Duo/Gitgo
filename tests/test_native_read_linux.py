"""Same canonical read admission on an actual delegated Linux backend."""
import pytest
import sys
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


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux mount namespace required')
def test_leaf_mount_extension_preserves_ancestor_mount_boundary(tmp_path_factory):
    """Real bind mounts: allow only the selected file, never a mounted ancestor."""
    import subprocess
    import sys
    from pathlib import Path
    root = tmp_path_factory / 'mount-root'
    (root / 'nested').mkdir(parents=True)
    (root / 'mounted-parent').mkdir()
    leaf = root / 'nested' / 'selected'; leaf.touch()
    source = tmp_path_factory / 'source'; source.write_text('selected-content')
    directory = tmp_path_factory / 'directory'; directory.mkdir()
    (directory / 'other').write_text('ancestor-content')
    code = f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from backend.core.sandbox_read_native import NativeReadRoot
from backend.core.sandbox_read import ReadBoundaryError
with NativeReadRoot({str(root)!r}) as root:
    try:
        root.open('nested/selected')
    except OSError:
        pass
    else:
        raise AssertionError('ordinary read crossed a mount')
    with root.open('nested/selected', allow_leaf_mount=True) as opened:
        assert opened.read_bytes() == b'selected-content'
    try:
        root.open('mounted-parent/other', allow_leaf_mount=True)
    except OSError:
        pass
    else:
        raise AssertionError('leaf exception crossed an ancestor mount')
print('leaf-mount-boundary-verified')
"""
    result = subprocess.run(['bwrap', '--unshare-user', '--unshare-pid', '--unshare-net',
        '--die-with-parent', '--ro-bind', '/', '/', '--proc', '/proc',
        '--ro-bind', str(source), str(leaf), '--ro-bind', str(directory), str(root / 'mounted-parent'),
        sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'leaf-mount-boundary-verified'
