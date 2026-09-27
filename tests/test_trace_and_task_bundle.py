from __future__ import annotations

import json
from types import SimpleNamespace

from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.code_dossier import (
    build_code_dossier,
    build_shard_dossier,
    suggest_task_shards,
)
from backend.core.loop.manager import AgentProcessManager
from backend.core.loop.models import AgentProcess, ProcessStatus, RingLevel
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.trace import DeltaCoalescer, TraceJournal, read_trace_detail
from backend.core.native_host import NativeHost
from backend.core.storage import StorageRuntime


def test_daemon_forwards_any_redacted_traced_runtime_event():
    from backend.core.daemon import _is_traced_runtime_event

    assert _is_traced_runtime_event({
        "schema_version": 1,
        "trace_id": "task-1",
        "seq": 7,
        "event": "composite_step_result",
    })
    assert not _is_traced_runtime_event({
        "event": "untrusted_internal_event",
        "trace_id": "",
        "seq": 7,
        "schema_version": 1,
    })


def test_trace_journal_redacts_secrets_deduplicates_details_and_replays(tmp_path_factory):
    tmp_path = tmp_path_factory
    trace = TraceJournal(tmp_path, "task-1")
    synthetic_key = "sk-" + "abcdefghijklmnop"
    first = trace.append(
        {"event": "reasoning_delta", "delta": "inspect " + synthetic_key},
        detail={"authorization": "Bearer private-token-value", "value": "same"},
    )
    second = trace.append(
        {"event": "provider_request_started"},
        detail={"authorization": "Bearer private-token-value", "value": "same"},
    )
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert first["detail_ref"] == second["detail_ref"]
    assert trace.path.name == "observability.sqlite3"
    assert not (tmp_path / ".gitgo" / "traces" / "events").exists()
    full_replay = trace.read()
    assert full_replay["events"][0]["delta"] == "inspect [REDACTED_API_KEY]"
    detail = read_trace_detail(tmp_path, first["detail_ref"])
    assert detail["authorization"] == "[REDACTED]"
    replay = trace.read(after_seq=1)
    assert [item["event"] for item in replay["events"]] == [
        "provider_request_started",
    ]


def test_trace_redaction_preserves_token_telemetry_types(tmp_path_factory):
    trace = TraceJournal(tmp_path_factory, "token-telemetry")
    cred = "credential-" + "value"
    event = trace.append({
        "event": "agent_complete",
        "outcome": {
            "session_tokens": 123,
            "metadata": {
                "input_tokens": 100,
                "cache_read_tokens": 80,
                "token": cred,
                "access_token": cred,
            },
        },
    })
    outcome = event["outcome"]
    assert outcome["session_tokens"] == 123
    assert outcome["metadata"]["input_tokens"] == 100
    assert outcome["metadata"]["cache_read_tokens"] == 80
    assert outcome["metadata"]["token"] == "[REDACTED]"
    assert outcome["metadata"]["access_token"] == "[REDACTED]"


def test_trace_journal_recovers_sequence_after_corrupt_tail_and_fails_closed(tmp_path_factory):
    tmp_path = tmp_path_factory
    raw_id = "../unsafe/task"
    safe_id = "_unsafe_task-" + __import__("hashlib").sha256(
        raw_id.encode("utf-8")
    ).hexdigest()[:12]
    legacy = tmp_path / ".gitgo" / "traces" / "events"
    legacy.mkdir(parents=True)
    (legacy / f"{safe_id}.jsonl").write_text(
        json.dumps({
            "schema_version": 1,
            "trace_id": safe_id,
            "seq": 1,
            "time": "2026-01-01T00:00:00+00:00",
            "event": "first",
            "private_key": "[REDACTED]",
        }) + "\ncorrupt partial tail\n",
        encoding="utf-8",
    )
    resumed = TraceJournal(tmp_path, "../unsafe/task")
    second = resumed.append({"event": "second", "object": object()})
    assert second["seq"] == 2
    replay = resumed.read()
    assert [item["seq"] for item in replay["events"]] == [1, 2]
    assert replay["events"][0]["private_key"] == "[REDACTED]"
    assert replay["events"][1]["object"] == "[UNSUPPORTED_TYPE:object]"
    assert not (tmp_path / ".gitgo" / "traces").exists()
    assert list((tmp_path / ".gitgo").glob("traces.legacy-imported-*"))


