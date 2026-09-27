"""Versioned, checksum-verified SQLite schemas.

Migrations are append-only.  Changing an applied migration is treated as
corruption instead of being silently accepted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


STATE_MIGRATIONS = (
    Migration(
        1,
        "state_foundation",
        """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    workspace_hint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    status TEXT NOT NULL,
    context_epoch INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project_updated
    ON sessions(project_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id),
    parent_task_id TEXT REFERENCES tasks(task_id),
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    contract_ref TEXT,
    outcome_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_session_created
    ON tasks(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent
    ON tasks(parent_task_id);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    task_id TEXT REFERENCES tasks(task_id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    reasoning_ref TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_messages_task_sequence
    ON messages(task_id, sequence);
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    tool_name TEXT NOT NULL,
    arguments_ref TEXT,
    result_ref TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task_started
    ON tool_calls(task_id, started_at);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    receipt_type TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_task_type
    ON receipts(task_id, receipt_type);
CREATE TABLE IF NOT EXISTS governance_signals (
    signal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    task_id TEXT REFERENCES tasks(task_id),
    signal_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_owner TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_governance_pending
    ON governance_signals(project_id, state, created_at);
CREATE TABLE IF NOT EXISTS test_evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    test_id TEXT NOT NULL,
    status TEXT NOT NULL,
    seed TEXT,
    payload_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_evidence_task_test
    ON test_evidence(task_id, test_id);
CREATE TABLE IF NOT EXISTS provider_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    provider_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    status TEXT NOT NULL,
    usage_ref TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_attempts_task_started
    ON provider_attempts(task_id, started_at);
CREATE TABLE IF NOT EXISTS mailbox_messages (
    mailbox_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    sender_task_id TEXT REFERENCES tasks(task_id),
    payload_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mailbox_pending
    ON mailbox_messages(task_id, state, created_at);
CREATE TABLE IF NOT EXISTS worktrees (
    worktree_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    task_id TEXT REFERENCES tasks(task_id),
    path_hint TEXT NOT NULL,
    git_head TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dependency_nodes (
    node_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL,
    locator TEXT NOT NULL,
    metadata_ref TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, kind, locator)
);
CREATE TABLE IF NOT EXISTS dependency_edges (
    edge_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    source_node_id TEXT NOT NULL REFERENCES dependency_nodes(node_id),
    target_node_id TEXT NOT NULL REFERENCES dependency_nodes(node_id),
    relation TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dependency_source
    ON dependency_edges(project_id, source_node_id, state);
CREATE INDEX IF NOT EXISTS idx_dependency_target
    ON dependency_edges(project_id, target_node_id, state);
CREATE TABLE IF NOT EXISTS objects (
    digest TEXT PRIMARY KEY,
    byte_length INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS object_refs (
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    digest TEXT NOT NULL REFERENCES objects(digest),
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_type, owner_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_object_refs_digest ON object_refs(digest);
CREATE TABLE IF NOT EXISTS storage_kv (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value_ref TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(namespace, key)
);
""",
    ),
    Migration(
        2,
        "durable_agent_sessions",
        """
ALTER TABLE sessions ADD COLUMN metadata_ref TEXT;
ALTER TABLE sessions ADD COLUMN checkpoint_at TEXT;
ALTER TABLE sessions ADD COLUMN checkpoint_sequence INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS session_processes (
    process_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(session_id),
    task_id TEXT,
    parent_process_id TEXT,
    role TEXT NOT NULL DEFAULT '',
    actor_kind TEXT NOT NULL DEFAULT '',
    capability_profile_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    steps_used INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL DEFAULT 0,
    checkpoint_event_sequence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_processes_session
    ON session_processes(session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_processes_task
    ON session_processes(task_id);
CREATE TABLE IF NOT EXISTS session_events (
    event_id TEXT PRIMARY KEY,
    process_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE(process_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_session_events_process_sequence
    ON session_events(process_id, sequence);
""",
    ),
    Migration(
        3,
        "agent_runtime_state_refs",
        """
ALTER TABLE session_processes ADD COLUMN runtime_state_ref TEXT;
""",
    ),
    Migration(
        4,
        "durable_agent_dag_worktrees",
        """
CREATE TABLE IF NOT EXISTS process_dependencies (
    process_id TEXT NOT NULL,
    depends_on_process_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'declared',
    artifact_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(process_id, depends_on_process_id),
    CHECK(process_id <> depends_on_process_id)
);
CREATE INDEX IF NOT EXISTS idx_process_dependencies_upstream
    ON process_dependencies(depends_on_process_id, state);
ALTER TABLE worktrees ADD COLUMN process_id TEXT;
ALTER TABLE worktrees ADD COLUMN mode TEXT NOT NULL DEFAULT 'write';
ALTER TABLE worktrees ADD COLUMN root_workspace_hint TEXT NOT NULL DEFAULT '';
ALTER TABLE worktrees ADD COLUMN base_commit TEXT NOT NULL DEFAULT '';
ALTER TABLE worktrees ADD COLUMN snapshot_commit TEXT NOT NULL DEFAULT '';
ALTER TABLE worktrees ADD COLUMN result_commit TEXT NOT NULL DEFAULT '';
ALTER TABLE worktrees ADD COLUMN own_commit TEXT NOT NULL DEFAULT '';
ALTER TABLE worktrees ADD COLUMN metadata_ref TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_process
    ON worktrees(process_id) WHERE process_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_worktrees_task_state
    ON worktrees(task_id, state);
""",
    ),
    Migration(
        5,
        "history_and_knowledge",
        """
CREATE TABLE IF NOT EXISTS history_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    project_name TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    parent_event_id TEXT NOT NULL DEFAULT '',
    record_ref TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_project_sequence
    ON history_events(project_name, sequence);
CREATE INDEX IF NOT EXISTS idx_history_operation_sequence
    ON history_events(operation, sequence);
CREATE INDEX IF NOT EXISTS idx_history_correlation
    ON history_events(correlation_id);
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('pending', 'instance', 'abstract')),
    project_name TEXT NOT NULL DEFAULT '',
    tech_stack TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'medium',
    content_hash TEXT NOT NULL,
    record_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_scope_project
    ON lessons(scope, project_name, updated_at);
CREATE INDEX IF NOT EXISTS idx_lessons_scope_stack
    ON lessons(scope, tech_stack, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_lesson_content
    ON lessons(project_name, content_hash) WHERE scope='pending';
""",
    ),
    Migration(
        6,
        "task_usage_rollups",
        """
CREATE TABLE IF NOT EXISTS task_usage (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    process_id TEXT NOT NULL DEFAULT '',
    parent_process_id TEXT NOT NULL DEFAULT '',
    task_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    provider_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_usage_root_updated
    ON task_usage(parent_process_id, updated_at DESC);
""",
    ),
)


STATE_MIGRATIONS += (
    Migration(7, "process_presentation", """
CREATE TABLE process_presentation (
    process_id TEXT PRIMARY KEY REFERENCES session_processes(process_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    updated_at TEXT NOT NULL
);
"""),
)


STATE_MIGRATIONS += (
    Migration(8, "explicit_deletion_lifecycle", """
CREATE TABLE deletion_plans (
    plan_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    state TEXT NOT NULL,
    not_before REAL NOT NULL,
    manifest_json TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_deletion_due ON deletion_plans(state, not_before);
CREATE UNIQUE INDEX idx_deletion_active_subject ON deletion_plans(subject_id)
    WHERE state IN ('scheduled', 'executing', 'blocked', 'recovery_required');
CREATE TABLE deleted_processes (
    process_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL
);
"""),
)


STATE_MIGRATIONS += (
    Migration(9, "addressable_tool_results_and_session_lineage", """
CREATE TABLE tool_result_objects (
    locator TEXT PRIMARY KEY,
    content_ref TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    char_length INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE tool_result_owners (
    locator TEXT NOT NULL REFERENCES tool_result_objects(locator) ON DELETE CASCADE,
    process_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(locator, process_id, task_id, tool_name)
);
CREATE INDEX idx_tool_result_owner_process
    ON tool_result_owners(process_id, created_at DESC);
CREATE INDEX idx_tool_result_owner_task
    ON tool_result_owners(task_id, created_at DESC);

CREATE TABLE session_lineage (
    checkpoint_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    process_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    parent_checkpoint_id TEXT REFERENCES session_lineage(checkpoint_id),
    snapshot_ref TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    context_epoch INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'retained',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, turn_id)
);
CREATE INDEX idx_session_lineage_session_created
    ON session_lineage(session_id, created_at DESC);
CREATE TABLE session_lineage_heads (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    checkpoint_id TEXT REFERENCES session_lineage(checkpoint_id),
    updated_at TEXT NOT NULL
);
"""),
)

OBSERVABILITY_MIGRATIONS = (
    Migration(
        1,
        "observability_foundation",
        """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT,
    session_id TEXT,
    sequence INTEGER,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_ref TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task_sequence
    ON events(task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_recorded
    ON events(recorded_at);
CREATE INDEX IF NOT EXISTS idx_events_type_recorded
    ON events(event_type, recorded_at);
CREATE TABLE IF NOT EXISTS event_rollups (
    bucket_start TEXT NOT NULL,
    bucket_seconds INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    sample_ref TEXT,
    PRIMARY KEY(bucket_start, bucket_seconds, event_type, severity)
);
CREATE TABLE IF NOT EXISTS storage_metrics (
    sampled_at TEXT PRIMARY KEY,
    state_bytes INTEGER NOT NULL,
    observability_bytes INTEGER NOT NULL,
    cas_bytes INTEGER NOT NULL,
    free_bytes INTEGER NOT NULL,
    health_level TEXT NOT NULL
);
""",
    ),
    Migration(
        2,
        "durable_bounded_traces",
        """
CREATE TABLE IF NOT EXISTS trace_events (
    trace_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    record_json TEXT NOT NULL,
    detail_ref TEXT,
    detail_bytes INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL,
    monotonic_ns INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(trace_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_trace_events_occurred
    ON trace_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_trace_events_type_occurred
    ON trace_events(event_type, occurred_at);
CREATE TABLE IF NOT EXISTS observability_maintenance (
    maintenance_key TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
""",
    ),
)


STATE_MIGRATIONS += (
    Migration(10, "versioned_project_custom_tools", """
CREATE TABLE custom_tools (
    tool_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'archived')),
    current_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE custom_tool_versions (
    tool_id TEXT NOT NULL REFERENCES custom_tools(tool_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    spec_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tool_id, version),
    UNIQUE(tool_id, digest)
);
CREATE INDEX idx_custom_tools_state_name
    ON custom_tools(state, name);
CREATE INDEX idx_custom_tool_versions_created
    ON custom_tool_versions(tool_id, created_at DESC);
"""),
)
