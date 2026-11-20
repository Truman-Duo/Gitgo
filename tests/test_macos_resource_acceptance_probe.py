"""Startup failures must remain bounded and leave evidence before task admission."""
import sys
import pytest

if sys.platform == 'win32':
    pytest.skip('POSIX identity and file operations', allow_module_level=True)

import json
import os
import subprocess
from scripts import check_macos_resource_fixes as check
from backend.core import macos_identity as identity

BOOT='1712b6c7-f85c-42cc-9780-851da992c246'


@pytest.fixture
def probe(monkeypatch):
    children=[];options=[];codes=[];real=subprocess.Popen
    def launch(argv,**kwargs):
        options.append(kwargs)
        child=real([sys.executable,'-I','-c',codes[0]],**kwargs)
        children.append(child);return child
    monkeypatch.setattr(check.subprocess,'Popen',launch)
    monkeypatch.setattr(identity,'boot_id',lambda:BOOT)
    monkeypatch.setattr(check,'sample_probe',lambda child:{'sampled_live_child':child.poll() is None})
    yield children,options,codes
    for child in children:
        if child.poll() is None:child.kill();child.wait(timeout=3)


def test_probe_uses_clean_current_uid_environment(probe,monkeypatch):
    children,options,codes=probe
    monkeypatch.setenv('API_KEY','must-not-inherit')
    monkeypatch.setenv('__CF_USER_TEXT_ENCODING','0xDEADBEEF:0:0')
    codes.append('import json;print(json.dumps([4,9,'+repr(BOOT)+']))')
    report={};check.probe_helper('/sealed/gitgo-host',report)
    env=options[0]['env']
    assert env['__CF_USER_TEXT_ENCODING']==f'0x{os.getuid():X}:0:0' and 'API_KEY' not in env
    assert options[0]['stdin']==subprocess.DEVNULL
    assert report['passed'] and report['reaped'] and children[0].poll()==0


def test_slow_start_is_sampled_and_can_finish_within_startup_budget(probe):
    children,options,codes=probe
    codes.append('import time,json;time.sleep(.2);print(json.dumps([4,9,'+repr(BOOT)+']))')
    report={};check.probe_helper('/sealed/gitgo-host',report,startup_seconds=2,diagnostic_after=.05)
    assert report['passed'] and report['slow_start'] and report['startup_sample']['sampled_live_child']


def test_startup_deadline_kills_and_preserves_partial_output(probe):
    children,options,codes=probe
    codes.append('import time;print("python-entry",flush=True);time.sleep(10)')
    report={}
    with pytest.raises(RuntimeError,match='startup deadline'):
        check.probe_helper('/sealed/gitgo-host',report,startup_seconds=.3,diagnostic_after=.1)
    assert report['timed_out'] and report['reaped'] and 'python-entry' in report['stdout']
    assert children[0].poll() is not None and not report.get('passed')


def test_wrong_contract_never_admits_tasks(probe):
    children,options,codes=probe;codes.append('print("[1,5,null]")')
    report={}
    with pytest.raises(RuntimeError,match='contract'):check.probe_helper('/sealed/gitgo-host',report)
    assert report['reaped'] and not report.get('passed')


def test_install_failure_still_emits_report(monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['check','--run','--staged','--bundle','fixture'])
    monkeypatch.setenv('SUDO_UID','501');monkeypatch.setattr(check.os,'getuid',lambda:0)
    def fail(*a,**k):raise ValueError('injected install failure')
    monkeypatch.setattr(check,'install',fail)
    with pytest.raises(ValueError):check.main()
    report=json.loads(capsys.readouterr().out)
    assert report['phase']=='install' and not report['passed'] and report['cases']==[]
    assert 'injected install failure' in report['error']
