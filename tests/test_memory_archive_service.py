"""Tool-memory snapshot data has bounded, immutable, project-bound ownership."""
import os
from pathlib import Path
import pytest
from backend.core.identity.memory_archive import snapshot,list_archives,read_archive,capture_sources


def roots(base):
    ws=base/"workspace";backup=base/"backup"
    ws.mkdir();backup.mkdir();(ws/".codex").mkdir()
    return ws,backup


def test_same_second_same_size_and_mtime_edits_are_distinct_immutable_snapshots(tmp_path_factory):
    ws,backup=roots(tmp_path_factory);note=ws/".codex/note.txt"
    note.write_bytes(b"first");os.utime(note,(1000000000,1000000000))
    first=snapshot(ws,backup)
    note.write_bytes(b"other");os.utime(note,(1000000000,1000000000))
    second=snapshot(ws,backup)
    assert first["timestamp"]!=second["timestamp"]
    rows=list_archives(backup,ws);assert len(rows)==2 and rows[0]["sequence"]>rows[1]["sequence"]
    assert read_archive(backup,rows[0],ws)["files"][".codex/note.txt"]=="b3RoZXI="
    assert read_archive(backup,rows[1],ws)["files"][".codex/note.txt"]=="Zmlyc3Q="


def test_snapshot_empty_is_no_sources_instead_of_failed_pruning(tmp_path_factory):
    ws=tmp_path_factory/"empty";backup=tmp_path_factory/"backup";ws.mkdir();backup.mkdir()
    result=snapshot(ws,backup)
    assert result["status"]=="no_sources" and result["effect_state"]=="not_committed"
    assert not (backup/".gitgo").exists()


def test_snapshot_rejects_external_hardlink_before_any_snapshot_publish(tmp_path_factory):
    ws,backup=roots(tmp_path_factory);external=tmp_path_factory/"outside";external.write_bytes(b"private synthetic")
    os.link(external,ws/".codex/note.txt")
    with pytest.raises(PermissionError):snapshot(ws,backup)
    assert list_archives(backup,ws)==[] and not (backup/".gitgo").exists()


def test_archive_retention_bounds_roots_not_user_files(tmp_path_factory):
    ws,backup=roots(tmp_path_factory);note=ws/".codex/note.txt"
    for n in range(8):note.write_text(str(n));snapshot(ws,backup)
    rows=list_archives(backup,ws)
    assert len(rows)==5 and [r["sequence"] for r in rows]==[8,7,6,5,4]
    assert note.read_text()=="7"


def test_same_backup_separates_workspace_archives(tmp_path_factory):
    ws,backup=roots(tmp_path_factory);other=tmp_path_factory/"other";other.mkdir();(other/".codex").mkdir()
    (ws/".codex/note.txt").write_bytes(b"left");(other/".codex/note.txt").write_bytes(b"right")
    snapshot(ws,backup);snapshot(other,backup)
    assert len(list_archives(backup,ws))==1 and len(list_archives(backup,other))==1
    with pytest.raises(RuntimeError):read_archive(backup,list_archives(backup,ws)[0],other)


def tool_context(ws,backup):
    from types import SimpleNamespace
    from backend.core.tools.memory_service import build_memory_tools
    from tests.test_file_feedback_contract import context
    tools=build_memory_tools(SimpleNamespace(workspace_path=ws,backup_path=backup,project=SimpleNamespace(name="p")))
    ctx=context(ws,list(tools));ctx.process.update_context_snapshot(lambda snap:{**snap,"project_name":"p"})
    return tools,ctx


def invoke(tool,ctx,args,index="memory"):
    from backend.core.loop.tool_pipeline import ToolPipeline
    return ToolPipeline().execute({"name":tool.name,"id":index,"args":args},tool,ctx,index)


def test_native_runner_cannot_reopen_memory_state_writers():
    from backend.core.tools.runner import handler_bindings
    assert not {"memory_snapshot","memory_restore"}&set(handler_bindings())


def test_root_file_source_roundtrips_and_lists_its_real_type(tmp_path_factory):
    ws=tmp_path_factory/"ws";backup=tmp_path_factory/"bk";ws.mkdir();backup.mkdir()
    (ws/".codex").write_bytes(b"file rather than directory")
    snapshot(ws,backup);row=list_archives(backup,ws)[0]
    assert row["is_dir"] is False
    assert read_archive(backup,row,ws)["files"][".codex"]


