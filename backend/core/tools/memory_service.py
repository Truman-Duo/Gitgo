"""Project-bound tool-memory capabilities; metadata and file effects stay distinct."""
from pathlib import Path
from backend.core.loop.agent_tool import AgentTool, CancellationMode
from backend.core.loop.execution_contract import data_broker
from backend.core.prompt_templates import load_prompt
from backend.core.errors import error_payload


def _fail(code, state="not_committed"):
    payload=error_payload(code)
    if state=="ambiguous":payload["error_info"]["state_changed"]=None
    return {**payload,"effect_state":state,
            "execution_state":"unknown" if state=="ambiguous" else "not_started"}


def build_memory_tools(session):
    root=Path(session.workspace_path).resolve()
    backup=Path(session.backup_path).resolve() if session.backup_path else None
    def prepare(args):
        return {**dict(args or {}),"_workspace":str(root)}
    def bound(args):
        if Path(args["_workspace"]).resolve()!=root:
            return _fail("GOVERNANCE_WORKSPACE_MISMATCH")
        if backup is None:return _fail("MEMORY_BACKUP_UNCONFIGURED")
        release=getattr(session.project,"release",None)
        access=getattr(release,"file_access",None)
        if access is not None and str(getattr(getattr(access,"kind",None),"value",getattr(access,"kind","local"))) != "local":
            return _fail("MEMORY_ARCHIVE_UNAVAILABLE")
        # Old transcripts may still carry public paths; they never select a root.
        for field, expected in (("workspace_path",root),("backup_path",backup)):
            if field in args and Path(args[field]).resolve()!=expected:
                return _fail("GOVERNANCE_WORKSPACE_MISMATCH")
        return None
    def take(args):
        denied=bound(args)
        if denied:return denied
        from backend.core.identity.snapshot import snapshot_tool_memories,list_memory_snapshots
        from backend.core.storage import StorageCommitUnknown
        try:
            result=snapshot_tool_memories(root,backup,session.project)
        except StorageCommitUnknown:return _fail("STORAGE_COMMIT_UNKNOWN","ambiguous")
        except Exception:return _fail("MEMORY_ARCHIVE_UNAVAILABLE")
        response={"snapshot":result,"effect_state":result["effect_state"],"execution_state":result["execution_state"]}
        if result.get("error"):response["error"]=result["error"]
        try:response["all_snapshots"]=list_memory_snapshots(backup,root)
        except Exception as exc:
            response["warnings"]=[{"code":"MEMORY_SNAPSHOT_LIST_UNAVAILABLE","error_type":type(exc).__name__,
                "message":"Snapshot outcome is preserved; the subsequent listing failed. Do not repeat snapshot creation."}]
        return response
    def restore(args):
        denied=bound(args)
        if denied:return denied
        from backend.core.identity.snapshot import restore_tool_memories
        return restore_tool_memories(backup,root,args.get("snapshot_timestamp"),service_args=args,project_name=session.project.name)
    paths={"workspace_path":{"type":"string","description":"Compatibility only; must match the owning project."},
           "backup_path":{"type":"string","description":"Compatibility only; must match the configured backup."}}
    common=dict(prepare_args=prepare,read_only=False,resources=["filesystem:*"],cancellation=CancellationMode.NON_INTERRUPTIBLE)
    return {
        "memory_snapshot":AgentTool(name="memory_snapshot",description=load_prompt("tools/memory_snapshot"),
            parameters={"type":"object","properties":paths,"additionalProperties":False},execute=take,
            execution_contract=data_broker("identity.memory_snapshot"),**common),
        "memory_restore":AgentTool(name="memory_restore",description=load_prompt("tools/memory_restore"),
            parameters={"type":"object","properties":{**paths,"snapshot_timestamp":{"type":"string"},
                "candidate_ref":{"type":"string"},"lesson_feedback_id":{"type":"string"}},"additionalProperties":False},
            execute=restore,execution_contract=data_broker("files.workspace.memory_restore"),**common)}
