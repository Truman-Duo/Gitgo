"""Governance metadata remains one project-bound, versioned authority."""
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,os
import pytest
from backend.core.config import Config,ProjectConfig
from backend.core.sync_session import SyncSession,FormalCommit
from backend.core.history import HistoryManager
from backend.core.state_reader import StateReader


def new_session(root):
    root.mkdir(exist_ok=True)
    project=ProjectConfig(name='formal-test');project.workspace_path=str(root)
    session=SyncSession(project,Config(projects=[project]))
    return session


def formal(tag,number=0):
    return FormalCommit(message=tag,number=number,prefix='FORM')


def test_save_never_follows_legacy_state_hardlink(tmp_path_factory):
    ws=tmp_path_factory/'workspace';session=new_session(ws)
    target=tmp_path_factory/'outside.json';target.write_bytes(b'outside must stay unchanged')
    meta=ws/'.gitgo';meta.mkdir(exist_ok=True);os.link(target,meta/'session.json')
    session.formal_commits=[formal('synthetic state')]
    session.save_session()
    assert target.read_bytes()==b'outside must stay unchanged'


def test_new_governance_session_restores_authoritative_metadata(tmp_path_factory):
    session=new_session(tmp_path_factory)
    session.formal_commits=[formal('preserved state')];session.save_session()
    restored=SyncSession(session.project,session.config)
    assert [f.message for f in restored.formal_commits]==['preserved state']


def test_conflicting_session_cannot_overwrite_newer_head(tmp_path_factory):
    session=new_session(tmp_path_factory);session.formal_commits=[formal('base')];session.save_session()
    left=SyncSession.load_session(session.project,session.config)
    right=SyncSession.load_session(session.project,session.config)
    left.formal_commits.append(formal('left',1));left.save_session()
    right.formal_commits.append(formal('stale-right',1))
    with pytest.raises(RuntimeError,match='GOVERNANCE_STATE_CHANGED'):
        right.save_session()
    current=SyncSession.load_session(session.project,session.config)
    assert [f.message for f in current.formal_commits]==['base','left']


def test_authoritative_empty_state_does_not_resurrect_old_history(tmp_path_factory):
    session=new_session(tmp_path_factory);session.formal_commits=[];session.save_session()
    with HistoryManager.workspace_scope(str(tmp_path_factory)):
        HistoryManager.add_operation('formal-test','governance_synced','success',{'commit':'[FORM-0] removed'})
        assert StateReader.get_formal_commits('formal-test',str(tmp_path_factory))==[]


def test_legacy_state_is_preserved_after_migration(tmp_path_factory):
    meta=tmp_path_factory/'.gitgo';meta.mkdir(exist_ok=True)
    original=json.dumps({'project':'formal-test','stage':'IDLE','formal_commits':[{'message':'legacy source','number':3,'prefix':'FORM'}]}).encode()
    (meta/'session.json').write_bytes(original)
    session=new_session(tmp_path_factory)
    assert [f.message for f in session.formal_commits]==['legacy source']
    session.formal_commits.append(formal('new state',4));session.save_session()
    assert (meta/'session.json').read_bytes()==original
    assert len(StateReader.get_formal_commits('formal-test',str(tmp_path_factory)))==2


def test_formal_number_does_not_reuse_regressed_legacy_counter(tmp_path_factory):
    from backend.core.operations.models import CommitInfo
    session=new_session(tmp_path_factory)
    session.commits=[CommitInfo(hash='0'*40,subject='synthetic',type='chore',scope=None)]
    session.bk_git_runner=SimpleNamespace(is_git_repo=lambda:False)
    first=session.step_create_formal_commit(set(),message='first')
    second=session.step_create_formal_commit(set(),message='second')
    (tmp_path_factory/'.gitgo').mkdir(exist_ok=True)
    (tmp_path_factory/'.gitgo/next_number').write_text('0',encoding='utf8')
    third=session.step_create_formal_commit(set(),message='third')
    assert first.number<second.number<third.number


def test_daemon_formalize_honors_explicit_selection():
    from backend.core.daemon.executors import _exec_formalize
    observed=[]
    session=SimpleNamespace(step_create_formal_commit=lambda **kw:observed.append(kw) or formal('selected'))
    _exec_formalize({},session,SimpleNamespace(),{'indices':[1],'message':'selected'})
    assert observed[0]['selected_indices']=={1}


def test_formalize_is_not_a_native_state_handler():
    from backend.core.tools.runner import handler_bindings
    assert 'formalize' not in handler_bindings()


def test_injected_storage_remains_the_governance_authority(tmp_path_factory):
    from backend.core.storage import StorageRuntime,bind_storage
    ws=tmp_path_factory/'workspace';ws.mkdir()
    with StorageRuntime(ws,state_home=tmp_path_factory/'custom-state') as store:
        bind_storage(ws,store)
        session=new_session(ws);session.formal_commits=[formal('injected')];session.save_session()
        assert [f.message for f in SyncSession(session.project,session.config).formal_commits]==['injected']