def test_restore_pipeline_uses_file_receipts_and_recoverable_deletions(tmp_path_factory):
    ws,backup=roots(tmp_path_factory);(ws/".codex/note.txt").write_bytes(b"saved")
    tools,ctx=tool_context(ws,backup)
    saved=invoke(tools["memory_snapshot"],ctx,{})
    assert not saved.is_error and saved.receipt["effect_state"]=="committed"
    (ws/".codex/note.txt").write_bytes(b"edited");(ws/".codex/extra.txt").write_bytes(b"added after snapshot")
    result=invoke(tools["memory_restore"],ctx,{})
    assert not result.is_error,result.formatted
    assert (ws/".codex/note.txt").read_bytes()==b"saved" and not (ws/".codex/extra.txt").exists()
    assert result.receipt["file_service_result"] is True
    assert result.receipt["files"]==[".codex/extra.txt",".codex/note.txt"]
    deleted=next(row for row in result.receipt["file_mutations"] if row["operation"]=="delete_file")
    assert deleted["recoverable"] is True and (ws/deleted["trash_ref"]).read_bytes()==b"added after snapshot"


def test_restore_lesson_reminder_reuses_frozen_bytes_and_rejects_changed_targets(tmp_path_factory):
    from backend.core.knowledge.manager import LessonManager
    from backend.core.knowledge.models import Lesson
    from backend.core.knowledge.feedback import observe_provider_input
    ws,backup=roots(tmp_path_factory);note=ws/".codex/note.txt";note.write_bytes(b"saved")
    snapshot(ws,backup);note.write_bytes(b"edited")
    LessonManager.save(ws,Lesson(id="advice",project_name="p",trigger="note.txt",rule="Preserve tool identity scope"))
    tools,ctx=tool_context(ws,backup);tool=tools["memory_restore"]
    first=invoke(tool,ctx,{})
    assert first.data["error"]=="LESSON_FEEDBACK_REQUIRED" and note.read_bytes()==b"edited"
    args={"candidate_ref":first.data["candidate_ref"],"lesson_feedback_id":first.data["lesson_feedback"]["feedback_id"]}
    assert invoke(tool,ctx,args,"unexposed").is_error
    ctx.session.append_tool_result(first.formatted,tool_name=tool.name,tool_call_id="memory",data=first.data,receipt=first.receipt)
    observe_provider_input(ctx.process,[{"role":"tool","content":first.formatted}])
    note.write_bytes(b"concurrent")
    stale=invoke(tool,ctx,args,"stale")
    assert stale.data["error"]=="FILE_CHANGED" and note.read_bytes()==b"concurrent"
    note.write_bytes(b"edited")
    final=invoke(tool,ctx,args,"continued")
    assert not final.is_error,final.formatted
    assert note.read_bytes()==b"saved" and final.data["candidate_ref"]==first.data["candidate_ref"]
    assert final.receipt["lesson_advisory_state"]=="continuation_requested"


def test_restore_registered_mandatory_check_refuses_old_bad_snapshot(tmp_path_factory):
    from backend.core.knowledge.manager import LessonManager
    from backend.core.knowledge.models import Lesson
    ws,backup=roots(tmp_path_factory);note=ws/".codex/note.txt";note.write_bytes(b"retired()")
    snapshot(ws,backup);note.write_bytes(b"current()")
    LessonManager.save(ws,Lesson(id="check",project_name="p",trigger="note.txt",rule="Avoid retired()",
        dangerous_tools=["write_file"],check={"authority":"registered","mode":"violation_pattern","pattern":"retired"}))
    tools,ctx=tool_context(ws,backup);result=invoke(tools["memory_restore"],ctx,{})
    assert result.data["error"]=="KNOWN_LESSON_VIOLATION" and note.read_bytes()==b"current()"
    assert result.receipt["effect_state"]=="not_committed"


def test_legacy_disk_snapshot_is_read_only_compatible_input(tmp_path_factory):
    from backend.core.identity.snapshot import restore_tool_memories
    ws,backup=roots(tmp_path_factory)
    legacy=backup/".gitgo/memories/.codex_20261001_000000";legacy.mkdir(parents=True)
    (legacy/"note.txt").write_bytes(b"legacy");(ws/".codex/note.txt").write_bytes(b"new")
    result=restore_tool_memories(backup,ws,"20261001_000000")
    assert not result.get("error"),result
    assert (ws/".codex/note.txt").read_bytes()==b"legacy"
    assert (legacy/"note.txt").read_bytes()==b"legacy"


def test_memory_tools_require_configured_roots_and_reject_child_scope(tmp_path_factory):
    ws,backup=roots(tmp_path_factory);tools,ctx=tool_context(ws,None)
    assert invoke(tools["memory_snapshot"],ctx,{}).data["error"]=="MEMORY_BACKUP_UNCONFIGURED"
    tools,ctx=tool_context(ws,backup)
    assert invoke(tools["memory_snapshot"],ctx,{"backup_path":str(tmp_path_factory/"other")}).is_error
    from tests.test_file_feedback_contract import context
    child=tmp_path_factory/"child";child.mkdir()
    assert invoke(tools["memory_snapshot"],context(child,list(tools)),{}).data["error"]=="GOVERNANCE_WORKSPACE_MISMATCH"


