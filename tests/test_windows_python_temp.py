"""Private mkdir compatibility; native checks keep the containment boundary."""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from backend.core import windows_python_temp as adapter
from tests.test_native_sandbox import isolated_python, native_box, finished  # noqa: F401


def test_child_local_adapter_preserves_other_modes_and_installs_once(monkeypatch):
    original_calls, private_calls = [], []
    original = lambda *a, **kw: original_calls.append((a, kw))
    fake_os = SimpleNamespace(name="nt", mkdir=original, fspath=os.fspath)
    monkeypatch.setattr(adapter, "os", fake_os)
    monkeypatch.setattr(adapter, "_PrivateDirectoryApi", lambda: SimpleNamespace(
        container_sid=lambda: "S-1-15-2-123", mkdir=lambda *args: private_calls.append(args)))
    assert adapter.install_private_mkdir()
    installed = fake_os.mkdir
    assert not adapter.install_private_mkdir() and fake_os.mkdir is installed
    installed(Path("private"), 0o700)
    installed("ordinary")
    installed("unsupported-dir-fd", 0o700, dir_fd=7)
    assert private_calls == [("private", "S-1-15-2-123")]
    assert original_calls == [(("ordinary", 0o777), {"dir_fd": None}),
                              (("unsupported-dir-fd", 0o700), {"dir_fd": 7})]
    with pytest.raises(TypeError):
        installed("bad-mode", "700")


@pytest.mark.parametrize("platform,sid", [("posix", "S-1-15-2-123"), ("nt", None)])
def test_other_platforms_and_uncontained_processes_are_unchanged(monkeypatch, platform, sid):
    original = lambda *a, **kw: None
    fake_os = SimpleNamespace(name=platform, mkdir=original)
    monkeypatch.setattr(adapter, "os", fake_os)
    monkeypatch.setattr(adapter, "_PrivateDirectoryApi", lambda: SimpleNamespace(container_sid=lambda: sid))
    assert not adapter.install_private_mkdir()
    assert fake_os.mkdir is original


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows AppContainer")
def test_real_private_temp_creation_cleanup_and_outside_denial(tmp_path_factory, isolated_python, monkeypatch):
    from backend.core import child_process
    from tests.native_source_runtime import populate_source_runtime
    from backend.core.sandbox import sandbox_popen, sandbox_environment, prepare_child_environment
    from backend.core.sandbox_access import WorkspaceAccess
    from backend.core.process_control import terminate_tree

    workspace = tmp_path_factory / "workspace"
    temp = workspace / ".gitgo/sandbox/home/Temp"
    temp.mkdir(parents=True)
    runtime = populate_source_runtime(isolated_python)
    secret = tmp_path_factory / "not-allowed"
    secret.mkdir()
    access = WorkspaceAccess(workspace, [runtime], tmp_path_factory / "access.json",
        private_temp_roots=[temp])
    with monkeypatch.context() as patch:
        patch.setattr(child_process, "__file__", str(runtime / "backend/core/child_process.py"))
        patch.setattr(child_process.sys, "executable", str(runtime / "python.exe"))
        command = child_process.workspace_python_command(["python", "-c", f'''
import os,tempfile
from pathlib import Path
from backend.core.sandbox import prepare_child_environment
prepare_child_environment(str(Path.cwd()))
with tempfile.TemporaryDirectory() as directory:
    target=Path(directory)/'test.txt'
    target.write_text('private content',encoding='utf8')
    assert target.read_text(encoding='utf8')=='private content'
assert not Path(directory).exists()
private=Path(os.environ['TEMP'])/'bytes-private'
os.mkdir(os.fsencode(private),0o700)
(private/'ok').write_text('yes')
try:
    os.mkdir(private,0o700)
except FileExistsError:
    pass
else:
    raise AssertionError('duplicate creation must fail')
try:
    os.mkdir({str(secret / 'escape')!r},0o700)
except PermissionError:
    pass
else:
    raise AssertionError('outside directory creation must remain denied')
try:
    Path({str(secret / 'private.txt')!r}).write_text('escape')
except PermissionError:
    pass
else:
    raise AssertionError('outside file write must remain denied')
print('private-temp-and-containment-ok')
'''], workspace)
    try:
        access.prepare()
        # Business directories keep Modify; only the private temp tree has FA.
        import struct
        from backend.core.windows_acl_plan import _entries, _profile_sid
        from backend.core.windows_access import AccessTree
        sid = _profile_sid(access.payload['principal'])
        with AccessTree([workspace], writable=False) as tree:
            records = {Path(item['path']): item for item in tree.snapshot()}
            def mask(path):
                return [struct.unpack_from('<I', ace, 4)[0]
                    for ace in _entries(tree.record_descriptor(records[path]).dacl) if ace[8:] == sid]
            assert mask(workspace) == [0x1301BF]
            assert mask(temp) == [0x1F01FF]
        with (workspace / "out.log").open("wb") as output, (workspace / "err.log").open("wb") as errors:
            proc = sandbox_popen(command, access.policy, cwd=str(workspace), stdin=subprocess.PIPE,
                stdout=output, stderr=errors, env=sandbox_environment(os.environ))
            try:
                proc.stdin.close()
                code = proc.wait(timeout=30)
            finally:
                terminate_tree(proc, getattr(proc, "_gitgo_job_handle", None))
                proc._gitgo_job_handle = None
        out = (workspace / "out.log").read_text("utf8")
        err = (workspace / "err.log").read_text("utf8", errors="replace")
        if code != 0 or "private-temp-and-containment-ok" not in out:
            pytest.fail(out + err)
        assert not (secret / "escape").exists() and not (secret / "private.txt").exists()
    finally:
        access.close()
    assert access.payload["phase"] == "revoked"