def test_formalize_pipeline_records_metadata_without_a_git_commit(git_repo):
    from backend.core.tools.governance_service import build_formalize_tool
    from backend.core.loop.tool_pipeline import ToolPipeline
    from tests.test_file_feedback_contract import context
    from backend.adapters.local_git_runner import LocalGitRunner
    session=new_session(git_repo);tool=build_formalize_tool(session)
    before=LocalGitRunner(git_repo).rev_parse()
    ctx=context(git_repo,[tool.name])
    result=ToolPipeline().execute({'name':tool.name,'args':{'message':'synthetic metadata','indices':[0]}},tool,ctx,'formalize')
    assert not result.is_error,result.formatted
    assert result.data['source_indices']==[0] and result.data['git_commit_created'] is False
    assert result.receipt['governance_state_ref']==result.data['governance_state_ref']
    assert LocalGitRunner(git_repo).rev_parse()==before
    assert [f.message for f in SyncSession(session.project,session.config).formal_commits]==['synthetic metadata']
    assert not (git_repo/'.gitgo/session.json').exists()


def test_child_workspace_cannot_change_owning_project_metadata(tmp_path_factory):
    from backend.core.tools.governance_service import build_formalize_tool
    from backend.core.loop.tool_pipeline import ToolPipeline
    from tests.test_file_feedback_contract import context
    session=new_session(tmp_path_factory/'root');tool=build_formalize_tool(session)
    child=tmp_path_factory/'child';child.mkdir();ctx=context(child,[tool.name])
    result=ToolPipeline().execute({'name':tool.name,'args':{'message':'wrong owner'}},tool,ctx,'child')
    assert result.is_error and result.data['error']=='GOVERNANCE_WORKSPACE_MISMATCH'
    assert result.receipt['effect_state']=='not_committed'
    assert session.formal_commits==[]


def test_unknown_ack_preserves_possible_commit_instead_of_claiming_no_change(git_repo,monkeypatch):
    from backend.core.tools.governance_service import build_formalize_tool
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.storage import StorageCommitUnknown
    from tests.test_file_feedback_contract import context
    session=new_session(git_repo);tool=build_formalize_tool(session)
    actual=SyncSession.save_session
    def lose_ack(self):
        actual(self)
        raise StorageCommitUnknown('commit')  # Protocol fault; real SQL fault tested separately.
    monkeypatch.setattr(SyncSession,'save_session',lose_ack)
    result=ToolPipeline().execute({'name':tool.name,'args':{'message':'possibly committed','indices':[0]}},tool,context(git_repo,[tool.name]),'unknown')
    assert result.is_error and result.data['error']=='STORAGE_COMMIT_UNKNOWN'
    assert result.receipt['effect_state']=='ambiguous'
    assert result.data['error_info']['state_changed'] is None
    assert [f.message for f in SyncSession(session.project,session.config).formal_commits]==['possibly committed']


def test_invalid_formal_selection_is_not_a_crash_or_a_commit(git_repo):
    from backend.core.tools.governance_service import build_formalize_tool
    from backend.core.loop.tool_pipeline import ToolPipeline
    from tests.test_file_feedback_contract import context
    session=new_session(git_repo);tool=build_formalize_tool(session)
    result=ToolPipeline().execute({'name':tool.name,'args':{'message':'bad selection','indices':[100]}},tool,context(git_repo,[tool.name]),'invalid')
    assert result.is_error and result.data['error']=='FORMALIZE_INVALID_SELECTION'
    assert result.receipt['effect_state']=='not_committed'
    assert SyncSession(session.project,session.config).formal_commits==[]


def test_linked_workspaces_keep_separate_sessions_and_share_number_authority(git_repo):
    from backend.adapters.local_git_runner import LocalGitRunner
    from backend.core.storage import get_storage
    from backend.core.sync_session.state import reserve_formal_number
    child = git_repo.parent / "linked"
    result = LocalGitRunner(git_repo).run(["worktree", "add", "--detach", str(child), "HEAD"])
    assert result.returncode == 0, result.stderr
    root_session = new_session(git_repo)
    child_session = new_session(child)
    root_session.formal_commits = [formal("root only", 5)]
    root_session.save_session()
    child_session.formal_commits = [formal("child only", 6)]
    child_session.save_session()
    assert get_storage(git_repo).paths.project_id == get_storage(child).paths.project_id
    assert [f.message for f in SyncSession(root_session.project, root_session.config).formal_commits] == ["root only"]
    assert [f.message for f in SyncSession(child_session.project, child_session.config).formal_commits] == ["child only"]
    first = reserve_formal_number(git_repo, 7)
    second = reserve_formal_number(child, 0)
    assert second > first