def test_legacy_trace_import_repairs_sparse_sequences_before_archiving(
    tmp_path_factory,
):
    workspace = tmp_path_factory
    events = workspace / ".gitgo" / "traces" / "events"
    events.mkdir(parents=True)
    trace_id = "sparse"
    (events / f"{trace_id}.jsonl").write_text(
        "\n".join([
            json.dumps({"seq": 1, "event": "legacy-first"}),
            json.dumps({"seq": 2, "event": "already-present"}),
        ]) + "\n",
        encoding="utf-8",
    )
    with StorageRuntime(workspace, state_home=workspace / "state") as storage:
        assert storage.import_trace_records(trace_id, [
            ({"seq": 2, "event": "already-present"}, None),
        ]) == 1
        journal = TraceJournal(workspace, trace_id, storage=storage)
        assert [item["seq"] for item in journal.read(limit=10)["events"]] == [1, 2]
    assert not (workspace / ".gitgo" / "traces").exists()


def test_legacy_oversized_inline_trace_moves_lossless_body_to_cas(
    tmp_path_factory,
):
    workspace = tmp_path_factory
    events = workspace / ".gitgo" / "traces" / "events"
    events.mkdir(parents=True)
    trace_id = "oversized"
    oversized = {
        "seq": 1,
        "event": "agent_complete",
        "process_id": "process-1",
        "outcome": {"report": "x" * (70 * 1024)},
    }
    (events / f"{trace_id}.jsonl").write_text(
        json.dumps(oversized) + "\n", encoding="utf-8",
    )
    with StorageRuntime(workspace, state_home=workspace / "state") as storage:
        journal = TraceJournal(workspace, trace_id, storage=storage)
        event = journal.read(limit=10)["events"][0]
        assert event["legacy_record_in_detail"] is True
        assert "outcome" not in event
        detail = storage.read_trace_detail(event["detail_ref"])
        assert detail["legacy_record"]["outcome"]["report"] == oversized[
            "outcome"
        ]["report"]
    assert not (workspace / ".gitgo" / "traces").exists()


def test_code_dossier_is_deterministic_and_shards_by_component(tmp_path_factory):
    tmp_path = tmp_path_factory
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "alpha" / "service.py").write_text(
        "import json\n\ndef run(value):\n    return json.dumps(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "beta" / "view.ts").write_text(
        "import { run } from '../alpha/service'\nexport function render() { return run(1) }\n",
        encoding="utf-8",
    )
    first = build_code_dossier(tmp_path, ["alpha", "beta"])
    second = build_code_dossier(tmp_path, ["beta", "alpha"])
    assert first["digest"] == second["digest"]
    assert first["file_count"] == 2
    assert first["files"][0]["symbols"][0]["name"] == "run"
    assert "000003: def run(value):" in first["files"][0]["numbered_source"]
    plan = suggest_task_shards(first, goal="Audit boundary behavior")
    assert [item["component"] for item in plan["shards"]] == ["alpha", "beta"]
    assert plan["requires_semantic_partition"] is False
    shard = build_shard_dossier(first, plan["shards"][0])
    assert shard["source_complete"] is True
    assert [item["path"] for item in shard["files"]] == ["alpha/service.py"]
    missing = build_shard_dossier(first, {
        "shard_id": "missing", "component": "alpha",
        "target_files": ["alpha/service.py", "alpha/missing.py"],
    })
    assert missing["source_complete"] is False


def test_code_dossier_excludes_private_harness_directories(tmp_path_factory):
    tmp_path = tmp_path_factory
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("secret", encoding="utf-8")
    (tmp_path / ".codex" / "config.toml").write_text("secret", encoding="utf-8")
    (tmp_path / "src" / "ok.py").write_text("value = 1\n", encoding="utf-8")
    dossier = build_code_dossier(tmp_path, ["."])
    assert [item["path"] for item in dossier["files"]] == ["src/ok.py"]


