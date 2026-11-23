"""Acceptance must prove the fault occurred after output with live detached work."""
import json
import os
import sys
from types import SimpleNamespace
import pytest
if sys.platform!='darwin':pytest.skip('macOS acceptance',allow_module_level=True)
from scripts import check_macos_git_broker_faults as harness
from backend.core import macos_git_read as git


@pytest.mark.parametrize('change',['same_session','same_group','dead_descendant','wrong_uid','wrong_output','not_running','kernel_group_changed'])
def test_readiness_rejects_faults_that_would_not_exercise_detached_work(tmp_path_factory,monkeypatch,change):
    work=tmp_path_factory/'work';work.mkdir()
    uid=os.getuid()
    leader=dict(pid=100,sid=100,pgid=100,uid=uid,git_output='blob\n',outside_denied=True)
    detached=dict(pid=102,sid=101,pgid=101,uid=uid)
    state=dict(uid=uid,workspace=str(work),phase='running',launch_count=1)
    if change=='same_session':detached['sid']=100
    if change=='same_group':detached['pgid']=100
    if change=='wrong_uid':detached['uid']=uid+1
    if change=='wrong_output':leader['git_output']=''
    if change=='not_running':state['phase']='clean'
    (work/'git-query-ready').write_text(json.dumps(leader))
    (work/'detached-ready').write_text(json.dumps(detached))
    monkeypatch.setattr(harness,'load_state',lambda _:state)
    monkeypatch.setattr(harness.identity,'owned_rows',lambda _:[{'pid':100}]+([] if change=='dead_descendant' else [{'pid':102}]))
    monkeypatch.setattr(harness.os,'getsid',lambda _:101)
    monkeypatch.setattr(harness.os,'getpgid',lambda _:999 if change=='kernel_group_changed' else 101)
    with pytest.raises(RuntimeError,match='live detached'):harness.observe_ready(tmp_path_factory)


def test_no_readiness_marker_is_not_a_pass(tmp_path_factory,monkeypatch):
    monkeypatch.setattr(harness,'load_state',lambda _:dict(uid=os.getuid(),workspace=str(tmp_path_factory)))
    assert harness.observe_ready(tmp_path_factory) is None


def test_test_wrapper_does_not_accept_shell_or_redirected_git_command(tmp_path_factory):
    policy=SimpleNamespace(workspace=tmp_path_factory)
    command=git.read_command(tmp_path_factory/'object-view',['cat-file','-t','a'*40])
    wrapper=harness.instrumented_command(command,policy)
    assert json.loads(wrapper[-2])==command
    for modified in (['/bin/sh','-c','true'],['/different/git',*command[1:]],
                     [*command[:-3],'status'],git.read_command(tmp_path_factory/'other',['cat-file','-t','a'*40])):
        with pytest.raises((ValueError,git.GitBoundaryError)):harness.instrumented_command(modified,policy)


def test_fixture_cannot_instrument_persistent_broker(monkeypatch):
    monkeypatch.setattr(harness.os,'getuid',lambda:0)
    monkeypatch.setattr(harness.os,'geteuid',lambda:0)
    with pytest.raises(ValueError,match='persistent'):
        harness.serve_fixture('/private/var/db/gitgo-helper/production')


def test_checkout_wrapper_accepts_only_broker_selected_fixed_code(tmp_path_factory):
    from backend.core.macos_git_checkout import command as checkout_command
    policy=SimpleNamespace(workspace=tmp_path_factory)
    command=checkout_command(tmp_path_factory/'object-view',tmp_path_factory/'checkout','a'*40)
    assert json.loads(harness.instrumented_command(command,policy)[-2])==command
    for change in ([*command[:-4],'print("different")',*command[-3:]],
                   [*command[:-3],str(tmp_path_factory/'other'),*command[-2:]],
                   [*command[:-1],'HEAD']):
        with pytest.raises((ValueError,git.GitBoundaryError)):harness.instrumented_command(change,policy)


@pytest.mark.parametrize('change',[None,'wrong_commit','false_files','bad_proof'])
def test_checkout_readiness_requires_real_commit_proof_and_materialized_files(tmp_path_factory,monkeypatch,change):
    import base64
    from backend.core.macos_git_checkout import object_oid
    raw=b'tree '+b'a'*40+b'\n\nfixture\n';commit=object_oid('commit',raw)
    uid=os.getuid();work=tmp_path_factory
    leader=dict(pid=100,sid=100,pgid=100,uid=uid,outside_denied=True,
        git_output=json.dumps(dict(commit=base64.b64encode(raw).decode())),checkout_oid=commit,checkout_files_matched=True)
    if change=='wrong_commit':leader['checkout_oid']='b'*40
    if change=='false_files':leader['checkout_files_matched']=False
    if change=='bad_proof':leader['git_output']='{"commit":"@@"}'
    detached=dict(pid=102,sid=101,pgid=101,uid=uid)
    (work/'git-query-ready').write_text(json.dumps(leader));(work/'detached-ready').write_text(json.dumps(detached))
    monkeypatch.setattr(harness,'load_state',lambda _:dict(uid=uid,workspace=str(work),phase='running',launch_count=1))
    monkeypatch.setattr(harness.identity,'owned_rows',lambda _:[{'pid':100},{'pid':102}])
    monkeypatch.setattr(harness.os,'getsid',lambda _:101);monkeypatch.setattr(harness.os,'getpgid',lambda _:101)
    if change:
        with pytest.raises(RuntimeError,match='live detached'):harness.observe_ready(work,checkout_commit=commit)
    else:assert harness.observe_ready(work,checkout_commit=commit)['leader']['checkout_files_matched']
