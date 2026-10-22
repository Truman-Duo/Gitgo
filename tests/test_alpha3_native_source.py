"""Real AppContainer + unmodified ToolPipeline in a disposable source runtime.

This is source-runtime evidence, not a frozen package or dashboard acceptance.
Only owned temporary directories receive test ACLs through native_box.
"""
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest
from tests.test_native_sandbox import isolated_python, native_box, owned_windows_access


@pytest.fixture(scope="module")
def source_runtime(isolated_python):
    from tests.native_source_runtime import populate_source_runtime
    return populate_source_runtime(isolated_python)


def test_read_then_checked_write_with_external_private_state(source_runtime,native_box):
    workspace,_,_=native_box
    (workspace/"input.txt").write_text("source data",encoding="utf8")
    private=workspace.parent/"outside-secret.txt"
    private.write_text("not for native children",encoding="utf8")
    driver=textwrap.dedent(f"""
        import json
        from pathlib import Path
        from dataclasses import asdict
        from backend.core.loop.execution_context import ExecutionContext
        from backend.core.loop.manager import AgentProcessManager
        from backend.core.loop.models import RingLevel
        from backend.core.loop.tool_pipeline import ToolPipeline
        from backend.core.loop.tools import ToolRegistry
        from backend.core.storage import get_storage,close_owned_storage
        from backend.core.tools.catalog import build_workspace_tools
        from backend.core.knowledge.models import Lesson
        from backend.core.knowledge.manager import LessonManager
        workspace=Path({str(workspace)!r})
        p=AgentProcessManager().fork(parent_id=None,role='worker',
            ring_level=RingLevel.RING_3,tool_registry=ToolRegistry(['read_file','write_file']),
            workspace_path=str(workspace),task_id='source-integration',max_steps=10)
        p.context_snapshot={{'project_name':'test-project'}}
        LessonManager.save(workspace,Lesson(id='read-advice',project_name='test-project',
            trigger='input.txt',rule='The input is a sample fixture, preserve its source identity.'))
        ctx=ExecutionContext(p,p.session,str(workspace),storage=get_storage(workspace))
        tools=build_workspace_tools(workspace)
        from backend.core.knowledge.prewrite import prepare_read_inventory
        inventory=prepare_read_inventory(workspace,{{'_lesson_context':{{'project_name':'test-project',
            'state_home':str(ctx.storage.paths.state_home)}}}})
        assert inventory['state']=='completed',inventory
        pipeline=ToolPipeline()
        try:
            read=pipeline.execute({{'name':'read_file','args':{{'path':'input.txt','bind_dependency':True}}}},
                tools['read_file'],ctx,'native-read')
            assert not read.is_error,asdict(read)
            assert read.data['content']=='source data',asdict(read)
            assert read.data['lesson_recall']['state']=='completed',asdict(read)
            assert read.data['lesson_advisories'][0]['lesson_id']=='read-advice',asdict(read)
            assert 'warnings' not in read.data,asdict(read)
            assert read.receipt['launch_state']=='started',asdict(read)
            assert read.receipt['runtime_cleanup']['complete'] is True,asdict(read)
            assert read.data['input_dependency']['path']=='input.txt',asdict(read)
            write=pipeline.execute({{'name':'write_file','args':{{'path':'output.txt','content':'checked output'}}}},
                tools['write_file'],ctx,'broker-write')
            assert not write.is_error,asdict(write)
            assert write.data['lesson_feedback']['mandatory_check_state']=='not_required',asdict(write)
            assert write.receipt['effect_state']=='committed',asdict(write)
            print(json.dumps({{'read':asdict(read),'write':asdict(write)}}))
        finally:
            close_owned_storage()
    """)
    environment=dict(os.environ,GITGO_STATE_HOME=str(workspace.parent/'private-state'),
        GITGO_CONFIG_PATH=str(workspace.parent/'config.json'),
        GITGO_LLM_CONFIG_PATH=str(workspace.parent/'llm.json'))
    result=subprocess.run([str(source_runtime/'python.exe'),'-B','-I','-X','utf8','-c',driver],
        cwd=source_runtime,env=environment,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=45)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    report=json.loads(result.stdout)
    assert (workspace/'output.txt').read_text()=='checked output'
    assert private.read_text()=='not for native children'
    assert 'not for native children' not in report['read']['formatted']