def test_trace_delta_coalescer_preserves_content_and_flushes_before_semantics():
    batcher = DeltaCoalescer(min_chars=5)
    base = {"event": "reasoning_delta", "process_id": "p1", "task_id": "t1"}
    assert batcher.push({**base, "delta": "ab"}) == []
    merged = batcher.push({**base, "delta": "cde"})
    assert merged[0]["delta"] == "abcde"
    assert merged[0]["coalesced_chunks"] == 2

    assert batcher.push({**base, "delta": "tail"}) == []
    flushed = batcher.push({"event": "provider_usage", "process_id": "p1"})
    assert [event["event"] for event in flushed] == [
        "reasoning_delta", "provider_usage",
    ]
    assert flushed[0]["delta"] == "tail"


def test_default_delta_coalescer_preserves_budget_for_semantic_events():
    batcher = DeltaCoalescer()
    emitted = []
    source = "r" * 12_000
    for offset in range(0, len(source), 24):
        emitted.extend(batcher.push({
            "event": "reasoning_delta",
            "process_id": "p1",
            "task_id": "t1",
            "delta": source[offset:offset + 24],
        }))
    emitted.extend(batcher.push({
        "event": "toolcall_start",
        "process_id": "p1",
        "task_id": "t1",
        "tool_call_id": "call-1",
        "tool_name": "write_file",
    }))

    reasoning = [item for item in emitted if item["event"] == "reasoning_delta"]
    assert "".join(item["delta"] for item in reasoning) == source
    assert len(reasoning) < 20
    assert emitted[-1]["event"] == "toolcall_start"


def test_trace_consolidates_tool_fragments_and_provider_request_prefixes(tmp_path_factory):
    journal = TraceJournal(tmp_path_factory, "bounded-provider-trace")
    fragment = journal.append({
        "event": "toolcall_delta", "process_id": "p1",
        "tool_call_id": "call-1", "delta": '{"path":"a.py"}',
    })
    assert fragment["trace_persisted"] is False
    assert fragment["consolidated_into"] == "toolcall_done"
    assert journal.read()["events"] == []

    first = journal.append({
        "event": "provider_request_started", "process_id": "p1",
    }, detail={
        "representation": "canonical_provider_input",
        "messages": [{"role": "system", "content": "stable"}],
        "tools": [{"name": "read_file"}],
    })
    second = journal.append({
        "event": "provider_request_started", "process_id": "p1",
    }, detail={
        "representation": "canonical_provider_input",
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "next"},
        ],
        "tools": [{"name": "read_file"}],
    })
    first_detail = journal.read_detail(first["detail_ref"])
    second_detail = journal.read_detail(second["detail_ref"])
    assert first_detail["snapshot_mode"] == "full"
    assert second_detail["snapshot_mode"] == "prefix_delta"
    assert second_detail["base_detail_ref"] == first["detail_ref"]
    assert second_detail["common_prefix_messages"] == 1
    assert second_detail["appended_messages"] == [
        {"role": "user", "content": "next"},
    ]
    assert second_detail["tools_reused"] is True
    assert second_detail["tools"] == []


def test_trace_delta_coalescer_keeps_agents_and_tool_calls_separate():
    batcher = DeltaCoalescer(min_chars=100)
    batcher.push({
        "event": "toolcall_delta", "process_id": "p1",
        "tool_call_id": "c1", "delta": "one",
    })
    batcher.push({
        "event": "toolcall_delta", "process_id": "p2",
        "tool_call_id": "c2", "delta": "two",
    })
    flushed = batcher.flush()
    assert {(event["process_id"], event["delta"]) for event in flushed} == {
        ("p1", "one"), ("p2", "two"),
    }


