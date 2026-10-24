"""Checkpoints repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import hashlib
from copy import deepcopy
from dataclasses import asdict
from ..project_registration import ProjectRegistration
from ..host_records import require
from ..checkpoint_plan import (
    CheckpointObjectBuilder, capture_basis, require_live_basis,
)
from ..checkpoint_sql import CheckpointSqlBuilder
from ..checkpoint_work import CheckpointPublication
from ..checkpoint_limits import CheckpointLimits, capture_footprint
from ..lifecycle import require_revision, check_transition
from .ports import CheckpointsPort
from ..support import _utc_now
from ..transactions import authoritative_transaction

def plan_agent_checkpoint(
    self: CheckpointsPort, snapshot: dict, *, limits: CheckpointLimits | None = None,
    registration: ProjectRegistration | None = None,
) -> CheckpointPublication:
    """Build the complete immutable publication without new file/SQL writes.

    Optional limits are supplied by a future qualified backend, never inferred
    from a workspace or permission grant. Legacy callers retain their existing
    policy. Explicit first-project registration consumes the same input budget
    and State snapshot and joins publication; it cannot rewrite an existing
    logical project. The returned value is not an admission or file ticket.
    """
    self._ensure_open()
    self._state.require_available()
    if registration is not None:
        require(type(registration) is ProjectRegistration and type(limits) is CheckpointLimits,
                "explicit qualified project registration and input budget required")
        require(registration.project_id == self.paths.project_id, "registration differs from selected logical project")
        # The entire semantic request shares one input allowance. Metadata is
        # not a second uncharged channel around snapshot depth/byte/node limits.
        snapshot = limits.copy_snapshot({"snapshot":snapshot,"registration":asdict(registration)})["snapshot"]
    else:
        snapshot = deepcopy(snapshot) if limits is None else limits.copy_snapshot(snapshot)
    process_id = str(snapshot.get("process_id", "")).strip()
    session_id = str(snapshot.get("session_id", "")).strip()
    if not process_id or not session_id:
        raise ValueError("process_id and session_id are required")
    parent_process_id = str(snapshot.get("parent_id") or "")
    with self._state_lock:
        if self._state.in_transaction:
            raise RuntimeError("STATE_TRANSACTION_ALREADY_ACTIVE")
        footprint = None
        if limits is None:
            basis = capture_basis(self._state, process_id, parent_process_id)
        else:
            # Bound retained history before queries that scan it. Both reads
            # observe the same snapshot; an independent writer cannot expand
            # history between the bounded count and dependency capture. This
            # read-only transaction is part of the qualified attempt schedule.
            with authoritative_transaction(self._state):
                footprint = capture_footprint(self._state, limits, process_id, session_id)
                basis = (capture_basis(self._state, process_id, parent_process_id)
                         if registration is None else
                         capture_basis(self._state, process_id, parent_process_id, registration=registration))
    require_live_basis(basis)
    require_revision(basis.revision, snapshot.get("expected_revision"))
    status = str(snapshot.get("status") or "running")
    prior_runtime = {}
    if basis.status == "completed" and status != basis.status and basis.runtime_ref:
        prior_runtime = self._read_json_blob(basis.runtime_ref, repair=False)
    check_transition(basis.status, snapshot, prior_runtime)
    previous_revision = basis.revision
    committed_revision = previous_revision + 1
    checkpoint_sequence = basis.event_sequence
    now = _utc_now()
    builder = CheckpointObjectBuilder(self._encoded_object, self.policy, now)
    prepare = builder.prepare
    messages = list(snapshot.get("messages") or [])
    provider_state = dict(snapshot.get("provider_state") or {})
    session_metadata = dict(snapshot.get("session_metadata") or {})

    provider_objects = {
        str(state_id): prepare(state)
        for state_id, state in provider_state.items()
    }
    session_metadata["provider_state_refs"] = {
        state_id: descriptor["ref"]
        for state_id, descriptor in provider_objects.items()
    }
    for field_name in (
        "context_memo", "host_ledger", "provider_usage",
        "cache_telemetry", "epoch_archive",
    ):
        if field_name in session_metadata:
            descriptor = prepare(session_metadata.pop(field_name))
            session_metadata[f"{field_name}_ref"] = descriptor["ref"]
    metadata_object = prepare(session_metadata)
    runtime_object = prepare(dict(snapshot.get("runtime_state") or {}))
    message_objects: list[tuple[int, dict, dict, dict | None]] = []
    for sequence, raw_message in enumerate(messages):
        message = dict(raw_message or {})
        message_object = prepare(message)
        state_id = str(message.get("provider_state_id", ""))
        reasoning_object = provider_objects.get(state_id)
        message_objects.append((sequence, message, message_object, reasoning_object))

    task_id = str(snapshot.get("task_id", "")).strip()
    contract_object = None
    outcome_object = None
    if task_id:
        contract_object = prepare({
            "task_description": snapshot.get("task_description", ""),
            "task_kind": snapshot.get("task_kind", ""),
            "required_test_ids": list(snapshot.get("required_test_ids") or []),
            "capability_profile_id": snapshot.get("capability_profile_id", ""),
        })
        if snapshot.get("result") is not None:
            outcome_object = prepare(snapshot.get("result"))

    usage_values = None
    raw_outcome = snapshot.get("result")
    if task_id and isinstance(raw_outcome, dict):
        metadata = dict(raw_outcome.get("metadata") or {})
        budget = dict(metadata.get("task_tree_budget") or {})
        used = dict(budget.get("used") or {})
        usage_values = {
            "task_kind": str(metadata.get("task_kind") or snapshot.get("task_kind") or ""),
            "status": str(raw_outcome.get("status") or snapshot.get("status") or ""),
            "provider_calls": int(used.get("provider_calls", 0) or 0),
            "input_tokens": int(used.get("reported_input_tokens", 0) or 0),
            "output_tokens": int(used.get("reported_output_tokens", 0) or 0),
            "cache_read_tokens": int(used.get("reported_cache_read_tokens", 0) or 0),
            "cache_write_tokens": int(used.get("reported_cache_write_tokens", 0) or 0),
            "duration_ms": float(raw_outcome.get("duration_ms", 0.0) or 0.0),
            "tool_calls": int(raw_outcome.get("tool_calls_executed", 0) or 0),
        }

    receipt_objects: list[tuple[dict, dict]] = []
    for raw_receipt in list(snapshot.get("tool_receipts") or []):
        receipt = dict(raw_receipt or {})
        if task_id and receipt.get("receipt_id"):
            receipt_objects.append((receipt, prepare(receipt)))

    checkpoint_at = str(snapshot.get("checkpoint_at") or now)
    dependencies = list(dict.fromkeys(
        str(item) for item in (snapshot.get("depends_on") or []) if str(item)
    ))
    if process_id in dependencies:
        raise ValueError("process cannot depend on itself")
    task_status = str((snapshot.get("result") or {}).get("status") or status)
    fact = prepare({"process_status": status,
        "task_status": task_status,
        "revision": committed_revision, "task_id": task_id,
        "checkpoint": f"sqlite:session/{process_id}/{checkpoint_sequence}",
        "runtime_state_ref": runtime_object["ref"],
        "session_metadata_ref": metadata_object["ref"],
        "message_refs": [item[2]["ref"] for item in message_objects],
        "contract_ref": contract_object["ref"] if contract_object else "",
        "outcome_ref": outcome_object["ref"] if outcome_object else "",
        "receipt_refs": [item[1]["ref"] for item in receipt_objects]})
    from backend.core.loop.lifecycle import TERMINAL
    barrier = None
    if (status in TERMINAL
            and (snapshot.get("runtime_state") or {}).get("lifecycle_finalized") is True
            and basis.terminal is None):
        barrier = prepare({"status": status, "task_status": task_status,
            "task_id": task_id, "state_revision": committed_revision,
            "steps_used": snapshot.get("steps_used", 0)})
    objects = builder.seal()
    descriptors = {member.digest: member.descriptor for member in objects.members}
    logical_estimate = 2048 + 512 * (
        len(message_objects) + len(receipt_objects) + len(descriptors)
    )
    publication = CheckpointSqlBuilder()
    if basis.registration is not None:
        basis.registration.append(publication)
    for descriptor in descriptors.values():
        self._register_object(publication, descriptor)
    # A checkpoint is one ownership unit.  Record the complete CAS
    # closure in the same transaction as the authoritative rows.
    # Historical facts keep their own closure when this one advances.
    # This reference index does not fence legacy concurrent collection.
    publication.execute(
        "DELETE FROM object_refs WHERE owner_type=? AND owner_id=?",
        ("session_checkpoint", session_id),
    )
    publication.executemany(
        """INSERT INTO object_refs(
               owner_type, owner_id, field_name, digest, created_at
           ) VALUES(?, ?, ?, ?, ?)""",
        [
            (
                "session_checkpoint", session_id,
                f"object:{digest}", digest, now,
            )
            for digest in sorted(descriptors)
        ],
    )
    publication.execute(
        """INSERT INTO sessions(
               session_id, project_id, status, context_epoch,
               created_at, updated_at, metadata_ref, checkpoint_at,
               checkpoint_sequence
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               status=excluded.status,
               context_epoch=excluded.context_epoch,
               updated_at=excluded.updated_at,
               metadata_ref=excluded.metadata_ref,
               checkpoint_at=excluded.checkpoint_at,
               checkpoint_sequence=excluded.checkpoint_sequence""",
        (
            session_id, self.paths.project_id, status,
            int(session_metadata.get("context_epoch", 0) or 0),
            str(snapshot.get("session_created_at") or now), now,
            metadata_object["ref"], checkpoint_at, checkpoint_sequence,
        ),
    )
    publication.execute(
        "DELETE FROM process_dependencies WHERE process_id=?",
        (process_id,),
    )
    for upstream_id in dependencies:
        publication.execute(
            """INSERT INTO process_dependencies(
                   process_id, depends_on_process_id, state,
                   created_at, updated_at
               ) VALUES(?, ?, 'declared', ?, ?)""",
            (process_id, upstream_id, now, now),
        )
    if task_id:
        parent_task_id = basis.parent_task_id
        publication.execute(
            """INSERT INTO tasks(
                   task_id, session_id, parent_task_id, role, status,
                   contract_ref, outcome_ref, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    parent_task_id=excluded.parent_task_id,
                    role=excluded.role,
                   status=excluded.status,
                   contract_ref=excluded.contract_ref,
                   outcome_ref=excluded.outcome_ref,
                   updated_at=excluded.updated_at""",
            (
                task_id, session_id, parent_task_id,
                str(snapshot.get("role") or ""),
                str((snapshot.get("result") or {}).get("status") or status), contract_object["ref"] if contract_object else None,
                outcome_object["ref"] if outcome_object else None,
                str(snapshot.get("created_at") or now), now,
            ),
        )
        if usage_values is not None:
            publication.execute(
                """INSERT INTO task_usage(
                       task_id, process_id, parent_process_id, task_kind,
                       status, provider_calls, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, duration_ms,
                       tool_calls, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       process_id=excluded.process_id,
                       parent_process_id=excluded.parent_process_id,
                       task_kind=excluded.task_kind,
                       status=excluded.status,
                       provider_calls=excluded.provider_calls,
                       input_tokens=excluded.input_tokens,
                       output_tokens=excluded.output_tokens,
                       cache_read_tokens=excluded.cache_read_tokens,
                       cache_write_tokens=excluded.cache_write_tokens,
                       duration_ms=excluded.duration_ms,
                       tool_calls=excluded.tool_calls,
                       updated_at=excluded.updated_at""",
                (
                    task_id, process_id,
                    str(snapshot.get("parent_id") or ""),
                    usage_values["task_kind"], usage_values["status"],
                    usage_values["provider_calls"], usage_values["input_tokens"],
                    usage_values["output_tokens"], usage_values["cache_read_tokens"],
                    usage_values["cache_write_tokens"], usage_values["duration_ms"],
                    usage_values["tool_calls"],
                    str(snapshot.get("created_at") or now), now,
                ),
            )
    publication.execute(
        """INSERT INTO session_processes(
               process_id, session_id, task_id, parent_process_id,
               role, actor_kind, capability_profile_id, status,
               steps_used, max_steps, checkpoint_event_sequence,
               created_at, updated_at, runtime_state_ref
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(process_id) DO UPDATE SET
               session_id=excluded.session_id,
               task_id=excluded.task_id,
               parent_process_id=excluded.parent_process_id,
               role=excluded.role,
               actor_kind=excluded.actor_kind,
               capability_profile_id=excluded.capability_profile_id,
               status=excluded.status,
               steps_used=excluded.steps_used,
               max_steps=excluded.max_steps,
               checkpoint_event_sequence=excluded.checkpoint_event_sequence,
               runtime_state_ref=excluded.runtime_state_ref,
               updated_at=excluded.updated_at""",
        (
            process_id, session_id, task_id or None,
            str(snapshot.get("parent_id") or "") or None,
            str(snapshot.get("role") or ""),
            str(snapshot.get("actor_kind") or ""),
            str(snapshot.get("capability_profile_id") or ""),
            status, int(snapshot.get("steps_used", 0) or 0),
            int(snapshot.get("max_steps", 0) or 0), checkpoint_sequence,
            str(snapshot.get("created_at") or now), now,
            runtime_object["ref"],
        ),
    )
    # Provider annotations can change the CAS reference of an old message.
    # Preserve its turn when the actual message remains the same, rather than
    # moving retained user history into the currently executing task.
    with self._state_lock:
        prior_messages = {int(row["sequence"]): dict(row) for row in self._state.execute(
            "SELECT sequence, task_id, content_ref FROM messages WHERE session_id=?", (session_id,))}
    for sequence, message, message_object, reasoning_object in message_objects:
        message_task_id = task_id
        prior = prior_messages.get(sequence)
        if prior and prior["task_id"] and prior["content_ref"] != message_object["ref"]:
            old_message = self._read_json_blob(str(prior["content_ref"]))
            if isinstance(old_message, dict) and all(
                old_message.get(key) == message.get(key) for key in ("role", "content")
            ):
                message_task_id = str(prior["task_id"])
        message_id = hashlib.sha256(
            f"{session_id}:{sequence}".encode("utf-8")
        ).hexdigest()
        publication.execute(
            """INSERT INTO messages(
                   message_id, session_id, task_id, sequence, role,
                   content_ref, reasoning_ref, created_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, sequence) DO UPDATE SET
                   task_id=CASE
                       WHEN messages.content_ref=excluded.content_ref
                       THEN messages.task_id ELSE excluded.task_id END,
                   role=excluded.role,
                   content_ref=excluded.content_ref,
                   reasoning_ref=excluded.reasoning_ref
               WHERE messages.role IS NOT excluded.role
                  OR messages.content_ref IS NOT excluded.content_ref
                  OR messages.reasoning_ref IS NOT excluded.reasoning_ref""",
            (
                message_id, session_id, message_task_id or None, sequence,
                str(message.get("role") or ""), message_object["ref"],
                reasoning_object["ref"] if reasoning_object else None, now,
            ),
        )
    publication.execute(
        "DELETE FROM messages WHERE session_id=? AND sequence>=?",
        (session_id, len(message_objects)),
    )
    for receipt, receipt_object in receipt_objects:
        publication.execute(
            """INSERT INTO receipts(
                   receipt_id, task_id, receipt_type, evidence_ref, created_at
               ) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(receipt_id) DO UPDATE SET
                   task_id=excluded.task_id,
                   receipt_type=excluded.receipt_type,
                   evidence_ref=excluded.evidence_ref""",
            (
                str(receipt["receipt_id"]), task_id,
                str(receipt.get("tool_name") or receipt.get("effect") or "tool"),
                receipt_object["ref"],
                str(receipt.get("timestamp") or now),
            ),
        )
    publication.expect_one(
        "UPDATE session_processes SET state_revision=? WHERE process_id=? AND state_revision=?",
        (committed_revision, process_id, previous_revision))
    # A historical fact owns its complete immutable closure, even
    # after current session rows replace those references.
    for digest in descriptors:
        publication.execute("INSERT INTO object_refs(owner_type,owner_id,field_name,digest,created_at) VALUES(?,?,?,?,?)",
            ("runtime_fact", f"{process_id}:{committed_revision}", digest, digest, now))
    from ..runtime_events import insert_fact
    facts = [insert_fact(publication, event_id=f"checkpoint:{process_id}:{committed_revision}",
        kind="checkpoint_committed", project_id=self.paths.project_id,
        session_id=session_id, task_id=task_id, process_id=process_id,
        stream_id=f"process:{process_id}", sequence=committed_revision,
        payload_ref=fact["ref"], occurred_at=now,
        causation_id=str(snapshot.get("causation_id") or ""))]
    if barrier is not None:
        sequence = checkpoint_sequence + 1
        publication.execute("INSERT INTO session_events(event_id,process_id,sequence,event_type,payload_ref,occurred_at) VALUES(?,?,?,'agent_complete',?,?)",
            (f"terminal:{process_id}", process_id, sequence, barrier["ref"], now))
        facts.append(insert_fact(publication, event_id=f"ledger:{process_id}:{sequence}", kind="agent_complete",
            project_id=self.paths.project_id, session_id=session_id, task_id=task_id, process_id=process_id,
            stream_id=f"ledger:{process_id}", sequence=sequence, payload_ref=barrier["ref"], occurred_at=now,
            causation_id=f"checkpoint:{process_id}:{committed_revision}"))
    writes = publication.seal(basis, objects, process_id, parent_process_id)
    if limits is not None:
        limits.require_publication(writes, objects)
        limits.require_transition(footprint, len(objects.members), len(message_objects), len(dependencies), barrier is not None)
    return CheckpointPublication(process_id, session_id, parent_process_id, basis, objects, writes,
        max(logical_estimate, writes.parameter_bytes), len(message_objects), len(dependencies),
        barrier is not None, tuple(facts), limits, footprint)


def save_agent_checkpoint(self: CheckpointsPort, snapshot: dict) -> str:
    """Publish the legacy path from the same closed repository-owned plan.

    Future batch admission consumes the plan without first creating CAS files.
    This wrapper still has the documented legacy lifetime/collector limitations.
    """
    plan = plan_agent_checkpoint(self, snapshot)
    self._reserve_authoritative_write(plan.estimated_sql_bytes)
    plan.objects.materialize_legacy(self._write_blob_file)
    with self._state_lock:
        with authoritative_transaction(self._state):
            plan.apply(self._state)
    return plan.reference()
