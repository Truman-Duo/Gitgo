"""Packaged acceptance must fail rather than silently use source modules."""
from pathlib import Path
from types import SimpleNamespace
import plistlib
import sys

import pytest

if sys.platform=='win32':pytest.skip('POSIX acceptance harness',allow_module_level=True)
from scripts import macos_git_packaged_support as packaged


@pytest.fixture
def runtime(monkeypatch,tmp_path_factory):
    root=tmp_path_factory/'bundle';root.mkdir()
    monkeypatch.setattr(packaged.sys,'frozen',True,raising=False)
    monkeypatch.setattr(packaged.sys,'_MEIPASS',str(root),raising=False)
    def module(name):
        return SimpleNamespace(__file__=str(root/Path(*name.split('.')).with_suffix('.py')))
    monkeypatch.setattr(packaged,'importlib',SimpleNamespace(import_module=module))
    return root,module


def test_packaged_modules_are_recorded_under_bundle(runtime):
    root,_=runtime
    result=packaged.evidence()
    assert result['embedded_backend'] is True
    assert all(Path(path).is_relative_to(root) for path in result['modules'].values())


@pytest.mark.parametrize('name',packaged.MODULES)
def test_one_source_module_invalidates_frozen_evidence(runtime,monkeypatch,tmp_path_factory,name):
    _,module=runtime
    monkeypatch.setattr(packaged.importlib,'import_module',lambda qualified:
        SimpleNamespace(__file__=str(tmp_path_factory/'source.py')) if qualified=='backend.core.'+name else module(qualified))
    with pytest.raises(RuntimeError,match='source backend'):
        packaged.evidence()


def test_source_python_never_claims_packaged_verification(runtime,monkeypatch):
    monkeypatch.setattr(packaged.sys,'frozen',False)
    result=packaged.evidence()
    assert result['frozen'] is False and result['embedded_backend'] is False


@pytest.mark.parametrize('suffix',[
    ['--gitgo-internal-role','python','service.py'],
    ['--gitgo-internal-role','macos-execution-service','/different/lease'],
    ['--gitgo-internal-role','macos-execution-service','/lease','unexpected'],
])
def test_supervisor_evidence_rejects_wrong_entry(monkeypatch,suffix):
    from backend.core import macos_identity
    monkeypatch.setattr(macos_identity,'bounded_read',lambda *args:
        plistlib.dumps({'ProgramArguments':['/installed/gitgo-host',*suffix]}))
    assert not packaged.task_evidence('/lease',{},Path('/installed/gitgo-host'))


def test_supervisor_evidence_accepts_only_exact_frozen_role(monkeypatch):
    from backend.core import macos_identity
    monkeypatch.setattr(macos_identity,'bounded_read',lambda *args:plistlib.dumps({'ProgramArguments':
        ['/installed/gitgo-host','--gitgo-internal-role','macos-execution-service','/lease']}))
    assert packaged.task_evidence('/lease',{},Path('/installed/gitgo-host'))


def test_frozen_child_probe_keeps_internal_python_role(runtime,monkeypatch):
    monkeypatch.setattr(packaged,'HELPER',None)
    assert packaged.command('/probe.py','--mode','published')==[
        sys.executable,'--gitgo-internal-role','python','-B','-I','/probe.py','--mode','published']


@pytest.mark.parametrize('platform',['linux','win32'])
def test_acceptance_rejects_non_macos_before_installing(monkeypatch,tmp_path_factory,platform):
    from scripts import check_macos_packaged_git as harness
    from backend.core import macos_helper_install as installer
    monkeypatch.setattr(harness,'sys',SimpleNamespace(platform=platform))
    monkeypatch.setattr(harness,'os',SimpleNamespace(getuid=lambda:0,geteuid=lambda:0,environ={'SUDO_UID':'501'}))
    monkeypatch.setattr(sys,'argv',['check','--run','--staged','--bundle',str(tmp_path_factory)])
    monkeypatch.setattr(installer,'install',lambda *a,**kw:pytest.fail('Non-Mac operator reached installation'))
    with pytest.raises(SystemExit) as exc:harness.main()
    assert exc.value.code==2