def test_code_dossier_never_forces_a_stale_graph_rebuild(tmp_path_factory, monkeypatch):
    from backend.core import dependency_graph as graph_module

    tmp_path = tmp_path_factory
    (tmp_path / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    graph_module.build_dependency_graph(tmp_path)
    (tmp_path / "a.py").write_text("def current():\n    return 2\n", encoding="utf-8")

    def forbidden_rebuild(_workspace):
        raise AssertionError("bounded dossier must not rebuild the whole graph")

    monkeypatch.setattr(graph_module, "build_dependency_graph", forbidden_rebuild)
    dossier = build_code_dossier(tmp_path, ["a.py"])
    assert dossier["dependency_graph"]["mode"] == "snapshot_allow_stale"
    assert dossier["dependency_graph"]["fresh_for_all_targets"] is False


def test_supervisor_bundle_shortcut_starts_deterministic_children(
    tmp_path_factory, monkeypatch,
):
    tmp_path = tmp_path_factory
    from backend.core.loop import executor as executor_module

    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "alpha" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "beta" / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    manager = AgentProcessManager(max_concurrency=4)
    supervisor = manager.fork(
        parent_id=None,
        role="supervisor",
        tool_registry=ToolRegistry(CapabilityProfiles.resolve_tools("supervisor.control")),
        max_steps=5,
        ring_level=RingLevel.RING_0,
        workspace_path=str(tmp_path),
        task_id="root",
        actor_kind="supervisor",
        capability_profile_id="supervisor.control",
        task_kind="supervisor",
    )

    def fake_agent_step(*, process, **_kwargs):
        process.status = ProcessStatus.COMPLETED
        process.result = {"status": "completed", "process_id": process.process_id}
        return process.result

    monkeypatch.setattr(executor_module, "agent_step", fake_agent_step)
    events = []
    tools = executor_module._build_internal_tools(
        supervisor,
        {},
        str(tmp_path),
        object(),
        SimpleNamespace(_executors={}),
        on_stream_event=events.append,
    )
    assert tools["complete_supervision"].execute({
        "result": "reviewed synthesis",
    }) == {"accepted": True, "result": "reviewed synthesis"}
    result = tools["delegate_task_bundle"].execute({
        "goal": "Audit component behavior without modifying files",
        "target_files": ["alpha", "beta"],
        "acceptance_criteria": ["Return evidence"],
        "capability_profile_id": "governance.observe",
        "task_kind": "answer",
        "max_steps": 3,
    })
    assert result["delegated"] is True
    assert len(result["children"]) == 2
    assert len({item["process_id"] for item in result["children"]}) == 2
    for item in result["children"]:
        child = manager.get(item["process_id"])
        assert child is not None
        manager.wait(child.process_id, timeout=2)
        assert child.status == ProcessStatus.COMPLETED
        assert item["handoff_ref"] in child.task_description
        assert set(child.tool_registry.list_all()) == {
            "artifact_read", "calculate", "context_open", "context_search",
            "decision_evidence",
        }
    waited = tools["wait_agents"].execute({
        "process_ids": [item["process_id"] for item in result["children"]],
        "timeout": 2,
    })
    assert waited["all_terminal"] is True
    for outcome in waited["agents"].values():
        assert outcome["status"] == "completed"
        assert outcome["outcome_ref"].startswith("context:agent-outcomes/")
        assert "provider_usage" not in outcome
    assert any(event.get("event") == "task_bundle_delegated" for event in events)

    def placeholder(name):
        return AgentTool(
            name=name, description=name,
            parameters={"type": "object", "properties": {}},
            execute=lambda _args: {},
        )

    all_tools = {
        name: placeholder(name) for name in supervisor.tool_registry.list_all()
    }
    all_tools.update(tools)
    authorized, unavailable = executor_module._select_authorized_tools(
        supervisor, all_tools,
    )
    assert unavailable == []
    # A new turn may inspect historical B evidence, but historical delegation
    # must not remove the supervisor's return-context shortcut.
    assert "assemble_return_context" in authorized
    assert "read_file" in authorized
    assert "complete_supervision" in authorized
    assert "review_child_outcome" in authorized


def test_completion_protocol_surface_tracks_current_task_kind():
    from backend.core.loop import executor as executor_module

    def placeholder(name):
        return AgentTool(
            name=name, description=name,
            parameters={"type": "object", "properties": {}},
            execute=lambda _args: {},
        )

    registry = ToolRegistry([
        "complete_supervision", "complete_task", "complete_review", "read_file",
    ])
    process = AgentProcess(
        process_id="routing-surface", parent_id=None, role="supervisor",
        max_steps=5, ring_level=RingLevel.RING_0,
        active_task_id="routing-surface", actor_kind="supervisor",
        capability_profile_id="supervisor.control", task_kind="answer",
        tool_registry=registry,
    )
    all_tools = {name: placeholder(name) for name in registry.list_all()}

    answer, unavailable = executor_module._select_authorized_tools(process, all_tools)
    assert unavailable == []
    assert not {"complete_supervision", "complete_task", "complete_review"} & answer.keys()

    process.task_kind = "action"
    action, unavailable = executor_module._select_authorized_tools(process, all_tools)
    assert unavailable == []
    assert "complete_task" in action
    assert "complete_supervision" not in action

    process.task_kind = "supervisor"
    supervisor, unavailable = executor_module._select_authorized_tools(process, all_tools)
    assert unavailable == []
    assert "complete_supervision" in supervisor
    assert "complete_task" not in supervisor


def test_bounded_self_execute_contract_hides_delegation_entry_points():
    from backend.core.loop import executor as executor_module
    from backend.core.loop.task_contract import publish_contract

    def placeholder(name):
        return AgentTool(
            name=name, description=name,
            parameters={"type": "object", "properties": {}},
            execute=lambda _args: {},
        )

    names = [
        "declare_task_contract", "request_self_execute", "read_file",
        "delegate_task", "delegate_task_dag", "delegate_task_bundle",
    ]
    process = AgentProcess(
        process_id="bounded-route-surface", parent_id=None, role="supervisor",
        max_steps=5, ring_level=RingLevel.RING_0,
        active_task_id="bounded-route-surface", actor_kind="supervisor",
        capability_profile_id="supervisor.control", task_kind="answer",
        tool_registry=ToolRegistry(names),
    )
    publish_contract(process, {
        "goal": "Make one bounded file change",
        "execution_mode": "self_execute",
        "delegation_required": False,
        "minimum_delegated_outcomes": 0,
        "deliverables": [{
            "kind": "workspace_file", "path": "one.py",
            "description": "bounded output", "required": True,
        }],
        "acceptance_criteria": ["one file exists"],
        "estimated_complexity": "bounded",
        "independent_workstreams": 1,
        "routing_transition": "keep_supervisor",
    })
    all_tools = {name: placeholder(name) for name in names}
    authorized, unavailable = executor_module._select_authorized_tools(
        process, all_tools,
    )
    assert unavailable == []
    assert "request_self_execute" in authorized
    assert not {
        "delegate_task", "delegate_task_dag", "delegate_task_bundle",
    } & authorized.keys()


def test_capability_profiles_expose_dossier_and_bundle_shortcuts():
    observe = CapabilityProfiles.resolve_tools("governance.observe")
    supervisor = CapabilityProfiles.resolve_tools("supervisor.control")
    assert "code_dossier" in observe
    assert "prepare_task_bundle" in supervisor
    assert "delegate_task_bundle" in supervisor
    assert "complete_supervision" in supervisor


def test_native_host_reads_trace_without_starting_a_daemon(tmp_path_factory):
    tmp_path = tmp_path_factory
    journal = TraceJournal(tmp_path, "native-trace")
    journal.append({"event": "text_delta", "delta": "hello"})

    def forbidden_daemon(*_args, **_kwargs):
        raise AssertionError("trace inspection must not start a project daemon")

    host = NativeHost(daemon_factory=forbidden_daemon)
    host.services.project_list = lambda: [{
        "name": "demo", "workspace": str(tmp_path),
    }]
    listed = host._runtime_trace("demo", {"action": "list"})
    assert listed["traces"][0]["trace_id"] == "native-trace"
    replay = host._runtime_trace("demo", {
        "action": "read", "trace_id": "native-trace",
    })
    assert replay["events"][0]["delta"] == "hello"
    host.close()
