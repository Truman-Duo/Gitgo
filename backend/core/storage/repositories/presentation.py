"""Presentation repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
from backend.core.loop.provider_identity import recorded_model_id
from ..models import StorageReferenceMissing
from .ports import PresentationPort
from ..support import _utc_now, _estimate_durable_session_tokens, _durable_context_breakdown, _durable_cache_summary

def _is_public_user_message(message: dict) -> bool:
    """Return whether a provider-visible user message belongs in the UI chat.

    AgentSession is the provider transcript, not the product transcript.  It
    also contains governance nudges, recovery continuations and host steering.
    Keeping that distinction here prevents the Dashboard from accidentally
    rendering internal control traffic as if the user had typed it.
    """
    if str(message.get("role") or "") != "user":
        return False
    message_type = str(message.get("message_type") or "")
    if message_type and message_type != "conversation":
        return False
    content = str(message.get("content") or "")
    internal_prefixes = (
        "[HOST ", "[USER DECISION ", "[GOVERNANCE ", "[\u6cbb\u7406\u5efa\u8bae]",
        "[\u7cfb\u7edf\u63d0\u793a]", "[\u7cfb\u7edf]",
    )
    return bool(content.strip()) and not content.lstrip().startswith(internal_prefixes)


def _storage_projection_warning(
    exc: StorageReferenceMissing, *, turn_id: str = "",
) -> dict:
    """Visible placeholder for damaged history without inventing content."""
    return {
        "message_id": f"storage-warning:{exc.ref}",
        "turn_id": turn_id,
        "role": "assistant",
        "content": (
            "[Error: [STORAGE_CAS_REFERENCE_MISSING] Part of this historical "
            "record is unavailable. Current project data was not replaced; "
            "restore a verified backup to recover the missing detail.]"
        ),
        "timestamp": "",
        "final": True,
        "kind": "error",
        "visibility": "public",
        "status": "degraded",
        "storage_ref": exc.ref,
    }


def _public_session_messages(self: PresentationPort, session_id: str) -> list[dict]:
    """Project one durable provider session into the canonical public chat.

    User turns come from the message journal.  Assistant turns come from the
    terminal TaskOutcome, which is the sole completion authority.  Intermediate
    provider prose remains available in traces/verbose mode but is deliberately
    excluded here, avoiding the former duplicate-final-answer race.
    """
    with self._state_lock:
        message_rows = self._state.execute(
            """SELECT message_id, task_id, sequence, content_ref, created_at
               FROM messages WHERE session_id=? ORDER BY sequence""",
            (session_id,),
        ).fetchall()
        task_rows = self._state.execute(
            """SELECT task_id, outcome_ref, created_at, updated_at
               FROM tasks WHERE session_id=? ORDER BY created_at, task_id""",
            (session_id,),
        ).fetchall()
        session_row = self._state.execute(
            "SELECT metadata_ref FROM sessions WHERE session_id=?", (session_id,),
        ).fetchone()

    messages_by_task: dict[str, list[dict]] = {}
    unscoped: list[dict] = []
    for row in message_rows:
        try:
            loaded = self._read_json_blob(str(row["content_ref"]))
        except StorageReferenceMissing as exc:
            warning = self._storage_projection_warning(
                exc, turn_id=str(row["task_id"] or ""),
            )
            task_id = str(row["task_id"] or "")
            if task_id:
                messages_by_task.setdefault(task_id, []).append(warning)
            else:
                unscoped.append(warning)
            continue
        if not isinstance(loaded, dict) or not self._is_public_user_message(loaded):
            continue
        projected = {
            "message_id": str(row["message_id"]),
            "turn_id": str(row["task_id"] or ""),
            "role": "user",
            "content": str(loaded.get("content") or ""),
            "timestamp": str(loaded.get("timestamp") or row["created_at"] or ""),
            "final": True,
            "kind": "conversation",
            "visibility": "public",
            "sequence": int(row["sequence"]),
        }
        task_id = str(row["task_id"] or "")
        if task_id:
            messages_by_task.setdefault(task_id, []).append(projected)
        else:
            unscoped.append(projected)

    if session_row is not None and session_row["metadata_ref"]:
        try:
            metadata = self._read_json_blob(str(session_row["metadata_ref"]))
            if isinstance(metadata, dict):
                ledger_ref = metadata.get("host_ledger_ref")
                try:
                    ledger = (
                        self._read_json_blob(str(ledger_ref))
                        if ledger_ref else metadata.get("host_ledger", [])
                    )
                except StorageReferenceMissing as exc:
                    unscoped.append(self._storage_projection_warning(exc))
                    ledger = []
                from backend.core.loop.decision_timeline import decision_timeline
                for card in decision_timeline(ledger if isinstance(ledger, list) else []):
                    messages_by_task.setdefault(card["turn_id"], []).append(card)
        except StorageReferenceMissing as exc:
            unscoped.append(self._storage_projection_warning(exc))
    public = list(unscoped)
    known_tasks: set[str] = set()
    for row in task_rows:
        task_id = str(row["task_id"])
        known_tasks.add(task_id)
        public.extend(sorted(messages_by_task.get(task_id, []), key=lambda item: item.get("sequence", 0)))
        outcome_ref = str(row["outcome_ref"] or "")
        if not outcome_ref:
            continue
        try:
            loaded = self._read_json_blob(outcome_ref)
        except StorageReferenceMissing as exc:
            public.append(self._storage_projection_warning(exc, turn_id=task_id))
            continue
        if not isinstance(loaded, dict):
            continue
        response = str(loaded.get("response") or "").strip()
        status = str(loaded.get("status") or "")
        if status == "awaiting_user":
            # The durable question card is the public projection; the
            # formatted control response would duplicate all its options.
            if any(item.get("kind") == "decision" for item in messages_by_task.get(task_id, [])):
                continue
        failed = status in {"failed", "cancelled", "timed_out", "degraded"}
        if failed:
            error = loaded.get("error") or {}
            code = str(error.get("code") or "AGENT_TASK_FAILED")
            detail = str(error.get("message") or f"Agent task ended as {status}")
            report = f"[Error: [{code}] {detail}]"
            response = f"{report}\n\n{response}" if status == "degraded" and response else report
        if not response:
            continue
        public.append({
            "message_id": f"outcome:{task_id}",
            "turn_id": task_id,
            "role": "assistant",
            "content": response,
            "timestamp": str(row["updated_at"] or row["created_at"] or ""),
            "final": True,
            "kind": "error" if failed else "outcome",
            "visibility": "public",
            "status": str(loaded.get("status") or ""),
            "duration_ms": float(loaded.get("duration_ms", 0.0) or 0.0),
            "trace_id": str((loaded.get("metadata") or {}).get("trace_id") or task_id),
            "process_id": str(loaded.get("process_id") or ""),
        })
    for task_id, items in messages_by_task.items():
        if task_id not in known_tasks:
            public.extend(items)
    return public


def read_latest_conversations(self, *, agent_conversations: dict | None = None) -> dict:
    """Return the latest durable root conversation and its Agent tree.

    This is a read model: completed processes are not resurrected in APM and
    the daemon does not need to be started merely to display chat history.
    """
    self._ensure_open()
    with self._state_lock:
        root = self._state.execute(
            """SELECT process_id, session_id FROM session_processes
               WHERE parent_process_id IS NULL
               ORDER BY updated_at DESC, created_at DESC LIMIT 1"""
        ).fetchone()
        if root is None:
            return {"main_conversation": [], "agent_conversations": {},
                    "conversation_process_id": "", "session_id": ""}
        root_id = str(root["process_id"])
        rows = self._state.execute(
            """WITH RECURSIVE tree(process_id, session_id) AS (
                   SELECT process_id, session_id FROM session_processes
                    WHERE process_id=?
                   UNION ALL
                   SELECT child.process_id, child.session_id
                     FROM session_processes child
                     JOIN tree parent ON child.parent_process_id=parent.process_id
               ) SELECT process_id, session_id FROM tree""",
            (root_id,),
        ).fetchall()

    conversations = {
        str(row["process_id"]): (
            agent_conversations[str(row["process_id"])]
            if agent_conversations is not None and str(row["process_id"]) in agent_conversations
            else self._public_session_messages(str(row["session_id"]))
        )
        for row in rows
    }
    session_id = str(root["session_id"])
    main_conversation = list(conversations.get(root_id, []))
    # B questions remain authoritative in the B session ledger.  A gets a
    # compact read projection so it can supervise without duplicating the
    # full option payload or becoming a manual message relay.
    question_summaries = []
    for process_id, messages in conversations.items():
        if process_id == root_id:
            continue
        for message in messages:
            decision = message.get("decision")
            if not isinstance(decision, dict):
                continue
            source = str(decision.get("source_display_name") or "Subprocess")
            question = str(decision.get("question") or message.get("content") or "")
            answer = str(message.get("decision_answer") or "")
            content = f"{source} asked: {question}"
            if answer:
                content += f" · answered: {answer}"
            question_summaries.append({
                "message_id": f"child-question:{decision.get('decision_id')}",
                "turn_id": str(message.get("turn_id") or ""),
                "role": "assistant",
                "content": content,
                "timestamp": str(message.get("timestamp") or ""),
                "final": True,
                "kind": "agent_question",
                "visibility": "public",
                "status": str(message.get("status") or "awaiting_user"),
                "process_id": process_id,
            })
    main_conversation.extend(question_summaries)
    return {
        "main_conversation": main_conversation,
        "agent_conversations": conversations,
        "conversation_process_id": root_id,
        "session_id": session_id,
    }


def read_process_presentation(self: PresentationPort, process_id: str) -> dict:
    self._ensure_open()
    with self._state_lock:
        row = self._state.execute(
            "SELECT display_name, archived FROM process_presentation WHERE process_id=?",
            (process_id,),
        ).fetchone()
    if row is None:
        return {}
    return {**({"display_name": row["display_name"]} if row["display_name"] else {}),
            "archived": bool(row["archived"])}


def read_process_presentations(self) -> dict[str, dict]:
    self._ensure_open()
    with self._state_lock:
        rows = self._state.execute("SELECT process_id, display_name, archived FROM process_presentation").fetchall()
    return {str(row["process_id"]): {
        **({"display_name": row["display_name"]} if row["display_name"] else {}),
        "archived": bool(row["archived"]),
    } for row in rows}


def update_process_presentation(self: PresentationPort, process_id: str, *,
                                display_name: str | None = None,
                                archived: bool | None = None) -> dict:
    """Small independent fields: checkpoints never overwrite user choices.

    Each update changes only the requested column in one transaction, so
    concurrent hosts renaming/archiving cannot clobber one another.
    """
    self._ensure_open()
    if (display_name is None) == (archived is None):
        raise ValueError("exactly one presentation field is required")
    column = "display_name" if display_name is not None else "archived"
    value = display_name if display_name is not None else int(bool(archived))
    self._reserve_authoritative_write(512 + len(str(value)))
    with self._state_lock:
        self._state.execute("BEGIN IMMEDIATE")
        try:
            process = self._state.execute(
                "SELECT actor_kind, parent_process_id FROM session_processes WHERE process_id=?",
                (process_id,),
            ).fetchone()
            if process is None:
                raise ValueError("PROCESS_NOT_FOUND")
            if process["actor_kind"] == "supervisor" or not (
                process["parent_process_id"] or process["actor_kind"] in ("worker", "reviewer")
            ):
                raise ValueError("PROCESS_NOT_B")
            self._state.execute(
                f"""INSERT INTO process_presentation(process_id, {column}, updated_at)
                    VALUES(?, ?, ?) ON CONFLICT(process_id) DO UPDATE SET
                    {column}=excluded.{column}, updated_at=excluded.updated_at
                    WHERE process_presentation.{column} IS NOT excluded.{column}""",
                (process_id, value, _utc_now()),
            )
            self._state.execute("COMMIT")
        except BaseException:
            self._state.execute("ROLLBACK")
            raise
    return {"process_id": process_id, **self.read_process_presentation(process_id)}


def read_project_b_processes(self) -> dict[str, dict]:
    """Return every durable B process for this project, across all tasks.

    A supervisor is a task execution lease behind the project Chat and is
    deliberately not part of the user-facing ProcessList. Legacy child rows
    without actor_kind remain identifiable by their parent_process_id.
    """
    self._ensure_open()
    with self._state_lock:
        rows = self._state.execute(
            """SELECT process_id, session_id, task_id, parent_process_id,
                      role, actor_kind, capability_profile_id, status,
                      steps_used, max_steps, runtime_state_ref, created_at,
                      updated_at,
                      (SELECT metadata_ref FROM sessions s WHERE s.session_id=session_processes.session_id) AS metadata_ref
               FROM session_processes
               WHERE COALESCE(actor_kind, '') != 'supervisor'
                 AND (parent_process_id IS NOT NULL OR actor_kind IN ('worker', 'reviewer'))
               ORDER BY created_at, process_id"""
        ).fetchall()
        dependency_rows = self._state.execute(
            """SELECT process_id, depends_on_process_id
               FROM process_dependencies ORDER BY created_at, depends_on_process_id"""
        ).fetchall()

    dependencies: dict[str, list[str]] = {}
    for dependency in dependency_rows:
        dependencies.setdefault(str(dependency["process_id"]), []).append(
            str(dependency["depends_on_process_id"]),
        )

    presentations = self.read_process_presentations()
    processes: dict[str, dict] = {}
    for row in rows:
        process_id = str(row["process_id"])
        runtime: dict = {}
        storage_error: StorageReferenceMissing | None = None
        if row["runtime_state_ref"]:
            try:
                loaded = self._read_json_blob(str(row["runtime_state_ref"]))
                if isinstance(loaded, dict):
                    runtime = dict(loaded)
            except StorageReferenceMissing as exc:
                storage_error = exc
        durable_state = None
        if not runtime.get("estimated_tokens") or not runtime.get("context"):
            try:
                durable_state = {"session": self.load_agent_session_state(process_id)}
            except StorageReferenceMissing as exc:
                storage_error = storage_error or exc
        durable_session = dict((durable_state or {}).get("session") or {})
        estimated_tokens = int(runtime.get("estimated_tokens", 0) or 0)
        if estimated_tokens <= 0 and durable_session:
            estimated_tokens = _estimate_durable_session_tokens(durable_session)
        context = dict(runtime.get("context") or {})
        if not context and durable_session:
            # load_agent_session_state returns durable metadata flattened
            # beside messages/provider_state, not under session_metadata.
            metadata = dict(durable_session.get("session_metadata") or durable_session)
            context = {
                "estimated_tokens": estimated_tokens,
                "limit": int(metadata.get("model_context_limit", 0) or 0),
                "epoch": int(metadata.get("context_epoch", 0) or 0),
            }
        if durable_session and not dict(context.get("breakdown") or {}):
            context["breakdown"] = _durable_context_breakdown(durable_session)
        cache_summary = dict(runtime.get("cache_summary") or {})
        if not cache_summary and durable_session:
            cache_summary = _durable_cache_summary(durable_session)
        owner = str(row["parent_process_id"] or "")
        depends_on = dependencies.get(process_id, [])
        worktree = dict(runtime.get("worktree") or {})
        worktree_path = str(
            runtime.get("worktree_path") or worktree.get("path") or ""
        )
        context_snapshot = dict(runtime.get("context_snapshot") or {})
        from backend.core.loop.coordination import coordination_summary_from_context
        display_name = ""
        identity_ledger = []
        if row["metadata_ref"]:
            try:
                loaded_metadata = self._read_json_blob(str(row["metadata_ref"]))
                if isinstance(loaded_metadata, dict):
                    display_name = str(loaded_metadata.get("display_name") or "")
                    if not runtime.get("model_id") or runtime.get("model_id") == "unknown":
                        ref = loaded_metadata.get("host_ledger_ref")
                        identity_ledger = self._read_json_blob(str(ref)) if ref else loaded_metadata.get("host_ledger", [])
            except StorageReferenceMissing as exc:
                storage_error = storage_error or exc
        processes[process_id] = {
            "process_id": process_id,
            "session_id": str(row["session_id"]),
            "display_name": display_name,
            "role": str(row["role"] or ""),
            "ring_level": int(runtime.get("ring_level", 3) or 3),
            "status": str(row["status"] or ""),
            "steps_used": int(row["steps_used"] or 0),
            "max_steps": int(row["max_steps"] or 0),
            "parent_id": owner or None,
            "parent_ids": list(dict.fromkeys(([owner] if owner else []) + depends_on)),
            "depends_on": depends_on,
            "downstream_ids": [],
            "child_ids": list(runtime.get("child_ids") or []),
            "child_contracts": dict(runtime.get("delegated_contracts") or {}),
            "child_reviews": dict(runtime.get("child_reviews") or {}),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "worktree_path": worktree_path,
            "worktree": {
                **worktree,
                "path": str(worktree.get("path") or worktree_path),
                "isolated": bool(worktree.get("isolated", False)),
                "state": str(
                    worktree.get("state")
                    or ("isolated" if worktree.get("isolated") else "shared_workspace")
                ),
            },
            "provider_id": str(runtime.get("provider_id") or ""),
            "model_id": recorded_model_id(runtime.get("model_id"), identity_ledger),
            "estimated_tokens": estimated_tokens,
            "active_task_id": str(row["task_id"] or ""),
            "task_kind": str(runtime.get("task_kind") or ""),
            "actor_kind": str(row["actor_kind"] or "worker"),
            "capability_profile_id": str(row["capability_profile_id"] or ""),
            "relationship_policy": dict(
                context_snapshot.get("relationship_policy") or {}
            ),
            "coordination": coordination_summary_from_context(context_snapshot),
            "pending_decision": runtime.get("pending_decision"),
            "recovery": (
                {
                    "state": "storage_reference_missing",
                    "code": StorageReferenceMissing.code,
                    "message": str(storage_error),
                    "ref": storage_error.ref,
                }
                if storage_error else runtime.get("recovery")
            ),
            "storage_error": (
                {
                    "code": StorageReferenceMissing.code,
                    "message": str(storage_error),
                    "ref": storage_error.ref,
                }
                if storage_error else None
            ),
            "mailbox": runtime.get("mailbox"),
            "task_budget": runtime.get("task_budget"),
            "context": context or None,
            "cache_summary": cache_summary,
            "durable": True,
            **presentations.get(process_id, {}),
        }

    for process_id, process in processes.items():
        process["downstream_ids"] = [
            candidate_id
            for candidate_id, candidate in processes.items()
            if process_id in candidate.get("depends_on", [])
        ]
    return processes


def read_latest_root_process(self) -> dict | None:
    """Return the latest durable A control projection without starting a Daemon.

    The project ProcessList still consumes ``read_project_b_processes`` and
    therefore excludes A.  This minimal root projection exists for the Chat
    controller: a pending decision, recovery state, context meter, and A
    status must survive a cold Dashboard/Daemon restart.
    """
    self._ensure_open()
    # Displaying a control snapshot must not invoke recovery, which hydrates
    # every provider continuation, historical message and receipt.
    with self._state_lock:
        selected = self._state.execute(
            """SELECT sp.*, s.metadata_ref, t.contract_ref FROM session_processes sp
               LEFT JOIN sessions s ON s.session_id=sp.session_id
               LEFT JOIN tasks t ON t.task_id=sp.task_id
               WHERE sp.parent_process_id IS NULL
               ORDER BY sp.updated_at DESC, sp.created_at DESC LIMIT 1"""
        ).fetchone()
    if selected is None:
        return None
    row = dict(selected)
    runtime = dict(self._read_json_blob(str(row["runtime_state_ref"])) or {}) if row.get("runtime_state_ref") else {}
    metadata = dict(self._read_json_blob(str(row["metadata_ref"])) or {}) if row.get("metadata_ref") else {}
    contract = dict(self._read_json_blob(str(row["contract_ref"])) or {}) if row.get("contract_ref") else {}
    durable_session = metadata
    if not runtime.get("context") or not runtime.get("estimated_tokens"):
        durable_session = dict(self.load_agent_session_state(str(row["process_id"])) or metadata)
    identity_ledger = []
    if not runtime.get("model_id") or runtime.get("model_id") == "unknown":
        ref = metadata.get("host_ledger_ref")
        identity_ledger = self._read_json_blob(str(ref)) if ref else metadata.get("host_ledger", [])
    estimated_tokens = int(runtime.get("estimated_tokens", 0) or 0)
    if estimated_tokens <= 0 and durable_session:
        estimated_tokens = _estimate_durable_session_tokens(durable_session)
    context = dict(runtime.get("context") or {})
    if not context and durable_session:
        metadata = dict(durable_session.get("session_metadata") or durable_session)
        context = {
            "estimated_tokens": estimated_tokens,
            "limit": int(metadata.get("model_context_limit", 0) or 0),
            "epoch": int(metadata.get("context_epoch", 0) or 0),
        }
    if durable_session and not dict(context.get("breakdown") or {}):
        context["breakdown"] = _durable_context_breakdown(durable_session)
    cache_summary = dict(runtime.get("cache_summary") or {})
    if not cache_summary and durable_session:
        cache_summary = _durable_cache_summary(durable_session)
    process_id = str(row.get("process_id") or "")
    if not process_id:
        return None
    worktree = dict(runtime.get("worktree") or {})
    worktree_path = str(
        runtime.get("worktree_path") or worktree.get("path") or ""
    )
    context_snapshot = dict(runtime.get("context_snapshot") or {})
    from backend.core.loop.coordination import coordination_summary_from_context
    return {
        "process_id": process_id,
        "session_id": str(row.get("session_id") or ""),
        "role": str(row.get("role") or "supervisor"),
        "ring_level": int(runtime.get("ring_level", 0) or 0),
        "status": str(row.get("status") or ""),
        "steps_used": int(row.get("steps_used", 0) or 0),
        "max_steps": int(row.get("max_steps", 0) or 0),
        "parent_id": None,
        "parent_ids": [],
        "depends_on": [],
        "downstream_ids": [],
        "child_ids": list(runtime.get("child_ids") or []),
        "child_contracts": dict(runtime.get("delegated_contracts") or {}),
        "child_reviews": dict(runtime.get("child_reviews") or {}),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "worktree_path": worktree_path,
        "worktree": {
            **worktree,
            "path": str(worktree.get("path") or worktree_path),
            "isolated": bool(worktree.get("isolated", False)),
            "state": str(
                worktree.get("state")
                or ("isolated" if worktree.get("isolated") else "shared_workspace")
            ),
        },
        "provider_id": str(runtime.get("provider_id") or ""),
        "model_id": recorded_model_id(runtime.get("model_id"), identity_ledger),
        "estimated_tokens": estimated_tokens,
        "active_task_id": str(row.get("task_id") or ""),
        "task_kind": str(
            contract.get("task_kind") or runtime.get("task_kind") or "answer"
        ),
        "actor_kind": str(row.get("actor_kind") or "supervisor"),
        "capability_profile_id": str(
            row.get("capability_profile_id") or "supervisor.control"
        ),
        "relationship_policy": dict(
            context_snapshot.get("relationship_policy") or {}
        ),
        "coordination": coordination_summary_from_context(context_snapshot),
        "pending_decision": runtime.get("pending_decision"),
        "recovery": runtime.get("recovery"),
        "mailbox": runtime.get("mailbox"),
        "task_budget": runtime.get("task_budget"),
        "context": context or None,
        "cache_summary": cache_summary,
        "durable": True,
    }


def read_latest_root_status(self) -> dict | None:
    """Cheap project-list projection with no CAS/session materialisation.

    The Dashboard polls this for cold projects.  One indexed SQLite read is
    deliberately separate from ``read_latest_root_process`` so project-list
    refreshes cannot repeatedly decode the whole context/session graph.
    """
    self._ensure_open()
    with self._state_lock:
        row = self._state.execute(
            """SELECT process_id, status, updated_at
               FROM session_processes
               WHERE COALESCE(actor_kind, '') = 'supervisor'
                  OR parent_process_id IS NULL
               ORDER BY updated_at DESC, created_at DESC
               LIMIT 1"""
        ).fetchone()
    if row is None:
        return None
    return {
        "process_id": str(row["process_id"] or ""),
        "status": str(row["status"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def read_project_b_conversations(self) -> dict[str, list[dict]]:
    """Return public durable transcripts for every B in the project."""
    self._ensure_open()
    with self._state_lock:
        rows = self._state.execute(
            """SELECT process_id, session_id, updated_at FROM session_processes
               WHERE COALESCE(actor_kind, '') != 'supervisor'
                 AND (parent_process_id IS NOT NULL OR actor_kind IN ('worker', 'reviewer'))
               ORDER BY created_at, process_id"""
        ).fetchall()
    cache = getattr(self, "_b_conversation_cache", {})
    current_ids: set[str] = set()
    result: dict[str, list[dict]] = {}
    for row in rows:
        process_id = str(row["process_id"])
        current_ids.add(process_id)
        version = str(row["updated_at"] or "")
        cached = cache.get(process_id)
        if cached is None or cached[0] != version:
            cached = (
                version,
                self._public_session_messages(str(row["session_id"])),
            )
            cache[process_id] = cached
        result[process_id] = cached[1]
    self._b_conversation_cache = {
        process_id: cache[process_id] for process_id in current_ids
    }
    return result
