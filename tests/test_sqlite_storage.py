from __future__ import annotations

import ast
import json
import os
import subprocess
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.storage import (
    StorageCorruptionDetected,
    StorageHealthLevel,
    StoragePolicy,
    StorageReferenceMissing,
    StorageRuntime,
    assess_repository_scope,
    resolve_storage_paths,
)


def _split_checkpoint(process_id: str = "root", *, host_ledger=None) -> dict:
    return {
        "process_id": process_id,
        "parent_id": "root" if process_id != "root" else "",
        "session_id": f"session-{process_id}",
        "task_id": f"task-{process_id}",
        "status": "completed",
        "role": "worker" if process_id != "root" else "supervisor",
        "actor_kind": "worker" if process_id != "root" else "supervisor",
        "messages": [{"role": "user", "content": f"work {process_id}"}],
        "session_metadata": {
            "context_memo": {},
            "host_ledger": list(host_ledger or []),
            "provider_usage": {"input_tokens": 10},
            "cache_telemetry": [],
            "epoch_archive": [],
        },
        "provider_state": {"reasoning": {"content": "kept"}},
        "result": {"status": "completed", "response": "done"},
    }


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    return workspace


def _checkpoint_damage_fixture(database: Path) -> None:
    # Deliberate main-file damage must have no recoverable WAL. The production
    # runtime leaves WAL cleanup to explicit checkpointing, not implicit close.
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_project_identity_is_stable_and_hot_state_is_external(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "host-state"

    first = resolve_storage_paths(workspace, state_home=state_home)
    second = resolve_storage_paths(workspace, state_home=state_home)

    assert first.project_id == second.project_id
    assert (workspace / ".gitgo" / "project-id").read_text(encoding="ascii").strip()
    assert first.project_root.is_relative_to(state_home)
    assert not first.state_db.is_relative_to(workspace)


def test_existing_identity_never_relaunches_git_on_hot_read(
    tmp_path_factory: Path, monkeypatch,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "host-state"
    first = resolve_storage_paths(workspace, state_home=state_home)

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("an existing Gitgo identity must not spawn Git")

    monkeypatch.setattr("backend.core.storage.paths.subprocess.run", unexpected_git)
    second = resolve_storage_paths(workspace, state_home=state_home)

    assert second.project_id == first.project_id


def test_first_identity_probe_cannot_inherit_native_host_stdin(
    tmp_path_factory: Path, monkeypatch,
):
    workspace = _workspace(tmp_path_factory)
    captured = {}

    def fake_git(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr("backend.core.storage.paths.subprocess.run", fake_git)
    resolve_storage_paths(workspace, state_home=tmp_path_factory / "host-state")

    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL


def test_linked_git_worktrees_share_one_project_identity(tmp_path_factory: Path):
    repository = _workspace(tmp_path_factory)
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "storage@test.invalid"],
        cwd=repository, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Storage Test"],
        cwd=repository, check=True, capture_output=True,
    )
    (repository / "README.md").write_text("storage\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True
    )
    linked = tmp_path_factory / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "storage-test", str(linked)],
        cwd=repository, check=True, capture_output=True,
    )

    state_home = tmp_path_factory / "host-state"
    main_paths = resolve_storage_paths(repository, state_home=state_home)
    linked_paths = resolve_storage_paths(linked, state_home=state_home)

    assert main_paths.project_id == linked_paths.project_id
    assert main_paths.project_root == linked_paths.project_root
    assert (repository / ".git" / "gitgo" / "project-id").exists()
    assert not (linked / ".gitgo" / "project-id").exists()