def test_retention_failure_preserves_successful_new_snapshot(tmp_path_factory,monkeypatch):
    from backend.core.storage import get_storage
    ws,backup=roots(tmp_path_factory);note=ws/".codex/note.txt";note.write_bytes(b"first")
    for n in range(5):snapshot(ws,backup)
    store=get_storage(backup)
    def unavailable(*a,**k):raise OSError("synthetic retention failure")
    monkeypatch.setattr(store,"release_state_ref",unavailable)
    note.write_bytes(b"latest");result=snapshot(ws,backup)
    assert result["effect_state"]=="committed" and result["warnings"][0]["code"]=="MEMORY_RETENTION_PENDING"
    assert read_archive(backup,list_archives(backup,ws)[0],ws)["files"][".codex/note.txt"]=="bGF0ZXN0"


def test_unknown_archive_publish_ack_does_not_claim_no_change(tmp_path_factory,monkeypatch):
    from backend.core.storage import get_storage,StorageCommitUnknown
    ws,backup=roots(tmp_path_factory);(ws/".codex/note.txt").write_bytes(b"saved")
    store=get_storage(backup);actual=store.compare_state_ref
    def lose_ack(*a,**k):actual(*a,**k);raise StorageCommitUnknown("commit")
    monkeypatch.setattr(store,"compare_state_ref",lose_ack)
    tools,ctx=tool_context(ws,backup);result=invoke(tools["memory_snapshot"],ctx,{})
    assert result.data["error"]=="STORAGE_COMMIT_UNKNOWN" and result.receipt["effect_state"]=="ambiguous"
    assert result.data["error_info"]["state_changed"] is None
    assert len(list_archives(backup,ws))==1


def test_restore_partial_commit_preserves_evidence_and_cannot_blindly_replay(tmp_path_factory,monkeypatch):
    from backend.core.tools import workspace_tools
    ws,backup=roots(tmp_path_factory)
    for name in ("a.txt","b.txt"):(ws/".codex"/name).write_bytes(b"saved")
    snapshot(ws,backup)
    for name in ("a.txt","b.txt"):(ws/".codex"/name).write_bytes(b"edited")
    actual=workspace_tools._atomic_write
    def fail_second(path,raw):
        if path.name=="b.txt":raise OSError("synthetic second-file failure")
        return actual(path,raw)
    monkeypatch.setattr(workspace_tools,"_atomic_write",fail_second)
    tools,ctx=tool_context(ws,backup);tool=tools["memory_restore"]
    result=invoke(tool,ctx,{})
    assert result.is_error and result.receipt["effect_state"]=="ambiguous"
    assert (ws/".codex/a.txt").read_bytes()==b"saved" and (ws/".codex/b.txt").read_bytes()==b"edited"
    retry=invoke(tool,ctx,{"candidate_ref":result.data["candidate_ref"]},"retry")
    assert retry.data["error"]=="FILE_CHANGED"
    assert (ws/".codex/b.txt").read_bytes()==b"edited"


def test_backup_cannot_be_nested_inside_its_own_input_source(tmp_path_factory):
    ws,backup=roots(tmp_path_factory);nested=ws/".codex/backup";nested.mkdir()
    with pytest.raises(RuntimeError):snapshot(ws,nested)
    assert not (nested/".gitgo").exists()


def test_snapshot_bounds_fail_without_publishing_a_truncated_version(tmp_path_factory):
    ws,backup=roots(tmp_path_factory)
    for n in range(129):(ws/".codex"/str(n)).write_bytes(b"x")
    with pytest.raises(RuntimeError):snapshot(ws,backup)
    assert list_archives(backup,ws)==[]


def test_application_snapshot_keeps_actual_domain_outcome(tmp_path_factory,monkeypatch):
    from backend.core.config import ProjectConfig,Config
    from backend.core.application import services
    ws,backup=roots(tmp_path_factory);project=ProjectConfig(name="p");project.workspace_path=str(ws);project.backup_path=str(backup)
    monkeypatch.setattr(services,"_project",lambda name:(Config(projects=[project]),project))
    import backend.core.identity.snapshot as module
    outcome={"error":"MEMORY_ARCHIVE_UNAVAILABLE","effect_state":"partial","snapped":[".codex"],"timestamp":"synthetic"}
    monkeypatch.setattr(module,"snapshot_tool_memories",lambda *a,**k:outcome)
    assert services.ApplicationServices().memory_snapshot("p")==outcome