@pytest.mark.parametrize('failed_stage',[None,'worktree_lifecycle','normal','sealing','runtime_merge'])
def test_result_acceptance_splits_batches_and_keeps_failure_closed(monkeypatch,tmp_path_factory,capsys,failed_stage):
    import json
    from scripts import check_macos_packaged_git as harness
    from backend.core import macos_helper_install as installer
    helper=tmp_path_factory/'gitgo-host';helper.write_bytes(b'test helper')
    version=tmp_path_factory/'version'
    monkeypatch.setattr(installer,'install',lambda *a,**kw:helper)
    monkeypatch.setattr(installer,'verify_bundle_path',lambda *a:version)
    # Model the Mac-only operator boundary locally; never alter the test
    # runner's platform or UID while checking pure batch orchestration.
    monkeypatch.setattr(harness,'os',SimpleNamespace(getuid=lambda:0,geteuid=lambda:0,environ=harness.os.environ))
    monkeypatch.setattr(harness,'sys',SimpleNamespace(platform='darwin',stderr=sys.stderr,executable=sys.executable))
    monkeypatch.setenv('SUDO_UID','501')
    monkeypatch.setattr(sys,'argv',['check','--run','--staged','--bundle',str(tmp_path_factory),
        '--consumers','--objects','--checkout','--worktrees','--results','--compare','--seal','--runtime-merge'])
    calls=[]
    stages=('worktree_lifecycle','normal','sealing','runtime_merge','faults')
    def run(command,**kw):
        key=stages[len(calls)];calls.append(command)
        report=dict(passed=key!=failed_stage,frozen_package_verified=True,
            **{flag:('--'+flag.replace('_','-') in command) for flag in ('consumers','objects','checkout','worktrees','results','compare','seal','runtime_merge')})
        return SimpleNamespace(returncode=0,stderr='',stdout=json.dumps(report))
    monkeypatch.setattr(harness.subprocess,'run',run)
    if failed_stage:
        with pytest.raises(SystemExit) as exc:harness.main()
        assert exc.value.code==1
    else:harness.main()
    result=json.loads(capsys.readouterr().out)
    assert result['passed']==(failed_stage is None) and result['split_worktree_batch']
    assert len(calls)==(stages.index(failed_stage)+1 if failed_stage else 5)
    first=calls[0]
    assert '--worktrees' in first and '--checkout' in first and '--objects' in first
    assert '--results' not in first and '--index' not in first and '--compare' in first
    if len(calls)>1:assert '--results' in calls[1] and '--worktrees' in calls[1] and '--compare' not in calls[1]
    if len(calls)>2:assert '--seal' in calls[2] and '--results' in calls[2] and '--compare' not in calls[2]
    if len(calls)>3:assert '--runtime-merge' in calls[3] and '--seal' in calls[3]
    if len(calls)>4:
        assert '--objects' in calls[4] and '--checkout' not in calls[4] and '--results' not in calls[4]