def test_dual_databases_migrate_and_use_bounded_wal(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as runtime:
        diagnostics = runtime.diagnostics()

        assert runtime.paths.state_db.exists()
        assert runtime.paths.observability_db.exists()
        assert diagnostics["state_pragmas"]["journal_mode"] == "wal"
        assert diagnostics["observability_pragmas"]["journal_mode"] == "wal"
        assert diagnostics["state_pragmas"]["foreign_keys"] == 1
        assert diagnostics["observability_pragmas"]["foreign_keys"] == 1
        assert diagnostics["state_pragmas"]["synchronous"] == 2
        assert diagnostics["observability_pragmas"]["synchronous"] == 1
        assert diagnostics["state_pragmas"]["max_page_count"] > 0
        assert diagnostics["observability_pragmas"]["max_page_count"] > 0
        assert Path(diagnostics["paths"]["health_file"]).exists()


def test_truncated_authoritative_database_is_rejected_without_mutation(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    with StorageRuntime(workspace, state_home=state_home) as runtime:
        state_db = runtime.paths.state_db

    _checkpoint_damage_fixture(state_db)
    original = state_db.read_bytes()
    page_size_raw = int.from_bytes(original[16:18], "big")
    page_size = 65536 if page_size_raw == 1 else page_size_raw
    physical_pages = len(original) // page_size
    damaged = bytearray(original)
    damaged[28:32] = (physical_pages + 1).to_bytes(4, "big")
    state_db.write_bytes(damaged)

    with pytest.raises(StorageCorruptionDetected, match="no WAL can recover"):
        StorageRuntime(workspace, state_home=state_home)
    assert state_db.read_bytes() == damaged


def test_btree_corruption_is_rejected_even_when_header_and_size_look_valid(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    with StorageRuntime(workspace, state_home=state_home) as runtime:
        state_db = runtime.paths.state_db
        for index in range(50):
            runtime.put_state_ref("integrity", f"key-{index}", "a" * 64)

    _checkpoint_damage_fixture(state_db)
    damaged = bytearray(state_db.read_bytes())
    page_size_raw = int.from_bytes(damaged[16:18], "big")
    page_size = 65536 if page_size_raw == 1 else page_size_raw
    assert len(damaged) >= page_size * 2
    damaged[page_size:page_size + 32] = b"\x00" * 32
    state_db.write_bytes(damaged)

    with pytest.raises(StorageCorruptionDetected, match="quick|malformed|corrupt"):
        StorageRuntime(workspace, state_home=state_home)
    assert state_db.read_bytes() == damaged


def test_cas_deduplicates_and_database_stores_only_reference(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as runtime:
        content = b"reasoning remains plaintext but is stored exactly once"
        first = runtime.put_blob(content, media_type="text/plain")
        second = runtime.put_blob(content, media_type="text/plain")
        runtime.put_state_ref("test", "reasoning", first)

        assert first == second
        assert runtime.read_blob(first) == content
        assert runtime.get_state_ref("test", "reasoning") == first
        objects = [path for path in runtime.paths.cas_dir.rglob("*") if path.is_file()]
        assert len(objects) == 1


def test_durable_conversation_read_model_survives_restart_and_hides_internal_turns(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    with StorageRuntime(workspace, state_home=state_home) as runtime:
        runtime.save_agent_checkpoint({
            "process_id": "root-1",
            "session_id": "session-root",
            "task_id": "task-root",
            "status": "completed",
            "role": "supervisor",
            "actor_kind": "supervisor",
            "created_at": "2026-01-01T00:00:00Z",
            "messages": [
                {"role": "user", "content": "Build the feature",
                 "message_type": "conversation"},
                {"role": "assistant", "content": "intermediate provider prose"},
                {"role": "user", "content": "[HOST PROVIDER CONTINUATION] continue",
                 "message_type": "host_provider_continuation"},
            ],
            "result": {"status": "completed", "response": "Feature delivered"},
        })
        runtime.save_agent_checkpoint({
            "process_id": "child-1",
            "parent_id": "root-1",
            "session_id": "session-child",
            "task_id": "task-child",
            "status": "completed",
            "role": "worker",
            "actor_kind": "worker",
            "created_at": "2026-01-01T00:01:00Z",
            "messages": [
                {"role": "user", "content": "Inspect the parser",
                 "message_type": "conversation"},
            ],
            "result": {"status": "completed", "response": "Parser inspected"},
        })

    with StorageRuntime(workspace, state_home=state_home) as reopened:
        result = reopened.read_latest_conversations()

    assert result["conversation_process_id"] == "root-1"
    assert result["session_id"] == "session-root"
    assert [(item["role"], item["content"]) for item in result["main_conversation"]] == [
        ("user", "Build the feature"),
        ("assistant", "Feature delivered"),
    ]
    assert [(item["role"], item["content"])
            for item in result["agent_conversations"]["child-1"]] == [
        ("user", "Inspect the parser"),
        ("assistant", "Parser inspected"),
    ]
    assert all(item["message_id"] for item in result["main_conversation"])


def test_project_b_projection_spans_tasks_excludes_a_and_preserves_task_parent(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"

    def checkpoint(
        process_id: str,
        task_id: str,
        *,
        parent_id: str = "",
        status: str = "completed",
        actor_kind: str = "worker",
    ) -> dict:
        return {
            "process_id": process_id,
            "parent_id": parent_id,
            "session_id": f"session-{process_id}",
            "task_id": task_id,
            "task_kind": "action" if parent_id else "answer",
            "status": status,
            "role": actor_kind,
            "actor_kind": actor_kind,
            "capability_profile_id": (
                "supervisor.control" if actor_kind == "supervisor"
                else "development.workspace"
            ),
            "created_at": f"2026-01-01T00:00:0{len(process_id)}Z",
            "steps_used": 2,
            "max_steps": 10,
            "messages": [{
                "role": "user", "content": f"work for {process_id}",
                "message_type": "conversation",
            }],
            "result": {"status": status, "response": f"result for {process_id}"},
        }

    with StorageRuntime(workspace, state_home=state_home) as runtime:
        runtime.save_agent_checkpoint(checkpoint(
            "root-one", "task-root-one", actor_kind="supervisor",
        ))
        runtime.save_agent_checkpoint(checkpoint(
            "child-complete", "task-child-complete", parent_id="root-one",
        ))
        runtime.save_agent_checkpoint(checkpoint(
            "legacy-child", "task-legacy-child", parent_id="root-one",
            status="failed", actor_kind="",
        ))
        runtime.save_agent_checkpoint(checkpoint(
            "root-two", "task-root-two", actor_kind="supervisor",
        ))
        runtime.save_agent_checkpoint(checkpoint(
            "child-waiting", "task-child-waiting", parent_id="root-two",
            status="waiting", actor_kind="reviewer",
        ))

        processes = runtime.read_project_b_processes()
        conversations = runtime.read_project_b_conversations()
        child_state = runtime.load_agent_process_state("child-complete")

    assert set(processes) == {"child-complete", "legacy-child", "child-waiting"}
    assert processes["child-complete"]["status"] == "completed"
    assert processes["legacy-child"]["status"] == "failed"
    assert processes["child-waiting"]["status"] == "waiting"
    assert set(conversations) == {"child-complete", "legacy-child", "child-waiting"}
    assert child_state is not None
    assert child_state["task"]["parent_task_id"] == "task-root-one"


def test_task_usage_rollup_counts_only_root_task_tree(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"

    def checkpoint(process_id: str, task_id: str, *, parent_id: str = "") -> dict:
        return {
            "process_id": process_id,
            "parent_id": parent_id,
            "session_id": "session-usage",
            "task_id": task_id,
            "task_kind": "answer" if not parent_id else "action",
            "status": "completed",
            "role": "supervisor" if not parent_id else "worker",
            "actor_kind": "supervisor" if not parent_id else "worker",
            "created_at": "2026-01-01T00:00:00Z",
            "messages": [{"role": "user", "content": "task"}],
            "result": {
                "status": "completed",
                "duration_ms": 1250,
                "tool_calls_executed": 2,
                "response": "done",
                "metadata": {
                    "task_kind": "answer" if not parent_id else "action",
                    "task_tree_budget": {"used": {
                        "provider_calls": 3,
                        "reported_input_tokens": 1000,
                        "reported_output_tokens": 100,
                        "reported_cache_read_tokens": 750,
                        "reported_cache_write_tokens": 0,
                    }},
                },
            },
        }

    with StorageRuntime(workspace, state_home=state_home) as runtime:
        runtime.save_agent_checkpoint(checkpoint("root", "task-root"))
        runtime.save_agent_checkpoint(checkpoint("child", "task-child", parent_id="root"))
        usage = runtime.read_task_usage()

    assert usage["summary"]["task_count"] == 1
    assert usage["summary"]["provider_calls"] == 3
    assert usage["summary"]["input_tokens"] == 1000
    assert usage["summary"]["cache_hit_ratio"] == 0.75
    assert [item["task_id"] for item in usage["tasks"]] == ["task-root"]
    assert usage["page"]["summary_scope"] == "all_authoritative_root_tasks"


def test_checkpoint_registers_complete_cas_ownership_and_gc_keeps_split_objects(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    policy = StoragePolicy(cas_gc_grace_days=1)
    with StorageRuntime(
        workspace, state_home=tmp_path_factory / "state", policy=policy,
    ) as runtime:
        runtime.save_agent_checkpoint(_split_checkpoint())
        with runtime._state_lock:
            owned = {
                str(row[0]) for row in runtime._state.execute(
                    "SELECT digest FROM object_refs WHERE owner_type='session_checkpoint' "
                    "AND owner_id='session-root'"
                ).fetchall()
            }
        assert len(owned) >= 8
        for digest in owned:
            path = runtime.paths.cas_dir / digest[:2] / digest[2:]
            os.utime(path, (1, 1))
        result = runtime.maintain_cas(force=True)
        assert result["deleted"] == 0
        assert all(
            (runtime.paths.cas_dir / digest[:2] / digest[2:]).is_file()
            for digest in owned
        )


def test_legacy_nested_checkpoint_refs_survive_gc_without_object_ref_index(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    policy = StoragePolicy(cas_gc_grace_days=1)
    with StorageRuntime(
        workspace, state_home=tmp_path_factory / "state", policy=policy,
    ) as runtime:
        runtime.save_agent_checkpoint(_split_checkpoint())
        with runtime._state_lock:
            metadata_ref = str(runtime._state.execute(
                "SELECT metadata_ref FROM sessions WHERE session_id='session-root'"
            ).fetchone()[0])
            runtime._state.execute("DELETE FROM object_refs")
        metadata = runtime._read_json_blob(metadata_ref)
        nested = runtime._nested_cas_digests(metadata)
        assert nested
        for digest in nested:
            os.utime(runtime.paths.cas_dir / digest[:2] / digest[2:], (1, 1))
        runtime.maintain_cas(force=True)
        assert all(
            (runtime.paths.cas_dir / digest[:2] / digest[2:]).is_file()
            for digest in nested
        )


def test_missing_canonical_empty_checkpoint_object_is_reconstructed_by_hash(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as runtime:
        runtime.save_agent_checkpoint(_split_checkpoint())
        with runtime._state_lock:
            metadata_ref = str(runtime._state.execute(
                "SELECT metadata_ref FROM sessions WHERE session_id='session-root'"
            ).fetchone()[0])
        metadata = runtime._read_json_blob(metadata_ref)
        ref = str(metadata["context_memo_ref"])
        digest = ref.removeprefix("sha256:")
        path = runtime.paths.cas_dir / digest[:2] / digest[2:]
        path.unlink()

        restored = runtime.load_agent_session_state("root")

        assert restored["context_memo"] == {}
        assert path.read_bytes() == b"{}"
        assert runtime.check_health(force_publish=True).level is StorageHealthLevel.OK


def test_missing_noncanonical_checkpoint_object_is_explicit_and_b_isolated(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    ledger = [{"event": "decision", "question": "keep this"}]
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as runtime:
        runtime.save_agent_checkpoint(_split_checkpoint("child", host_ledger=ledger))
        with runtime._state_lock:
            metadata_ref = str(runtime._state.execute(
                "SELECT metadata_ref FROM sessions WHERE session_id='session-child'"
            ).fetchone()[0])
        metadata = runtime._read_json_blob(metadata_ref)
        ref = str(metadata["host_ledger_ref"])
        digest = ref.removeprefix("sha256:")
        (runtime.paths.cas_dir / digest[:2] / digest[2:]).unlink()

        with pytest.raises(StorageReferenceMissing) as exc:
            runtime.load_agent_session_state("child")
        assert exc.value.ref == ref

        processes = runtime.read_project_b_processes()
        conversations = runtime.read_project_b_conversations()
        health = runtime.check_health(force_publish=True)

        assert processes["child"]["storage_error"]["code"] == (
            "STORAGE_CAS_REFERENCE_MISSING"
        )
        assert any(item["kind"] == "error" for item in conversations["child"])
        assert health.level is StorageHealthLevel.BLOCKED
        assert any("cas_owned_reference_missing" in reason for reason in health.reasons)


def test_task_usage_keyset_pages_do_not_claim_a_bounded_list_is_complete(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as runtime:
        for index in range(3):
            runtime.save_agent_checkpoint({
                "process_id": f"root-{index}", "session_id": f"session-{index}",
                "task_id": f"task-{index}", "task_kind": "answer", "status": "completed",
                "role": "supervisor", "actor_kind": "supervisor",
                "messages": [{"role": "user", "content": "task"}],
                "result": {"status": "completed", "response": "done"},
            })
        first = runtime.read_task_usage(limit=2)
        second = runtime.read_task_usage(limit=2, cursor=first["page"]["next_cursor"])

    assert first["summary"]["task_count"] == 3
    assert first["page"]["has_more"] is True
    assert second["page"]["has_more"] is False
    assert len({item["task_id"] for item in [*first["tasks"], *second["tasks"]]}) == 3


def test_round_summary_survives_reopen_without_loading_verbose_traces(tmp_path_factory):
    workspace = _workspace(tmp_path_factory)
    state_home = tmp_path_factory / "state"
    with StorageRuntime(workspace, state_home=state_home) as runtime:
        runtime.save_agent_checkpoint({
            "process_id": "worker", "session_id": "session-round", "task_id": "child-task",
            "status": "completed", "messages": [{"role": "user", "content": "hello"}],
            "result": {"process_id": "worker", "status": "completed", "response": "done",
                       "duration_ms": 61230, "metadata": {"trace_id": "root-trace"}},
        })
    with StorageRuntime(workspace, state_home=state_home) as runtime:
        def unexpected_trace_read(*_args, **_kwargs):
            raise AssertionError("status polling must not read verbose trace bodies")
        runtime.read_trace_events = unexpected_trace_read
        message = runtime.read_latest_conversations()["main_conversation"][-1]
        assert message["duration_ms"] == 61230
        assert message["trace_id"] == "root-trace"
        assert message["process_id"] == "worker"
        assert message["status"] == "completed"
        assert "reasoning" not in message


def test_failed_round_is_a_durable_error_report(tmp_path_factory):
    workspace = _workspace(tmp_path_factory)
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as runtime:
        runtime.save_agent_checkpoint({
            "process_id": "root", "session_id": "failed-round", "task_id": "failed-task",
            "status": "failed", "messages": [{"role": "user", "content": "do work"}],
            "result": {"status": "failed", "response": "not a final answer",
                       "error": {"code": "TEST_FAILURE", "message": "useful error"}},
        })
        message = runtime.read_latest_conversations()["main_conversation"][-1]
        assert message["kind"] == "error"
        assert message["content"] == "[Error: [TEST_FAILURE] useful error]"


def test_trace_pages_filter_agents_and_skip_raw_deltas_for_compact_view(tmp_path_factory):
    workspace = _workspace(tmp_path_factory)
    with StorageRuntime(workspace, state_home=tmp_path_factory / "state") as runtime:
        for index, (process, event) in enumerate([
            ("A", "reasoning_delta"), ("B", "reasoning_delta"),
            ("B", "tool_result"), ("A", "tool_result"), ("B", "agent_terminal"),
        ], start=1):
            runtime.append_trace_record("tree", {
                "seq": index, "process_id": process, "event": event,
            })
        page = runtime.read_trace_events("tree", process_id="B", include_deltas=False, limit=1)
        assert [item["seq"] for item in page["events"]] == [3]
        assert page["has_more"]
        tail = runtime.read_trace_events("tree", process_id="B", include_deltas=False,
                                         after_seq=page["next_seq"], limit=1)
        assert [item["seq"] for item in tail["events"]] == [5]
        assert not tail["has_more"]
        full = runtime.read_trace_events("tree", process_id="B")
        assert [item["seq"] for item in full["events"]] == [2, 3, 5]
        summary = runtime.read_trace_summary("tree", process_id="B")
        assert summary["complete"] is True
        assert summary["event_count"] == 3
        assert summary["event_counts"] == {
            "agent_terminal": 1, "reasoning_delta": 1, "tool_result": 1,
        }


def test_shutdown_health_snapshot_reflects_post_checkpoint_file_family_sizes(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    runtime = StorageRuntime(workspace, state_home=tmp_path_factory / "state")
    runtime.put_blob(b"durable shutdown measurement", media_type="text/plain")
    paths = runtime.paths
    runtime.close()

    snapshot = json.loads(paths.health_file.read_text(encoding="utf-8"))
    actual_cas = sum(path.stat().st_size for path in paths.cas_dir.rglob("*") if path.is_file())
    actual_state_family = sum(
        path.stat().st_size
        for path in (
            paths.state_db,
            Path(str(paths.state_db) + "-wal"),
            Path(str(paths.state_db) + "-shm"),
        )
        if path.exists()
    )
    assert snapshot["cas_bytes"] == actual_cas
    assert snapshot["state_bytes"] == actual_state_family
    assert snapshot["total_bytes"] >= actual_cas + actual_state_family


def test_observability_rate_limit_degrades_without_blocking_core(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    events: list[dict] = []
    policy = StoragePolicy(
        observability_transactions_per_minute=1,
        observability_logical_bytes_per_minute=1024 * 1024,
    )
    with StorageRuntime(
        workspace,
        state_home=tmp_path_factory / "state",
        policy=policy,
        health_listener=events.append,
    ) as runtime:
        assert runtime.append_event(event_type="task.started", summary="first") is True
        assert runtime.append_event(event_type="transport.delta", summary="second") is False
        runtime.put_state_ref("task", "still-writable", "sha256:" + "0" * 64)

        snapshot = json.loads(runtime.paths.health_file.read_text(encoding="utf-8"))
        assert snapshot["level"] == StorageHealthLevel.DEGRADED.value
        assert "observability_write_throttled" in snapshot["reasons"]
        assert any(event["event"] == "storage_health" for event in events)


def test_large_observability_body_must_use_cas(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    policy = StoragePolicy(max_observability_record_bytes=512)
    with StorageRuntime(
        workspace, state_home=tmp_path_factory / "state", policy=policy
    ) as runtime:
        with pytest.raises(ValueError, match="store the body in CAS"):
            runtime.append_event(event_type="provider.raw", summary="x" * 1024)


def test_global_sqlite_rate_limit_cannot_be_bypassed_by_state_writes(
    tmp_path_factory: Path,
):
    workspace = _workspace(tmp_path_factory)
    policy = StoragePolicy(
        sqlite_transactions_per_minute=1,
        sqlite_estimated_bytes_per_minute=1024 * 1024,
    )
    with StorageRuntime(
        workspace, state_home=tmp_path_factory / "state", policy=policy
    ) as runtime:
        runtime.put_state_ref("task", "one", "ref:one")
        from backend.core.storage import StorageBlocked

        with pytest.raises(StorageBlocked, match="global_sqlite_write_rate_exceeded"):
            runtime.put_state_ref("task", "two", "ref:two")


def test_repository_scope_blocks_home_and_volume_roots(tmp_path_factory: Path):
    workspace = _workspace(tmp_path_factory)
    home_scope = assess_repository_scope(
        workspace, git_root=tmp_path_factory, home=tmp_path_factory
    )
    volume_scope = assess_repository_scope(
        workspace, git_root=Path(workspace.anchor), home=tmp_path_factory / "home"
    )
    normal_scope = assess_repository_scope(
        workspace, git_root=tmp_path_factory, home=tmp_path_factory / "home"
    )

    assert home_scope.allowed is False
    assert "git_root_is_user_home" in home_scope.reasons
    assert volume_scope.allowed is False
    assert "git_root_is_volume_root" in volume_scope.reasons
    assert normal_scope.allowed is True


def test_backend_cannot_open_sqlite_outside_storage_facade():
    backend = Path(__file__).parents[1] / "backend" / "core"
    offenders: list[str] = []
    allowed = (backend / "storage" / "runtime.py").resolve()
    for path in backend.rglob("*.py"):
        if path.resolve() == allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "sqlite3" for alias in node.names):
                offenders.append(str(path))
            if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                offenders.append(str(path))
    assert offenders == []