@pytest.mark.parametrize("archive_fails",[False,True])
@pytest.mark.parametrize("contract_fails", [False, True])
def test_real_file_sync_preserves_result_when_memory_observation_succeeds_or_fails(git_repo,monkeypatch,archive_fails,contract_fails):
    from backend.adapters.local_git_runner import LocalGitRunner
    from backend.core.config import ProjectConfig,Config
    from backend.core.sync_session import SyncSession,FormalCommit
    from backend.core.operations.models import FileEntry
    from backend.core.history import HistoryManager
    import backend.core.identity.snapshot as module
    backup=git_repo.parent/"backup";backup.mkdir();runner=LocalGitRunner(backup)
    assert runner.run(["init"]).returncode == 0
    # Synthetic repository identity is fixture preparation, not a product API
    # for arbitrary repository configuration. Exercise real protected writes below.
    import subprocess
    for key, value in (("user.name", "Synthetic Test"), ("user.email", "test@local.gitgo.invalid")):
        subprocess.run(["git", "-C", str(backup), "config", key, value],
                       check=True, capture_output=True, timeout=15)
    (backup/"README.md").write_text("initial backup",encoding="utf8")
    assert runner.add_all() and runner.commit("initial backup")[0]
    initial_head=runner.rev_parse()
    project=ProjectConfig(name="memory-sync");project.workspace_path=str(git_repo);project.backup_path=str(backup)
    project.commit_format={"prefix":"TEST","number_start":0,"plugins":[]}
    session=SyncSession(project,Config(projects=[project]))
    (git_repo/"README.md").write_text("updated business content",encoding="utf8")
    (git_repo/".codex").mkdir();(git_repo/".codex/note.txt").write_text("synthetic identity",encoding="utf8")
    session.entries=[FileEntry("README.md","modified")]
    session.formal_commits=[FormalCommit(message="[TEST-0] feat: copy one business file",number=0,prefix="TEST")]
    session.save_session()
    if archive_fails:
        def fail(*a,**k):raise RuntimeError("synthetic post-sync archive failure")
        monkeypatch.setattr(module,"snapshot_tool_memories",fail)
    if contract_fails:
        from backend.core.contract import ContractManager, ContractStateUnavailable
        def contract_failure(*a, **k):
            raise ContractStateUnavailable("synthetic post-commit contract read failure")
        monkeypatch.setattr(ContractManager, "update_feature", contract_failure)
    assert session.step_sync() is True
    if contract_fails:
        assert any(w["operation"] == "contract_update" for w in session._last_sync_result["warnings"])
    assert (backup/"README.md").read_text(encoding="utf8")=="updated business content"
    assert SyncSession(project,session.config).formal_commits[0].synced is True
    assert runner.rev_parse()!=initial_head
    assert any("copy one business file" in row for row in runner.log())
    rows=[entry for entry in HistoryManager.load() if entry.operation=="governance_memory_snapshot"]
    assert len(rows)==1 and rows[0].status==("failed" if archive_fails else "success")
    if archive_fails:assert session._last_memory_snapshot_result["error"]=="MEMORY_ARCHIVE_UNAVAILABLE"
    else:assert len(list_archives(backup,git_repo))==1


def test_nested_backup_archives_survive_cold_readonly_lookup(git_repo):
    from backend.core.storage import release_storage
    backup=git_repo/"plain-backup";backup.mkdir();(git_repo/".codex").mkdir();(git_repo/".codex/note.txt").write_bytes(b"saved")
    snapshot(git_repo,backup)
    release_storage(backup)
    rows=list_archives(backup,git_repo)
    assert len(rows)==1
    assert read_archive(backup,rows[0],git_repo)["files"][".codex/note.txt"]=="c2F2ZWQ="


def test_sibling_backup_nodes_in_one_git_repository_do_not_mix_archives(git_repo):
    from backend.core.storage import get_storage
    first=git_repo/"backup-a";second=git_repo/"backup-b";first.mkdir();second.mkdir()
    (git_repo/".codex").mkdir();(git_repo/".codex/note.txt").write_bytes(b"saved")
    snapshot(git_repo,first);snapshot(git_repo,second)
    assert get_storage(first).paths.project_id==get_storage(second).paths.project_id
    assert len(list_archives(first,git_repo))==1 and len(list_archives(second,git_repo))==1


def test_legacy_restore_combines_sources_under_one_total_limit(tmp_path_factory,monkeypatch):
    from backend.core.identity.snapshot import restore_tool_memories
    from backend.core.tools import memory_restore_service
    ws,backup=roots(tmp_path_factory)
    for source in (".codex",".claude"):
        legacy=backup/".gitgo/memories"/(source+"_20261001_000000");legacy.mkdir(parents=True)
        (legacy/"note.txt").write_bytes(b"123")
    monkeypatch.setattr(memory_restore_service,"MAX_BYTES",4)
    result=restore_tool_memories(backup,ws,"20261001_000000")
    assert result["error"]=="MEMORY_ARCHIVE_UNAVAILABLE" and result["effect_state"]=="not_committed"
    assert not (ws/".codex/note.txt").exists() and not (ws/".claude").exists()
