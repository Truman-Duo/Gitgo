"""Worktrees repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
from .ports import WorktreesPort
from ..support import _utc_now

def save_worktree_state(self: WorktreesPort, record: dict) -> None:
    """Persist one low-frequency Agent worktree lifecycle transition."""
    self._ensure_open()
    worktree_id = str(record.get("worktree_id") or "").strip()
    process_id = str(record.get("process_id") or "").strip()
    if not worktree_id or not process_id:
        raise ValueError("worktree_id and process_id are required")
    metadata = {
        key: record.get(key)
        for key in (
            "upstream_commits", "changed_files", "runtime_offsets", "error", "created_at",
            "updated_at", "isolated", "promoted", "promoted_at",
        )
    }
    descriptor = self._write_blob_file(
        self._encoded_object(metadata), media_type="application/json"
    )
    self._reserve_authoritative_write(2048)
    now = _utc_now()
    task_id = str(record.get("task_id") or "").strip()
    with self._state_lock:
        known_task = None
        if task_id:
            known_task = self._state.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        self._state.execute("BEGIN IMMEDIATE")
        try:
            self._register_object(self._state, descriptor)
            self._state.execute(
                """INSERT INTO worktrees(
                       worktree_id, project_id, task_id, path_hint, git_head,
                       state, created_at, updated_at, process_id, mode,
                       root_workspace_hint, base_commit, snapshot_commit,
                       result_commit, own_commit, metadata_ref
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(worktree_id) DO UPDATE SET
                       task_id=COALESCE(excluded.task_id, worktrees.task_id),
                       path_hint=excluded.path_hint,
                       git_head=excluded.git_head,
                       state=excluded.state,
                       updated_at=excluded.updated_at,
                       process_id=excluded.process_id,
                       mode=excluded.mode,
                       root_workspace_hint=excluded.root_workspace_hint,
                       base_commit=excluded.base_commit,
                       snapshot_commit=excluded.snapshot_commit,
                       result_commit=excluded.result_commit,
                       own_commit=excluded.own_commit,
                       metadata_ref=excluded.metadata_ref""",
                (
                    worktree_id, self.paths.project_id,
                    task_id if known_task is not None else None,
                    str(record.get("path") or ""),
                    str(record.get("result_commit") or record.get("snapshot_commit") or ""),
                    str(record.get("state") or "unknown"),
                    str(record.get("created_at") or now), now, process_id,
                    str(record.get("mode") or "write"),
                    str(record.get("root_workspace") or ""),
                    str(record.get("base_commit") or ""),
                    str(record.get("snapshot_commit") or ""),
                    str(record.get("result_commit") or ""),
                    str(record.get("own_commit") or ""), descriptor["ref"],
                ),
            )
            self._state.execute("COMMIT")
        except BaseException:
            self._state.execute("ROLLBACK")
            raise


def load_worktree_state(self: WorktreesPort, process_id: str) -> dict | None:
    self._ensure_open()
    with self._state_lock:
        row = self._state.execute(
            """SELECT worktree_id, process_id, task_id, path_hint, state,
                      mode, root_workspace_hint, base_commit, snapshot_commit,
                      result_commit, own_commit, metadata_ref, created_at, updated_at
               FROM worktrees WHERE process_id=?""",
            (process_id,),
        ).fetchone()
    if row is None:
        return None
    result = {key: row[key] for key in row.keys() if key != "metadata_ref"}
    result["path"] = result.pop("path_hint")
    result["root_workspace"] = result.pop("root_workspace_hint")
    if row["metadata_ref"]:
        loaded = self._read_json_blob(str(row["metadata_ref"]))
        if isinstance(loaded, dict):
            result.update(loaded)
    result["isolated"] = bool(result.get("path")) and result.get("state") != "disposed"
    return result


def list_worktree_states(self: WorktreesPort, *, active_only: bool = False) -> list[dict]:
    self._ensure_open()
    with self._state_lock:
        rows = self._state.execute(
            "SELECT process_id FROM worktrees ORDER BY created_at"
        ).fetchall()
    values = [self.load_worktree_state(str(row["process_id"])) for row in rows]
    result = [value for value in values if value is not None]
    if active_only:
        result = [value for value in result if value.get("state") != "disposed"]
    return result
