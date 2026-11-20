"""The admin batch reports failure and stops before starting another suite."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='macOS operator batch')

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from scripts import check_macos_packaged_git as batch
from backend.core import macos_helper_install as install


@pytest.fixture
def prepared(tmp_path_factory,monkeypatch):
    helper=tmp_path_factory/'gitgo-host';helper.write_bytes(b'fixture only')
    monkeypatch.setattr(batch,'os',SimpleNamespace(getuid=lambda:0,geteuid=lambda:0,environ={'SUDO_UID':'501'}))
    monkeypatch.setattr(batch.sys,'argv',['check','--run','--staged','--bundle',str(tmp_path_factory),
        '--allow-local-adhoc','--consumers','--with-lifecycle'])
    monkeypatch.setattr(install,'install',lambda *a,**k:helper)
    monkeypatch.setattr(install,'verify_bundle_path',lambda _:tmp_path_factory)
    return []


def test_all_suites_receive_expected_runtime_and_flags(prepared,monkeypatch,capsys):
    def run(command,**kwargs):
        prepared.append(command)
        lifecycle=Path(command[3]).name=='check_macos_helper_lifecycle.py'
        assert '--staged' in command and '--run' in command
        if lifecycle:assert '--bundle' in command and '--allow-local-adhoc' in command and '--helper' not in command
        else:assert '--helper' in command and '--consumers' in command
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(passed=True,frozen_package_verified=True,consumers=True)),stderr='')
    monkeypatch.setattr(batch.subprocess,'run',run)
    batch.main()
    report=json.loads(capsys.readouterr().out)
    assert len(prepared)==3 and report['passed'] and report['bundle_unchanged'] and report['lifecycle']['passed']


@pytest.mark.parametrize('fault',['nonzero','failed','source_runtime','missing_consumer','malformed'])
def test_failed_suite_retains_diagnostics_and_stops(prepared,monkeypatch,capsys,fault):
    def run(command,**kwargs):
        prepared.append(command)
        report=dict(passed=fault!='failed',frozen_package_verified=fault!='source_runtime',consumers=fault!='missing_consumer')
        return SimpleNamespace(returncode=1 if fault=='nonzero' else 0,
            stdout='not json' if fault=='malformed' else json.dumps(report),stderr='retained diagnostic')
    monkeypatch.setattr(batch.subprocess,'run',run)
    with pytest.raises(SystemExit):batch.main()
    report=json.loads(capsys.readouterr().out)
    assert len(prepared)==1 and not report['passed'] and report['normal_stderr']=='retained diagnostic'
    assert report['faults'] is None and report['error']


def test_object_batch_requires_writer_reports(prepared,monkeypatch,capsys):
    monkeypatch.setattr(batch.sys,'argv',[*batch.sys.argv,'--objects'])
    def run(command,**kwargs):
        prepared.append(command)
        assert '--objects' in command
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(passed=True,frozen_package_verified=True,consumers=True)),stderr='')
    monkeypatch.setattr(batch.subprocess,'run',run)
    with pytest.raises(SystemExit):batch.main()
    assert len(prepared)==1 and not json.loads(capsys.readouterr().out)['passed']


@pytest.mark.parametrize('missing',[None,'normal','faults'])
def test_checkout_batch_distinguishes_normal_writes_and_checkout_faults(prepared,monkeypatch,capsys,missing):
    monkeypatch.setattr(batch.sys,'argv',[a for a in batch.sys.argv if a!='--with-lifecycle']+['--objects','--checkout'])
    def run(command,**kwargs):
        prepared.append(command)
        key='faults' if Path(command[3]).name=='check_macos_git_broker_faults.py' else 'normal'
        assert '--checkout' in command
        assert ('--objects' in command)==(key=='normal')
        assert ('--consumers' in command)==(key=='normal')
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(passed=True,frozen_package_verified=True,
            checkout=missing!=key,objects=key=='normal',consumers=key=='normal')),stderr='')
    monkeypatch.setattr(batch.subprocess,'run',run)
    if missing:
        with pytest.raises(SystemExit):batch.main()
    else:batch.main()
    report=json.loads(capsys.readouterr().out)
    assert report['passed']==(missing is None)
    assert len(prepared)==(1 if missing=='normal' else 2)
