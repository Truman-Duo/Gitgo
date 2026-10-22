"""Restore immutable tool-memory data using existing file checks and commits."""
import base64
import hashlib
import json
from pathlib import Path

from backend.core.identity.memory_archive import (SOURCES, MAX_FILES, MAX_BYTES,
    capture_sources, encoded, read_archive, MemoryArchiveError)
from .file_mutation import FileMutation, file_service_runtime
from .file_mutation_batch import FileMutationBatch, commit_file_batch
from .workspace_tools import _confined_path

_NAMESPACE = "memory_restore_candidates.v1"


def _failure(code, message=""):
    from backend.core.errors import error_payload
    return {**error_payload(code, **({"message":str(message)[:2000]} if message else {})),
            "effect_state":"not_committed", "execution_state":"not_started"}


def _selected(backup, workspace, timestamp):
    from backend.core.identity.snapshot import list_memory_snapshots, read_legacy_archive
    rows = list_memory_snapshots(backup,workspace)
    selected = {}; refs = {}; total = 0
    for source in SOURCES:
        row = next((row for row in rows if row["source"]==source and
                    (timestamp is None or row["timestamp"]==timestamp)),None)
        if row is None: continue
        selected[source] = (read_archive(backup,row,workspace) if row["format"]=="cas-v2"
                            else read_legacy_archive(backup,row,workspace))
        total += sum(len(base64.b64decode(content,validate=True)) for content in selected[source]["files"].values())
        if total > MAX_BYTES: raise MemoryArchiveError("Selected memory archives exceed the total restore bound")
        refs[source] = row.get("snapshot_ref") or "legacy:" + row["timestamp"]
    return selected, refs


def _project(backup, workspace, timestamp):
    before = capture_sources(workspace)
    selected, references = _selected(backup,workspace,timestamp)
    if not selected: raise MemoryArchiveError("No matching tool-memory snapshot")
    after = dict(before)
    for source, archive in selected.items():
        if source in before and before[source]["is_dir"] != archive["is_dir"]:
            raise MemoryArchiveError("File/directory shape changes need an explicit migration")
        if source not in before and archive["is_dir"] and not archive["files"]:
            raise MemoryArchiveError("An absent empty directory cannot be restored as file data")
        after[source] = archive
    old = {name:raw for archive in before.values() for name,raw in archive["files"].items()}
    new = {name:raw for archive in after.values() for name,raw in archive["files"].items()}
    names = sorted(set(old)|set(new))
    if len(names)>MAX_FILES: raise MemoryArchiveError("Restore candidate exceeds its file bound")
    members = []
    for name in names:
        if old.get(name)==new.get(name): continue
        # Apply directory/metadata restrictions before a pending candidate is saved.
        path = _confined_path(workspace,name,must_exist=False)
        from .file_operations import metadata_mutation_error
        if metadata_mutation_error(workspace,path):
            raise MemoryArchiveError("Tool-memory data cannot restore product or Git metadata")
        first = base64.b64decode(old.get(name,""),validate=True)
        last = base64.b64decode(new.get(name,""),validate=True)
        try: first.decode("utf8");last.decode("utf8");view=True
        except UnicodeError:view=False
        members.append({"path":name,"operation":"write_file" if name in new else "delete_file",
            "before":base64.b64encode(first).decode("ascii"),"after":base64.b64encode(last).decode("ascii"),
            "before_sha256":hashlib.sha256(first).hexdigest() if name in old else "", "text_view_available":view})
    return {"version":1,"before":before,"members":members,"sources":list(selected),"snapshots":references}


def restore_memory_files(backup, workspace, timestamp, *, service_args=None, project_name=""):
    args = dict(service_args or {});workspace=Path(workspace).resolve();backup=Path(backup).resolve()
    runtime=file_service_runtime()
    from backend.core.storage import get_storage, StorageCommitUnknown
    store=runtime.storage if runtime else get_storage(workspace)
    context=args.get("_lesson_context") or {}
    identity={"workspace":str(workspace),"backup":str(backup),"timestamp":timestamp,
              "owner":{k:context.get(k,"") for k in ("process_id","task_id","session_id")},
              "registration":runtime.registration if runtime else "direct-user-operation",
              "project_id":store.paths.project_id}
    key=hashlib.sha256(encoded(identity)).hexdigest()
    from backend.core.storage.maintenance import StorageLease
    lock=store.paths.project_root/"memory-restore-locks"/key;lock.mkdir(parents=True,exist_ok=True)
    with StorageLease(lock,exclusive=True):
        try:
            reference=store.get_state_ref(_NAMESPACE,key)
            record=json.loads(store.read_blob(reference,repair=False)) if reference else None
            if args.get("candidate_ref") and args["candidate_ref"]!=reference:
                return _failure("FILE_CANDIDATE_STALE")
            if record is None:
                if args.get("lesson_feedback_id"):return _failure("FILE_CANDIDATE_STALE")
                record=_project(backup,workspace,timestamp);record["identity"]=identity
                reference=store.put_blob(encoded(record),media_type="application/vnd.gitgo.memory-restore+json")
                if not store.compare_state_ref(_NAMESPACE,key,reference,expected_ref=None):
                    return _failure("FILE_CANDIDATE_STALE")
            if record.get("identity")!=identity or record.get("version")!=1:
                return _failure("FILE_CANDIDATE_STALE")
            def current():return encoded(capture_sources(workspace))==encoded(record["before"])
            if not current():return {**_failure("FILE_CHANGED"),"candidate_ref":reference}
            members=[]
            for row in record["members"]:
                first=base64.b64decode(row["before"],validate=True);last=base64.b64decode(row["after"],validate=True)
                view=row["text_view_available"]
                members.append(FileMutation(row["operation"],workspace,workspace/row["path"],row["before_sha256"],
                    first.decode("utf8") if view else "",last.decode("utf8") if view else "",
                    after_bytes=last,text_view_available=view))
        except StorageCommitUnknown:
            return _failure("STORAGE_COMMIT_UNKNOWN")  # No formal file commit was attempted.
        except (OSError,ValueError,KeyError,TypeError,MemoryArchiveError) as exc:
            return _failure("MEMORY_ARCHIVE_UNAVAILABLE",type(exc).__name__+": "+str(exc))
        # Formal effects begin here. Exceptions cannot be relabelled as a failed read.
        try:
            if not members:
                result={"effect_state":"not_committed","execution_state":"completed","file_results":[],"changed":False}
            else:
                args["_mutation_lock_root"]=str(store.paths.state_home/"mutation-locks")
                args.setdefault("_lesson_context",{"project_name":project_name})
                result=commit_file_batch(FileMutationBatch(workspace,tuple(members),operation="memory_restore"),args,
                    check_versions=current,guard_paths=[workspace/name for archive in record["before"].values() for name in archive["files"]])
        except Exception as exc:
            return {**_failure("FILE_MUTATION_FAILED",type(exc).__name__),"effect_state":"ambiguous","execution_state":"unknown","candidate_ref":reference}
        result.update(candidate_ref=reference,snapshot_sources=record["snapshots"])
        for action in result.get("next_actions",[]):
            action.update(action="memory_restore",candidate_ref=reference)
        if not result.get("error"):
            result["restored"]=record["sources"]
            try:store.release_state_ref(_NAMESPACE,key,expected_ref=reference)
            except Exception as exc:result.setdefault("warnings",[]).append({"code":"MEMORY_RESTORE_RETIRE_PENDING","error_type":type(exc).__name__})
        return result