def test_head_compare_rejects_stale_updates_without_permanent_snapshot_roots(tmp_path_factory):
    from backend.core.storage import get_storage
    store = get_storage(tmp_path_factory)
    left = store.put_blob(b"left"); right = store.put_blob(b"right")
    assert store.compare_state_ref("synthetic-head", "head", left, expected_ref=None)
    assert not store.compare_state_ref("synthetic-head", "head", right, expected_ref=None)
    assert store.get_state_ref("synthetic-head", "head") == left
    assert store.compare_state_ref("synthetic-head", "head", right, expected_ref=left)
    rows = store.list_state_refs("synthetic-head", prefix="")
    assert len(rows) == 1 and rows[0]["value_ref"] == right


@pytest.mark.parametrize("operation", ["create", "delete", "edit", "renumber", "dissolve"])
def test_metadata_observer_failure_never_turns_committed_mutation_into_failure(tmp_path_factory, monkeypatch, operation):
    from backend.core.operations.models import CommitInfo
    session = new_session(tmp_path_factory)
    session.commits = [CommitInfo(hash="0"*40, subject="synthetic", type="chore", scope=None)]
    session.formal_commits = [formal("original", 1)]
    session.formal_commits[0].source_indices = {0}
    session.save_session()
    def unavailable(*args, **kwargs):
        raise OSError("synthetic observer unavailable")
    monkeypatch.setattr(HistoryManager, "add_operation", unavailable)
    session.on_log = unavailable
    if operation == "create":
        assert session.step_create_formal_commit(set(), message="created")
    elif operation == "delete":
        assert session.step_delete_formal(0)
    elif operation == "edit":
        assert session.step_edit_formal_message(0, "[FORM-1] feat: edited")
    elif operation == "renumber":
        assert session.step_edit_formal_number(0, 9)
    else:
        assert session.step_dissolve_formal(0)
    restored = SyncSession(session.project, session.config)
    assert [(f.message,f.number) for f in restored.formal_commits] == [(f.message,f.number) for f in session.formal_commits]
    assert session._metadata_audit_warning["code"] == "GOVERNANCE_AUDIT_PENDING"


def test_conflicting_edit_emits_no_success_notification_or_history(tmp_path_factory, monkeypatch):
    from backend.core.sync_session.state import GovernanceStateChanged
    current = new_session(tmp_path_factory);current.formal_commits=[formal("base")];current.save_session()
    stale=SyncSession(current.project,current.config)
    current.formal_commits[0].message="newer";current.save_session()
    observed=[]
    stale.on_log=lambda message: observed.append(message)
    monkeypatch.setattr(HistoryManager,"add_operation",lambda *a,**k:observed.append("history"))
    with pytest.raises(GovernanceStateChanged): stale.step_edit_formal_message(0,"[FORM-0] feat: stale")
    assert observed==[]
    assert SyncSession(current.project,current.config).formal_commits[0].message=="newer"


@pytest.mark.parametrize("data", [{"stage":[]}, {"formal_commits":[{"message":"bad","number":True,"prefix":"P"}]}])
def test_malformed_legacy_state_is_explicitly_unavailable(tmp_path_factory,data):
    from backend.core.sync_session.state import GovernanceStateUnavailable
    meta=tmp_path_factory/".gitgo";meta.mkdir();(meta/"session.json").write_text(json.dumps(data),encoding="utf8")
    with pytest.raises(GovernanceStateUnavailable): new_session(tmp_path_factory)


@pytest.mark.parametrize("failure", ["changed", "unavailable"])
def test_reserved_number_is_an_effect_even_when_formal_metadata_cannot_publish(git_repo, monkeypatch, failure):
    from backend.core.tools.governance_service import build_formalize_tool
    from backend.core.loop.tool_pipeline import ToolPipeline
    from backend.core.sync_session.state import GovernanceStateChanged, GovernanceStateUnavailable, peek_formal_number, read_state
    from tests.test_file_feedback_contract import context
    session=new_session(git_repo);tool=build_formalize_tool(session)
    before=peek_formal_number(git_repo)
    error=GovernanceStateChanged if failure=="changed" else GovernanceStateUnavailable
    def cannot_save(self): raise error()
    monkeypatch.setattr(SyncSession,"save_session",cannot_save)
    result=ToolPipeline().execute({"name":tool.name,"args":{"message":"not published","indices":[0]}},tool,context(git_repo,[tool.name]),"reserved")
    assert result.is_error and result.data["error"]==error.code
    assert peek_formal_number(git_repo)>before
    assert read_state(git_repo)==(None,None)
    assert result.receipt["effect_state"]=="partial"
    assert result.data["error_info"]["state_changed"] is True
    assert result.data["execution_state"]=="completed"