@pytest.fixture
def source_native_runner(source_runtime, monkeypatch):
    """Install the same source handler in an owned runtime; keep OS execution.

    Only the trusted executable/source location changes in the test launcher;
    admission, native policy, handler, results and cleanup remain production.
    """
    from backend.core.child_process import tool_runner_command
    command=tool_runner_command(source_runtime)
    command[0]=str(source_runtime/"python.exe")
    monkeypatch.setattr("backend.core.loop.process_tool_runner.tool_runner_command",lambda:command)
    monkeypatch.setattr("backend.core.loop.process_tool_runner.owned_child_cwd",lambda _:source_runtime)
    return lambda workspace: owned_windows_access(workspace,[source_runtime])


def test_scoped_preparation_runs_real_native_children_with_disjoint_workspaces(source_runtime, tmp_path_factory):
    first=tmp_path_factory/"first"; second=tmp_path_factory/"second"; private=tmp_path_factory/"private"
    for path in (first,second,private): path.mkdir()
    (first/"input.txt").write_text("first-data")
    (second/"input.txt").write_text("second-data")
    (private/"secret.txt").write_text("SYNTHETIC-PRIVATE")
    driver=textwrap.dedent(f"""
        from pathlib import Path
        import json,subprocess,sys
        from backend.core.sandbox import sandbox_popen,sandbox_environment,SandboxPolicy
        from backend.core.sandbox_access import WorkspaceAccess
        from backend.core.process_control import terminate_tree
        import os
        first,second,private=map(Path,{[str(first),str(second),str(private)]!r})
        runtime=Path({str(source_runtime)!r})
        one=WorkspaceAccess(first,[runtime],private/'first-access.json')
        two=WorkspaceAccess(second,[runtime],private/'second-access.json')
        def probe(workspace,code):
            proc=sandbox_popen([sys.executable,'-I','-X','utf8','-c',code],SandboxPolicy(workspace),
                cwd=str(workspace),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding='utf8',env=sandbox_environment(os.environ))
            try:
                out,err=proc.communicate(timeout=15)
                result={{'code':proc.returncode,'stdout':out,'stderr':err}}
                assert proc.close_sandbox()['complete']
                return result
            finally:
                terminate_tree(proc,getattr(proc,'_gitgo_job_handle',None))
                for stream in (proc.stdin,proc.stdout,proc.stderr):
                    if stream:stream.close()
        try:
            one.prepare();two.prepare()
            inside=probe(first,"from pathlib import Path;Path('output.txt').write_text('ok');print(Path('input.txt').read_text())")
            assert inside['code']==0 and inside['stdout'].strip()=='first-data',inside
            denied=[]
            for target in (second/'input.txt',private/'secret.txt',private/'first-access.json'):
                result=probe(first,"from pathlib import Path;print(Path("+repr(str(target))+").read_text())")
                assert result['code']!=0 and 'PermissionError' in result['stderr'],result
                assert 'second-data' not in result['stdout'] and 'SYNTHETIC-PRIVATE' not in result['stdout']
                denied.append(result['code'])
            writing_runtime=probe(first,"from pathlib import Path;Path("+repr(str(runtime/'forbidden.txt'))+").write_text('escaped')")
            assert writing_runtime['code']!=0 and 'PermissionError' in writing_runtime['stderr'],writing_runtime
            one.close()
            peer=probe(second,"from pathlib import Path;print(Path('input.txt').read_text())")
            assert peer['code']==0 and peer['stdout'].strip()=='second-data',peer
            print(json.dumps({{'inside':inside['code'],'denied':denied,'runtime_write_denied':True,'peer_survived_cleanup':True}}))
        finally:
            one.close();two.close()
    """)
    result=subprocess.run([str(source_runtime/'python.exe'),'-B','-I','-X','utf8','-c',driver],
        cwd=source_runtime,capture_output=True,text=True,encoding='utf8',timeout=90)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    assert json.loads(result.stdout)['peer_survived_cleanup'] is True
    assert (first/'output.txt').read_text()=='ok'
    assert not (source_runtime/'forbidden.txt').exists()
    for path in private.glob('*-access.json'):
        assert json.loads(path.read_text())['phase']=='revoked'