@pytest.mark.parametrize('focused',['','finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review'])
@pytest.mark.parametrize('failed_stage',[None,'finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read'])
def test_finalization_acceptance_splits_crash_boundaries(monkeypatch,tmp_path_factory,capsys,failed_stage,focused):
    import json
    from scripts import check_macos_packaged_git as harness
    from backend.core import macos_helper_install as installer
    helper=tmp_path_factory/'gitgo-host';helper.write_bytes(b'test helper');version=tmp_path_factory/'version'
    monkeypatch.setattr(installer,'install',lambda *a,**kw:helper)
    monkeypatch.setattr(installer,'verify_bundle_path',lambda *a:version)
    monkeypatch.setattr(harness,'os',SimpleNamespace(getuid=lambda:0,geteuid=lambda:0,environ=harness.os.environ))
    monkeypatch.setattr(harness,'sys',SimpleNamespace(platform='darwin',stderr=sys.stderr,executable=sys.executable))
    monkeypatch.setenv('SUDO_UID','501')
    monkeypatch.setattr(sys,'argv',['check','--run','--staged','--bundle',str(tmp_path_factory),
        '--consumers','--objects','--checkout','--worktrees','--results','--compare','--seal','--runtime-merge','--finalize'])
    calls=[];stages=('worktree_lifecycle','normal','finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read','faults')
    if focused:
        sys.argv.extend(['--only-stage',focused])
        stages=(focused,)
        if failed_stage not in stages:failed_stage=None
    def run(command,**kw):
        key=stages[len(calls)];calls.append(command)
        value=dict(passed=key!=failed_stage,frozen_package_verified=True,
            **{f:('--'+f.replace('_','-') in command) for f in ('consumers','objects','checkout','worktrees','results','compare','seal','runtime_merge')})
        value['finalize']=command[command.index('--finalize')+1] if '--finalize' in command else ''
        return SimpleNamespace(returncode=0,stderr='',stdout=json.dumps(value))
    monkeypatch.setattr(harness.subprocess,'run',run)
    if failed_stage:
        with pytest.raises(SystemExit):harness.main()
    else:harness.main()
    result=json.loads(capsys.readouterr().out)
    assert result['passed']==(failed_stage is None)
    assert len(calls)==(stages.index(failed_stage)+1 if failed_stage else len(stages))
    assert result['selected_stages']==list(stages) and result['focused_acceptance']==bool(focused)
    if focused:
        assert calls[0][calls[0].index('--finalize')+1]==focused.removeprefix('finalize_')
        assert result['normal'] is None and result['faults'] is None
        return
    for i,group in ((2,'pending'),(3,'ack'),(4,'boundaries'),(5,'upstream'),(6,'resume'),(7,'promotion_read')):
        if len(calls)>i:
            assert '--runtime-merge' in calls[i] and '--seal' in calls[i]
            assert calls[i][calls[i].index('--finalize')+1]==group


def test_upstream_acceptance_fixture_has_real_parent_and_nonempty_tree(tmp_path_factory):
    """Use the admin harness producer, not a separately invented linear fixture."""
    import os,subprocess
    from scripts.check_macos_git_broker import object_fixture_commit
    root=tmp_path_factory/'repository';root.mkdir()
    env=dict(os.environ,GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null')
    def git(*args,data=None,extra=None):
        return subprocess.run(['git',*args],cwd=root,env={**env,**(extra or {})},input=data,capture_output=True,check=True)
    git('init','-q')
    tree=git('mktree',data=b'').stdout.decode().strip()
    parent=git('-c','user.name=Test','-c','user.email=test@local.invalid','commit-tree',tree,'-m','base').stdout.decode().strip()
    blob=git('hash-object','-w','--stdin',data=b'gitgo protected object\0\xff\r\n').stdout.decode().strip()
    tree=git('mktree',data=f'100644 blob {blob}\tdata.bin\n'.encode()).stdout.decode().strip()
    class Runner:
        def run(self,args,timeout,author_identity):
            identity=author_identity
            extra={f'GIT_{role}_{field.upper()}':identity[field]
                for role in ('AUTHOR','COMMITTER') for field in ('name','email','date')}
            result=git(*args,extra=extra)
            return SimpleNamespace(returncode=result.returncode,stdout=result.stdout.decode())
    result,tip=object_fixture_commit(Runner(),tree,parent)
    assert result.stdout==tip+'\n' and tip!=parent
    assert git('merge-base','--is-ancestor',parent,tip).returncode==0
    assert git('rev-list','--parents','-n','1',tip).stdout.decode().strip()==tip+' '+parent
    assert git('show',tip+':data.bin').stdout==b'gitgo protected object\0\xff\r\n'
