"""Focused regression tests for the Agent runtime P0 infrastructure.

Written with unittest so this critical subset can run without pytest.
"""

from __future__ import annotations

from backend.core.loop.execution_contract import NATIVE_PROCESS, data_broker

import queue
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.core.daemon.client import DaemonClient
from backend.core.daemon import (
    _background_llm_provider, _start_background_harvest,
)
from backend.core.daemon.dispatch import (
    _cmd_loop_status, _cmd_task, _resolve_capability_command,
)
from backend.core.loop.agent_tool import AgentTool
from backend.core.loop.executor import (
    _apply_host_task_transitions, _build_internal_tools,
    _build_current_turn_envelope, _select_authorized_tools, agent_step,
)
from backend.core.loop.manager import AgentProcessManager, SessionStore
from backend.core.loop.mailbox import AgentMailbox, MailboxClosedError
from backend.core.loop.models import ProcessStatus, RingLevel
from backend.core.loop.outcome import OutcomeStatus, TaskOutcome
from backend.core.loop.runtime import AgentRuntimeFactory, RuntimeSpec
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.capabilities import CapabilityProfiles
from backend.core.loop.capability_negotiation import record_provider_changed
from backend.core.loop.completion_protocol import (
    CompletionClaim, HostCompletionEvaluator,
)
from backend.core.loop.prompt_compiler import PromptCompiler
from backend.core.loop.session import AgentSession
from backend.core.loop.test_manifest import TestManifest, run_registered_test
from backend.core.loop.test_manifest import SeedResult, TestRecord
from backend.core.storage import StorageRuntime


class LoopStatusProjectionTests(unittest.TestCase):
    def test_status_merges_latest_durable_a_with_project_b_rows(self):
        class Storage:
            def read_project_b_processes(self):
                return {"b": {"process_id": "b", "actor_kind": "worker"}}

            def read_latest_root_process(self):
                return {
                    "process_id": "a", "actor_kind": "supervisor",
                    "context": {"estimated_tokens": 321, "limit": 1000,
                                "breakdown": {"sections": [{"name": "User conversation", "estimated_tokens": 321, "ratio": .321}]}},
                }

            def read_latest_conversations(self): return {}
            def read_project_b_conversations(self): return {}
            def check_health(self): return SimpleNamespace(to_dict=lambda: {"level": "ok"})
            def record_storage_metric(self): pass
            def maintain_cas(self): pass

        emitted: list[dict] = []
        with patch("backend.core.history.HistoryManager.load", return_value=[]):
            _cmd_loop_status(
                {}, SimpleNamespace(), SimpleNamespace(name="demo"),
                {"apm": None, "storage": Storage()}, emitted.append,
            )
        processes = emitted[-1]["result"]["processes"]
        self.assertEqual(set(processes), {"a", "b"})
        self.assertEqual(processes["a"]["context"]["estimated_tokens"], 321)

    def test_corrupt_durable_projection_preserves_live_processes_and_blocks_storage(self):
        class DamagedStorage:
            def _damaged(self):
                raise RuntimeError("database disk image is malformed")

            read_project_b_processes = _damaged
            read_latest_conversations = _damaged
            read_project_b_conversations = _damaged

            def check_health(self):  # pragma: no cover - must be suppressed
                raise AssertionError("health must not erase a projection failure")

            def record_storage_metric(self):  # pragma: no cover
                raise AssertionError("must not write metrics after a failed projection")

            def maintain_cas(self):  # pragma: no cover
                raise AssertionError("must not maintain CAS after a failed projection")

        live_b = SimpleNamespace(
            process_id="live-b",
            role="worker",
            ring_level=SimpleNamespace(value=3),
            status=SimpleNamespace(value="running"),
            steps_used=1,
            max_steps=10,
            parent_id="root-a",
            depends_on=[],
            created_at="2026-01-01T00:00:00Z",
            worktree_path="",
            worktree={},
            pending_decision=None,
            recovery=None,
            session=None,
            coordination_snapshot=lambda: ([], {}, {}),
        )
        emitted: list[dict] = []
        with patch("backend.core.history.HistoryManager.load", return_value=[]):
            _cmd_loop_status(
                {}, SimpleNamespace(), SimpleNamespace(name="demo"),
                {
                    "apm": SimpleNamespace(_processes={"live-b": live_b}),
                    "storage": DamagedStorage(),
                },
                emitted.append,
            )

        result = emitted[-1]["result"]
        self.assertIn("live-b", result["processes"])
        self.assertEqual(result["storage"]["level"], "blocked")
        self.assertIn("storage_projection_failed", result["storage"]["reasons"])
        self.assertEqual(len(result["storage"]["projection_errors"]), 3)


def _tool(name: str) -> AgentTool:
    return AgentTool(
        execution_contract=data_broker("test.fixture"), name=name,
        description=f"test tool {name}",
        parameters={"type": "object", "properties": {}},
        execute=lambda _args: {"ok": True},
    )


class ChildArtifactReadTests(unittest.TestCase):
    def test_completed_shared_workspace_child_uses_bounded_artifact_reader(self):
        manager = AgentProcessManager()
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "result.txt").write_text("SHARED-OK\n", encoding="utf-8")
            with patch("backend.core.history.HistoryManager.add_operation"):
                supervisor = manager.fork(
                    parent_id=None, role="supervisor", actor_kind="supervisor",
                    capability_profile_id="supervisor.control",
                    tool_registry=ToolRegistry([]), max_steps=8,
                    ring_level=RingLevel.RING_0, task_id="shared-artifact",
                    task_kind="supervisor",
                )
                child = manager.fork(
                    parent_id=supervisor.process_id, role="executor",
                    actor_kind="worker", capability_profile_id="development.workspace",
                    tool_registry=ToolRegistry([]), max_steps=4,
                    ring_level=RingLevel.RING_3, task_id="shared-artifact:1",
                    task_kind="action",
                )
            child.status = ProcessStatus.COMPLETED

            result = _build_internal_tools(
                supervisor, workspace_path=str(root),
            )["read_child_artifact"].execute({
                "process_id": child.process_id,
                "path": "result.txt",
            })

        self.assertEqual(result["content"], "SHARED-OK\n")
        self.assertEqual(result["process_id"], child.process_id)
        self.assertEqual(result["authority"], "shared_workspace_current")


class RuntimeFactoryTests(unittest.TestCase):
    def test_process_and_session_are_created_atomically(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="executor",
            ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry([]),
            max_steps=10,
            task_description="inspect status",
            task_id="task-1",
        ))

        self.assertIsNotNone(process.session)
        self.assertEqual(process.task_description, "inspect status")
        self.assertEqual(process.active_task_id, "task-1")

    def test_manager_delegates_to_runtime_factory(self):
        manager = AgentProcessManager()
        with patch("backend.core.history.HistoryManager.add_operation"):
            process = manager.fork(
                parent_id=None,
                role="executor",
                tool_registry=ToolRegistry([]),
                max_steps=5,
                ring_level=RingLevel.RING_3,
                task_description="task",
                task_id="task-2",
            )

        self.assertIsNotNone(process.session)
        self.assertEqual(process.task_description, "task")

    def test_empty_registry_is_explicit_text_only_capability(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="executor",
            ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry([]),
            max_steps=5,
        ))
        authorized, unavailable = _select_authorized_tools(
            process, {"scan": _tool("scan")},
        )

        self.assertEqual(authorized, {})
        self.assertNotIn("declare_task_contract", unavailable)


class CapabilityLeaseTests(unittest.TestCase):
    def test_plain_chat_has_no_project_or_self_execution_tools(self):
        self.assertEqual(CapabilityProfiles.resolve_tools("supervisor.chat"), [])
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.chat",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="answer",
        ))
        selected, unavailable = _select_authorized_tools(
            process, _build_internal_tools(process),
        )
        self.assertNotIn("declare_task_contract", unavailable)
        self.assertNotIn("request_self_execute", selected)
        self.assertEqual(selected, {})

    def test_caller_cannot_pregrant_supervisor_self_execution(self):
        with self.assertRaisesRegex(ValueError, "must call request_self_execute"):
            _resolve_capability_command({
                "actor_kind": "supervisor",
                "capability_profile_id": "supervisor.control",
                "task_kind": "action",
                "task_id": "task-pregrant",
                "self_execute_request": {
                    "profile_id": "governance.operate",
                    "reason": "caller asks on the model's behalf",
                    "intended_actions": ["formalize"],
                },
            }, default_actor="supervisor")

    def test_supervisor_effectful_tools_require_explicit_lease(self):
        base = CapabilityProfiles.resolve_tools("supervisor.control")
        self.assertNotIn("formalize", base)

        with self.assertRaises(ValueError):
            CapabilityProfiles.issue_self_execute_lease(
                actor_kind="supervisor", task_id="task-lease",
                requested_by="A", profile_id="governance.operate",
                reason="", intended_actions=["formalize"],
            )

        lease = CapabilityProfiles.issue_self_execute_lease(
            actor_kind="supervisor", task_id="task-lease",
            requested_by="A", profile_id="governance.operate",
            reason="single low-risk governance update",
            intended_actions=["formalize"],
        )
        expanded = CapabilityProfiles.resolve_tools(
            "supervisor.control", lease=lease,
        )
        self.assertIn("formalize", expanded)

    def test_request_tool_records_a_task_scoped_lease(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry(
                CapabilityProfiles.resolve_tools("supervisor.control")
            ),
            max_steps=5, task_id="task-explicit",
        ))
        tool = _build_internal_tools(process)["request_self_execute"]
        result = tool({
            "profile_id": "governance.operate",
            "reason": "perform one bounded update",
            "intended_actions": ["formalize selected files"],
        })
        self.assertTrue(result["granted"])
        self.assertEqual(process.capability_lease.task_id, "task-explicit")
        self.assertEqual(process.task_kind, "answer")
        self.assertTrue(process.tool_registry.has("formalize"))

    def test_adaptive_supervisor_exposes_one_workflow_entry_before_contract(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry(
                CapabilityProfiles.resolve_tools("supervisor.control")
            ),
            max_steps=5, task_id="task-uncompiled-route", task_kind="answer",
        ))
        selected, unavailable = _select_authorized_tools(
            process, _build_internal_tools(process),
        )
        self.assertNotIn("declare_task_contract", unavailable)
        self.assertIn("declare_task_contract", selected)
        self.assertNotIn("request_self_execute", selected)
        self.assertNotIn("delegate_task", selected)
        self.assertNotIn("delegate_task_dag", selected)
        self.assertNotIn("delegate_task_bundle", selected)
        self.assertIn("capability_status", selected)
        self.assertIn("configure_capability", selected)
        self.assertIn("list_files", CapabilityProfiles.pre_contract_tools())
        self.assertIn("web_search", CapabilityProfiles.pre_contract_tools())
        self.assertIn("web_fetch", CapabilityProfiles.pre_contract_tools())
        self.assertNotIn("author_tool", selected)
        self.assertNotIn("write_file", selected)
        self.assertNotIn("exec_command", selected)

    def test_manual_b_requirement_keeps_delegation_entry_before_contract(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry(
                CapabilityProfiles.resolve_tools("supervisor.control")
            ),
            max_steps=5, task_id="task-manual-b", task_kind="answer",
            context_snapshot={"task_contract": {
                "host_requirements": {"manual_B_creation": True},
            }},
        ))
        selected, unavailable = _select_authorized_tools(
            process, _build_internal_tools(process),
        )
        self.assertNotIn("declare_task_contract", unavailable)
        self.assertIn("declare_task_contract", selected)
        self.assertIn("delegate_task", selected)
        self.assertNotIn("request_self_execute", selected)

    def test_request_tool_derives_workspace_profile_from_contract(self):
        from backend.core.loop.task_contract import publish_contract

        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry(
                CapabilityProfiles.resolve_tools("supervisor.control")
            ),
            max_steps=5, task_id="task-derived-workspace-lease",
        ))
        publish_contract(process, {
            "goal": "Create one Python file",
            "execution_mode": "self_execute",
            "delegation_required": False,
            "minimum_delegated_outcomes": 0,
            "deliverables": [{
                "kind": "workspace_file", "path": "answer.py",
                "description": "implementation", "required": True,
            }],
            "acceptance_criteria": ["file exists"],
            "estimated_complexity": "bounded",
            "independent_workstreams": 1,
            "routing_transition": "keep_supervisor",
        })
        tool = _build_internal_tools(process)["request_self_execute"]
        result = tool({
            "profile_id": "supervisor.control",
            "reason": "perform the requested bounded workspace change",
            "intended_actions": ["write answer.py", "run its test"],
        })
        self.assertTrue(result["granted"])
        self.assertEqual(result["profile_id"], "development.workspace")
        self.assertEqual(result["profile_normalized_from"], "supervisor.control")
        self.assertTrue(process.tool_registry.has("write_file"))

    def test_task_semantics_promote_only_after_committed_mutation(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5, task_kind="answer",
        ))
        public_read = SimpleNamespace(
            tool_name="web_search", is_error=False,
            receipt={"effect": "external_read", "committed": True},
        )
        self.assertFalse(_apply_host_task_transitions(process, [public_read]))
        self.assertEqual(process.task_kind, "answer")

        write = SimpleNamespace(
            tool_name="edit_file", is_error=False,
            receipt={"effect": "workspace_write", "committed": True},
        )
        self.assertTrue(_apply_host_task_transitions(process, [write]))
        self.assertEqual(process.task_kind, "action")


class PromptAndReasoningStateTests(unittest.TestCase):
    def test_prompt_is_layered_and_does_not_inline_reasoning(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="answer", model_id="mimo-v2.5",
        ))
        process.session.provider_state["secret"] = {
            "reasoning_content": "raw private reasoning"
        }
        text, sections = PromptCompiler.compile(
            process=process, tools={}, workspace_path="C:/workspace",
            governance_brief="high level rule",
        )
        self.assertEqual([s.name for s in sections], [
            "Base identity", "Behavior and delivery standard",
            "Role and authority", "Capabilities",
            "User collaboration protocol",
            "Runtime environment", "Interaction contract",
        ])
        self.assertNotIn("high level rule", text)
        self.assertIn("A greeting should receive a brief greeting", text)
        self.assertIn("mimo-v2.5", text)
        self.assertIn("inside the Gitgo harness", text)
        self.assertNotIn("not exposed", text)
        self.assertNotIn("A-level project supervisor", text)
        self.assertNotIn("raw private reasoning", text)

    def test_provider_identity_is_injected_once_per_route_change(self):
        session = AgentSession()
        event = record_provider_changed(
            session, model_id="mimo-v2.5", protocol="openai_responses",
            provider_route="route-a",
        )
        self.assertEqual(event["model_id"], "mimo-v2.5")
        self.assertIn("Gitgo harness", session.messages[-1]["content"])
        self.assertNotIn("web_search", session.messages[-1]["content"])
        self.assertIsNone(record_provider_changed(
            session, model_id="mimo-v2.5", protocol="openai_responses",
            provider_route="route-a",
        ))
        self.assertEqual(len(session.messages), 1)

        changed = record_provider_changed(
            session, model_id="deepseek-v4-flash", protocol="openai_responses",
            provider_route="route-b",
        )
        self.assertEqual(changed["model_id"], "deepseek-v4-flash")
        self.assertEqual(len(session.messages), 2)

    def test_capability_shortcuts_report_and_apply_only_approved_host_settings(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry(
                CapabilityProfiles.resolve_tools("supervisor.control")
            ),
            max_steps=5, task_id="capability-config", task_kind="answer",
            model_id="mimo-v2.5",
            runtime_preferences={
                "provider_protocol": "openai_responses",
                "provider_capabilities": {
                    "hosted_web_search": False,
                    "probed_at": "2026-09-27T00:00:00Z",
                },
                "web_search_mode": "provider",
                "web_search_endpoint": "",
                "web_search_engine": "duckduckgo",
            },
        ))
        tools = _build_internal_tools(process)
        status = tools["capability_status"].execute({"capability": "web_access"})
        self.assertEqual(status["model"]["model_id"], "mimo-v2.5")
        self.assertEqual(
            status["web_access"]["search_state"], "provider_plan_unavailable",
        )
        self.assertEqual(tools["configure_capability"].approval.value, "ask")
        self.assertTrue(tools["configure_capability"].approval_per_invocation)

        unsupported = tools["configure_capability"].execute({
            "capability": "web_search", "mode": "provider",
        })
        self.assertEqual(unsupported["error"], "PROVIDER_CAPABILITY_UNAVAILABLE")

        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"GITGO_CONFIG_PATH": str(Path(td) / "config.json")},
        ):
            configured = tools["configure_capability"].execute({
                "capability": "web_search", "mode": "searxng",
                "endpoint": "https://search.example.test", "engine": "bing",
            })
            self.assertTrue(configured["configured"])
            self.assertEqual(configured["mode"], "searxng")
            self.assertTrue(configured["endpoint_configured"])
            from backend.core.config import ConfigManager
            saved = ConfigManager.load()
            self.assertEqual(saved.web_search_mode, "searxng")
            self.assertEqual(saved.web_search_engine, "bing")
            self.assertEqual(
                saved.web_search_endpoint, "https://search.example.test",
            )

    def test_declared_self_execute_route_explains_the_pre_lease_surface(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="answer", model_id="mimo-v2.5",
        ))
        process.context_snapshot = {"task_contract": {
            "revision": 1,
            "execution_mode": "self_execute",
            "deliverables": [{
                "kind": "workspace_file", "path": "result.txt", "required": True,
            }],
        }}

        text, _sections = PromptCompiler.compile(
            process=process, tools={}, workspace_path="C:/workspace",
        )

        self.assertIn("Call request_self_execute now", text)
        self.assertIn("pre-lease surface lacks the tool", text)
        self.assertIn("is not evidence that command, file or test capability is unsupported", text)

    def test_current_turn_capability_snapshot_supersedes_stale_history(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="answer",
        ))
        process.context_snapshot = {"task_contract": {
            "revision": 2,
            "execution_mode": "self_execute",
            "routing_compilation": {"next_action": "request_self_execute"},
        }}
        process.session.append_assistant(
            "I cannot write files because no execution tools exist."
        )
        envelope = _build_current_turn_envelope(process, [{
            "type": "function",
            "function": {"name": "request_self_execute", "parameters": {}},
        }])
        wire = process.session.to_provider_messages(dynamic_envelope=envelope)

        self.assertIn("request_self_execute", wire[-1]["content"])
        self.assertIn("supersedes earlier conversation claims", wire[-1]["content"])
        self.assertIn("lease: not-active", wire[-1]["content"])
        self.assertEqual(wire[-1]["message_type"], "host_dynamic_envelope")
        self.assertEqual(
            wire[-2]["content"],
            "I cannot write files because no execution tools exist.",
        )

    def test_pre_contract_snapshot_explains_admission_is_not_final_capability(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="answer", model_id="mimo-v2.5",
        ))
        envelope = _build_current_turn_envelope(process, [
            {"type": "function", "function": {"name": "declare_task_contract"}},
            {"type": "function", "function": {"name": "request_user_decision"}},
        ])

        self.assertIn("surface_phase: contract_admission", envelope)
        self.assertIn("call declare_task_contract now", envelope)
        self.assertIn("safe read-only evidence and public-research", envelope)
        self.assertIn("Host will rebuild the appropriate surface", envelope)
        self.assertNotIn("mimo-v2.5", envelope)
        self.assertNotIn("runtime_identity", envelope)

    def test_current_turn_capability_snapshot_reflects_active_lease(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="action",
        ))
        process.context_snapshot = {"task_contract": {
            "revision": 3,
            "execution_mode": "self_execute",
            "routing_compilation": {"next_action": "continue_owner"},
        }}
        process.capability_lease = SimpleNamespace(
            profile_id="development.workspace",
        )
        envelope = _build_current_turn_envelope(process, [
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "exec_command"}},
        ])

        self.assertIn("lease: development.workspace", envelope)
        self.assertIn("available_tools: exec_command, write_file", envelope)
        self.assertIn("task_kind: action", envelope)

    def test_reasoning_is_plaintext_persisted_and_replayed(self):
        session = AgentSession()
        session.append_assistant_provider(
            "calling tool",
            tool_calls=[{
                "id": "call-1", "type": "function",
                "function": {"name": "scan", "arguments": "{}"},
            }],
            continuation_state={"reasoning_content": "raw reasoning text"},
        )
        wire = session.to_openai_messages()[0]
        self.assertEqual(wire["reasoning_content"], "raw reasoning text")

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp, state_home=Path(tmp) / "state")
            path = store.save_checkpoint("process-reasoning", session)
            self.assertIsNotNone(path)
            cas_bytes = b"\n".join(
                item.read_bytes()
                for item in store.storage_paths.cas_dir.rglob("*")
                if item.is_file()
            )
            self.assertIn(b"raw reasoning text", cas_bytes)
            state = store.load_session_state("process-reasoning")
            self.assertIn("reasoning_content", json.dumps(state["provider_state"]))
            store.close()

    def test_stream_reasoning_details_are_saved_even_without_business_tools(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="worker", actor_kind="worker",
            capability_profile_id="text.only",
            ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_id="task-reasoning-stream", task_kind="answer",
        ))

        class ReasoningProvider:
            def stream_chat(self, *_args, **_kwargs):
                yield {"choices": [{"delta": {
                    "reasoning_content": "raw thought",
                    "reasoning_details": [{"type": "summary", "text": "part"}],
                }}]}
                yield {"choices": [{"delta": {
                    "content": "answer\nTASK_COMPLETE",
                }}]}

        outcome = TaskOutcome.from_dict(agent_step(
            process, ReasoningProvider(), instruction="answer this",
        ))

        self.assertEqual(outcome.status, OutcomeStatus.COMPLETED)
        saved = list(process.session.provider_state.values())[-1]
        self.assertEqual(saved["reasoning_content"], "raw thought")
        self.assertEqual(saved["reasoning_details"][0]["text"], "part")

    def test_plain_answer_is_terminal_without_governance_nudge_or_marker(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.answer",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_id="plain-conversation", task_kind="answer",
        ))

        class PlainAnswerProvider:
            calls = 0

            def stream_chat(self, *_args, **_kwargs):
                self.calls += 1
                yield {"choices": [{"delta": {"content": "你好！有什么我可以帮你？"}}]}

        provider = PlainAnswerProvider()
        outcome = TaskOutcome.from_dict(agent_step(
            process, provider, instruction="你好",
        ))

        self.assertEqual(outcome.status, OutcomeStatus.COMPLETED)
        self.assertEqual(outcome.response, "你好！有什么我可以帮你？")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(outcome.tool_calls_executed, 0)
        self.assertFalse(any(
            message.get("message_type") == "governance_nudge"
            for message in process.session.messages
        ))

    def test_provider_tool_outputs_remain_contiguous_before_storm_nudge(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="worker", actor_kind="worker",
            capability_profile_id="test.tools",
            ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry(["scan", "status"]), max_steps=4,
            task_id="provider-turn-atomicity", task_kind="answer",
        ))
        dispatcher = SimpleNamespace(_executors={
            "scan": _tool("scan"), "status": _tool("status"),
        })

        class Provider:
            calls = 0

            def stream_chat(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "id": "call-scan",
                        "function": {"name": "scan", "arguments": "{}"},
                    }, {
                        "index": 1, "id": "call-status",
                        "function": {"name": "status", "arguments": "{}"},
                    }]}}]}
                    return
                yield {"choices": [{"delta": {"content": "done"}}]}

        def storm_break(_guard, tool_name, _last_error, _args=None):
            return "change strategy" if tool_name == "status" else None

        with patch(
            "backend.core.loop.executor.LoopGuard.check_storm_break",
            new=storm_break,
        ):
            outcome = TaskOutcome.from_dict(agent_step(
                process, Provider(), "inspect", dispatcher=dispatcher,
            ))

        self.assertEqual(outcome.status, OutcomeStatus.COMPLETED)
        assistant_index = next(
            i for i, item in enumerate(process.session.messages)
            if item.get("role") == "assistant"
            and process.session.provider_state.get(item.get("provider_state_id"), {}).get("tool_calls")
        )
        following = process.session.messages[assistant_index + 1:assistant_index + 4]
        self.assertEqual([item.get("role") for item in following], ["tool", "tool", "user"])
        self.assertEqual(
            [item.get("tool_call_id") for item in following[:2]],
            ["call-scan", "call-status"],
        )
        self.assertEqual(following[1]["receipt"]["error_code"], "TOOL_CALL_STORM_BLOCKED")

    def test_historical_delegation_does_not_remove_current_answer_tools(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry(["web_search", "delegate_task"]), max_steps=4,
            task_id="new-answer", task_kind="answer",
        ))
        process.delegated_contracts["old-child"] = {
            "required_for_parent_completion": False,
            "inherited_from_process_id": "old-root",
        }
        process.context_snapshot = {"task_contract": {
            "execution_mode": "answer", "delegation_required": False,
        }}
        catalog = {
            "web_search": _tool("web_search"),
            "delegate_task": _tool("delegate_task"),
        }

        selected, _unavailable = _select_authorized_tools(process, catalog)
        self.assertIn("web_search", selected)

        process.context_snapshot = {"task_contract": {
            "execution_mode": "delegate", "delegation_required": True,
        }}
        selected, _unavailable = _select_authorized_tools(process, catalog)
        self.assertNotIn("web_search", selected)
        self.assertIn("delegate_task", selected)

    def test_supervisor_prompt_routes_bounded_work_to_self_and_iterations_to_owner(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="supervisor", actor_kind="supervisor",
            capability_profile_id="supervisor.control",
            ring_level=RingLevel.RING_0,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="supervisor", task_id="routing-contract",
        ))
        process.runtime_preferences = {"agent_routing": "owner"}

        prompt, _sections = PromptCompiler.compile(
            process=process, tools={}, workspace_path="C:/workspace",
        )

        self.assertIn("default to self-execution for bounded one-owner work", prompt)
        self.assertIn("continue that B by process_id under owner routing", prompt)
        self.assertIn("Do not create a B merely because you are A", prompt)


class HostCompletionTests(unittest.TestCase):
    def _action_process(self):
        return AgentRuntimeFactory.create(RuntimeSpec(
            role="executor", actor_kind="worker",
            capability_profile_id="governance.operate",
            ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry([]), max_steps=5,
            task_kind="action", task_id="task-action",
        ))

    def test_action_requires_claim_effect_receipt_and_evidence(self):
        process = self._action_process()
        denied = HostCompletionEvaluator.evaluate(
            process, "TASK_COMPLETE", [],
        )
        self.assertFalse(denied.allowed)

        process.successful_actions = 1
        process.tool_receipts.append({
            "receipt_id": "receipt-1", "tool_name": "formalize",
            "succeeded": True, "committed": True, "effect": "workspace_write",
        })
        process.completion_claim = CompletionClaim.from_args({
            "result": "updated governance state",
            "verification": [{"receipt_id": "receipt-1"}],
            "files": ["contract.yaml"],
        }, step=2)
        allowed = HostCompletionEvaluator.evaluate(
            process, "TASK_COMPLETE", [],
        )
        self.assertTrue(allowed.allowed)

    def test_action_claim_may_reference_host_nested_composite_receipt(self):
        process = self._action_process()
        process.tool_receipts.append({
            "receipt_id": "composite-parent",
            "succeeded": True, "committed": True,
            "effect": "workspace_write",
            "child_receipts": [{
                "receipt_id": "component-write",
                "succeeded": True, "committed": True,
                "effect": "workspace_write",
            }, {
                "receipt_id": "component-read",
                "succeeded": True, "committed": True,
                "effect": "read",
            }],
        })
        process.completion_claim = CompletionClaim.from_args({
            "result": "created and verified",
            "verification": [
                {"receipt_id": "component-write"},
                {"receipt_id": "component-read"},
            ],
        }, step=2)
        self.assertTrue(
            HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE", []).allowed
        )

    def test_required_manifest_entries_are_hard_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = self._action_process()
            process.worktree_path = tmp
            process.required_test_ids = ["unit:core"]
            process.successful_actions = 1
            process.tool_receipts.append({
                "receipt_id": "receipt-1", "succeeded": True,
                "effect": "workspace_write",
            })
            process.completion_claim = CompletionClaim.from_args({
                "result": "done",
                "verification": [{"receipt_id": "receipt-1"}],
            }, step=2)
            result = HostCompletionEvaluator.evaluate(
                process, "TASK_COMPLETE", [],
            )
            self.assertFalse(result.allowed)
            self.assertIn("unit:core:not_registered", ";".join(result.reasons))

    def test_required_test_cannot_reuse_stale_manifest_without_current_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".gitgo").mkdir()
            (workspace / ".gitgo" / "test_manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "tests": [{
                    "test_id": "unit:core",
                    "target": "tests/test_core.py",
                    "seeds": [17],
                    "results": [{
                        "seed": 17, "exit_code": 0,
                        "duration_ms": 1.0, "output_tail": "passed",
                    }],
                    "last_run_at": "2000-01-01T00:00:00",
                }],
            }), encoding="utf-8")
            process = self._action_process()
            process.worktree_path = str(workspace)
            process.required_test_ids = ["unit:core"]
            process.tool_receipts.append({
                "receipt_id": "effect-1", "tool_name": "define_tool",
                "succeeded": True, "committed": True, "effect": "process",
            })
            process.completion_claim = CompletionClaim.from_args({
                "result": "done", "verification": [{"receipt_id": "effect-1"}],
            }, step=2)
            result = HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE", [])
            self.assertFalse(result.allowed)
            self.assertIn(
                "unit:core:not_run_by_current_process", ";".join(result.reasons),
            )

            process.tool_receipts.append({
                "receipt_id": "test-1", "tool_name": "run_test",
                "succeeded": True, "committed": True, "effect": "process",
                "test_id": "unit:core", "test_passed": True,
                "task_id": "older-task",
            })
            process.completion_claim = CompletionClaim.from_args({
                "result": "done",
                "verification": [
                    {"receipt_id": "effect-1"},
                    {"kind": "test", "test_id": "unit:core", "receipt_id": "test-1"},
                ],
            }, step=3)
            still_stale = HostCompletionEvaluator.evaluate(
                process, "TASK_COMPLETE", []
            )
            self.assertFalse(still_stale.allowed)
            process.tool_receipts[-1]["task_id"] = process.active_task_id
            self.assertTrue(HostCompletionEvaluator.evaluate(
                process, "TASK_COMPLETE", [],
            ).allowed)

    def test_structured_dynamic_evidence_requires_matching_host_receipts(self):
        process = self._action_process()
        process.tool_receipts.append({
            "receipt_id": "define-1", "tool_name": "define_tool",
            "succeeded": True, "committed": True, "effect": "process",
            "dynamic_tool_name": "inspect_note", "definition_digest": "abc",
            "child_receipts": [{
                "receipt_id": "read-1", "tool_name": "read_file",
                "succeeded": True, "committed": True, "effect": "read",
                "composite_tool": "inspect_note", "composite_step": "read_note",
            }],
        })
        process.completion_claim = CompletionClaim.from_args({
            "result": "done",
            "verification": [{
                "kind": "component_step", "step_id": "read_note",
                "tool": "read_file", "receipt": "host-attached",
            }],
        }, step=2)
        denied = HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE", [])
        self.assertFalse(denied.allowed)
        self.assertIn(
            "component_step verification requires a concrete Host receipt_id",
            denied.reasons,
        )

        process.completion_claim = CompletionClaim.from_args({
            "result": "done",
            "verification": [
                {"kind": "dynamic_definition", "name": "inspect_note",
                 "digest": "abc", "receipt_id": "define-1"},
                {"kind": "component_step", "step_id": "read_note",
                 "tool": "read_file", "receipt_id": "read-1"},
            ],
        }, step=3)
        self.assertTrue(
            HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE", []).allowed
        )

    def test_required_tool_call_counts_are_host_enforced(self):
        process = self._action_process()
        process.context_snapshot = {"task_contract": {
            "required_tool_calls": [{
                "tool_name": "inspect_note", "min_calls": 1, "max_calls": 1,
                "include_composite_steps": False,
            }],
        }}
        process.tool_receipts.append({
            "receipt_id": "define-1", "tool_name": "define_tool",
            "succeeded": True, "committed": True, "effect": "process",
        })
        process.completion_claim = CompletionClaim.from_args({
            "result": "done", "verification": [{"receipt_id": "define-1"}],
        }, step=2)
        denied = HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE", [])
        self.assertFalse(denied.allowed)
        self.assertIn(
            "required tool inspect_note has 0 committed successful call(s); minimum is 1",
            denied.reasons,
        )
        process.tool_receipts.append({
            "receipt_id": "inspect-1", "tool_name": "inspect_note",
            "succeeded": True, "committed": True, "effect": "read",
        })
        self.assertTrue(
            HostCompletionEvaluator.evaluate(process, "TASK_COMPLETE", []).allowed
        )

    def test_supervisor_closes_failed_reviewed_delivery_without_step_loop(self):
        manager = AgentProcessManager()
        supervisor_tool_names = CapabilityProfiles.resolve_tools(
            "supervisor.control"
        )
        supervisor_tools = ToolRegistry(supervisor_tool_names)
        dispatcher = SimpleNamespace(_executors={
            name: _tool(name) for name in supervisor_tool_names
        })
        with patch("backend.core.history.HistoryManager.add_operation"):
            supervisor = manager.fork(
                parent_id=None, role="supervisor", actor_kind="supervisor",
                capability_profile_id="supervisor.control",
                tool_registry=supervisor_tools, max_steps=8,
                ring_level=RingLevel.RING_0, task_id="supervision-failure",
                task_kind="supervisor",
            )
            child = manager.fork(
                parent_id=supervisor.process_id, role="executor",
                actor_kind="worker", capability_profile_id="development.workspace",
                tool_registry=ToolRegistry([]), max_steps=4,
                ring_level=RingLevel.RING_3, task_id="supervision-failure:1",
                task_kind="action",
            )
        supervisor.register_child_contract(child.process_id, {
            "required_for_parent_completion": True,
            "superseded_by": "",
        })
        child.status = ProcessStatus.FAILED
        child.result = TaskOutcome.failed(
            task_id=child.active_task_id,
            process_id=child.process_id,
            process_status="failed",
            code="INFRASTRUCTURE_FAILURE",
            message="manifest remained locked",
        ).to_dict()
        supervisor.child_reviews[child.process_id] = {
            "verdict": "changes_required",
            "summary": "Host infrastructure prevented sealing",
        }

        class FailureReportingProvider:
            def stream_chat(self, *_args, **_kwargs):
                yield {"choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-failed-supervision",
                    "function": {
                        "name": "complete_supervision",
                        "arguments": json.dumps({
                            "result": "Delivery failed because the Host could not seal it."
                        }),
                    },
                }]}}]}

        outcome = TaskOutcome.from_dict(agent_step(
            supervisor, FailureReportingProvider(), "finish supervision",
            dispatcher=dispatcher,
        ))
        self.assertEqual(outcome.status, OutcomeStatus.FAILED)
        self.assertEqual(outcome.error.code, "SUPERVISED_DELIVERY_FAILED")
        self.assertEqual(outcome.steps_used, 1)
        self.assertIn("Host could not seal", outcome.response)


class TestManifestTests(unittest.TestCase):
    @staticmethod
    def _record(test_id: str, seed: int = 1) -> TestRecord:
        return TestRecord(
            test_id=test_id,
            target="test_sample.py",
            seeds=[seed],
            results=[SeedResult(seed, 0, 1.0, "passed")],
            last_run_at="now",
        )

    def test_run_test_registers_multiple_seed_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test_sample.py"
            target.write_text(
                "def test_seed_is_available():\n"
                "    import os\n"
                "    assert os.environ['GITGO_TEST_SEED'] in {'11', '22'}\n",
                encoding="utf-8",
            )
            result = run_registered_test(tmp, {
                "test_id": "unit:sample",
                "target": "test_sample.py",
                "seeds": [11, 22],
                "timeout": 30,
            })
            self.assertTrue(result["passed"])
            manifest = TestManifest.load(tmp)
            passed, failures = manifest.evaluate_required(["unit:sample"])
            self.assertTrue(passed)
            self.assertEqual(failures, [])

    def test_separate_seed_runs_merge_without_erasing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test_sample.py"
            target.write_text("def test_ok(): assert True\n", encoding="utf-8")
            for seed in (17, 29):
                result = run_registered_test(tmp, {
                    "test_id": "unit:merged-seeds",
                    "target": "test_sample.py",
                    "seeds": [seed],
                    "timeout": 30,
                })
                self.assertTrue(result["passed"])
            record = TestManifest.load(tmp).records["unit:merged-seeds"]
            self.assertEqual(record.seeds, [17, 29])
            self.assertEqual([item.seed for item in record.results], [17, 29])
            self.assertTrue(record.passed)

    def test_register_reloads_inside_lock_to_preserve_other_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = TestManifest.load(tmp)
            stale_second = TestManifest.load(tmp)
            first.register(self._record("unit:first"))
            stale_second.register(self._record("unit:second"))
            self.assertEqual(
                set(TestManifest.load(tmp).records),
                {"unit:first", "unit:second"},
            )

    def test_atomic_manifest_replace_retries_transient_windows_lock(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            manifest = TestManifest.load(tmp)
            real_replace = os.replace
            attempts = 0

            def temporarily_locked(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts <= 3:
                    error = PermissionError("sharing violation")
                    error.winerror = 5
                    raise error
                return real_replace(source, destination)

            with patch(
                "backend.core.loop.test_manifest.os.replace",
                side_effect=temporarily_locked,
            ), patch("backend.core.loop.test_manifest.time.sleep"):
                manifest.register(self._record("unit:windows-lock"))

            self.assertEqual(attempts, 4)
            self.assertIn(
                "unit:windows-lock", TestManifest.load(tmp).records,
            )


class DurableSessionTests(unittest.TestCase):
    def test_new_process_can_continue_existing_session(self):
        manager = AgentProcessManager()
        with patch("backend.core.history.HistoryManager.add_operation"):
            first = manager.fork(
                parent_id=None, role="supervisor",
                actor_kind="supervisor",
                capability_profile_id="supervisor.control",
                tool_registry=ToolRegistry([]), max_steps=5,
                ring_level=RingLevel.RING_0, task_id="task-one",
            )
            first.session.append_user("preserved context")
            first.status = ProcessStatus.COMPLETED
            second = manager.fork(
                parent_id=None, role="supervisor",
                actor_kind="supervisor",
                capability_profile_id="supervisor.control",
                tool_registry=ToolRegistry([]), max_steps=5,
                ring_level=RingLevel.RING_0, task_id="task-two",
                session_id=first.session.session_id,
            )
        self.assertNotEqual(first.process_id, second.process_id)
        self.assertIs(first.session, second.session)
        self.assertEqual(second.session.messages[-1]["content"], "preserved context")

    def test_schema_and_execution_share_the_same_capability(self):
        process = AgentRuntimeFactory.create(RuntimeSpec(
            role="executor",
            ring_level=RingLevel.RING_3,
            tool_registry=ToolRegistry(["scan"]),
            max_steps=5,
        ))
        authorized, unavailable = _select_authorized_tools(
            process, {"scan": _tool("scan"), "push": _tool("push")},
        )

        self.assertEqual(list(authorized), ["scan"])
        self.assertEqual(unavailable, [])


class TaskOutcomeTests(unittest.TestCase):
    def test_failed_outcome_round_trips_as_data(self):
        original = TaskOutcome.failed(
            task_id="task-3",
            process_id="process-3",
            process_status="failed",
            code="NO_SESSION",
            message="missing session",
        )

        restored = TaskOutcome.from_dict(original.to_dict())
        self.assertEqual(restored.status, OutcomeStatus.FAILED)
        self.assertEqual(restored.error.code, "NO_SESSION")

    def test_completed_outcome_rejects_embedded_error(self):
        raw = {
            "status": "completed",
            "task_id": "task-4",
            "process_id": "process-4",
            "process_status": "completed",
            "error": {"code": "IMPOSSIBLE", "message": "bad"},
        }
        with self.assertRaises(ValueError):
            TaskOutcome.from_dict(raw)


class CompletionCorrelationTests(unittest.TestCase):
    def test_fast_completion_cannot_beat_waiter_registration(self):
        client = DaemonClient("test")
        observed_timeouts = []
        outcome = TaskOutcome(
            task_id="task-fast",
            process_id="process-fast",
            status=OutcomeStatus.COMPLETED,
            process_status="completed",
            response="done",
            llm_used=True,
        ).to_dict()

        def immediate_completion(command, timeout=30.0):
            observed_timeouts.append(timeout)
            task_id = command["task_id"]
            event = {
                "event": "agent_complete",
                "task_id": task_id,
                "process_id": "process-fast",
                "outcome": outcome,
            }
            with client._lock:
                self.assertIn(task_id, client._agent_events)
                client._agent_data[task_id] = event
                client._agent_events[task_id].set()
            return {"task_id": task_id, "process_id": "process-fast"}

        client.send_command = immediate_completion
        result = client.send_task({
            "cmd": "task",
            "action": "chat",
            "task_id": "task-fast",
        }, timeout=0.1)

        self.assertEqual(result["outcome"]["status"], "completed")
        self.assertEqual(observed_timeouts, [0.1])

    def test_task_failure_is_not_raised_as_transport_failure(self):
        client = DaemonClient("test")
        outcome = TaskOutcome.failed(
            task_id="task-failed",
            process_id="process-failed",
            process_status="failed",
            code="NO_SESSION",
            message="missing session",
        ).to_dict()

        def immediate_failure(command, timeout=30.0):
            task_id = command["task_id"]
            event = {
                "event": "agent_complete",
                "task_id": task_id,
                "process_id": "process-failed",
                "outcome": outcome,
            }
            with client._lock:
                client._agent_data[task_id] = event
                client._agent_events[task_id].set()
            return {"task_id": task_id, "process_id": "process-failed"}

        client.send_command = immediate_failure
        result = client.send_task({
            "cmd": "task",
            "action": "chat",
            "task_id": "task-failed",
        }, timeout=0.1)

        self.assertEqual(result["outcome"]["status"], "failed")
        self.assertEqual(result["outcome"]["error"]["code"], "NO_SESSION")


class CancellationTruthTests(unittest.TestCase):
    def test_kill_only_requests_cancellation_until_executor_observes_it(self):
        manager = AgentProcessManager()
        with patch("backend.core.history.HistoryManager.add_operation"):
            process = manager.fork(
                parent_id=None,
                role="executor",
                tool_registry=ToolRegistry([]),
                max_steps=5,
                ring_level=RingLevel.RING_3,
                task_id="task-cancel",
            )
            result = manager.kill(process.process_id)

        self.assertTrue(result["requested"])
        self.assertEqual(process.status, ProcessStatus.CANCELLING)

        class NeverCalledLLM:
            def stream_chat(self, *_args, **_kwargs):
                raise AssertionError("LLM must not run after cancellation")

        outcome = TaskOutcome.from_dict(agent_step(process, NeverCalledLLM()))
        self.assertEqual(outcome.status, OutcomeStatus.CANCELLED)
        self.assertEqual(process.status, ProcessStatus.CANCELLED)


class MailboxTests(unittest.TestCase):
    def test_instruction_is_fifo_and_records_applied_turn(self):
        mailbox = AgentMailbox()
        first = mailbox.enqueue_instruction("first")
        second = mailbox.enqueue_instruction("second")

        drained = mailbox.drain(task_id="task-mailbox", step=4)

        self.assertEqual([m.message_id for m in drained], [
            first.message_id, second.message_id,
        ])
        self.assertEqual(drained[0].status, "applied")
        self.assertEqual(drained[0].applied_task_id, "task-mailbox")
        self.assertEqual(drained[0].applied_at_step, 4)

    def test_completion_barrier_rejects_late_instruction(self):
        mailbox = AgentMailbox()
        self.assertTrue(mailbox.close_if_empty("completed"))
        with self.assertRaises(MailboxClosedError):
            mailbox.enqueue_instruction("too late")

    def test_daemon_instruct_enqueues_instead_of_dispatching_status_tool(self):
        manager = AgentProcessManager()
        with patch("backend.core.history.HistoryManager.add_operation"):
            process = manager.fork(
                parent_id=None,
                role="executor",
                tool_registry=ToolRegistry([]),
                max_steps=5,
                ring_level=RingLevel.RING_3,
                task_id="task-instruct",
            )

        emitted = []
        _cmd_task(
            {"action": "instruct", "process_id": process.process_id,
             "instruction": "check the latest constraint"},
            session=None,
            project=None,
            daemon_ctx={"apm": manager},
            emit=emitted.append,
        )

        result = emitted[-1]["result"]
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(process.mailbox.snapshot()["pending"], 1)
        self.assertEqual(result["instruction"]["status"], "accepted")


class AmbiguousReplayTests(unittest.TestCase):
    def test_background_provider_reuses_foreground_runtime(self):
        existing = object()
        daemon_ctx = {"llm": existing}
        self.assertIs(_background_llm_provider(daemon_ctx), existing)

    def test_background_provider_lazily_resolves_active_config(self):
        configured = SimpleNamespace(
            base_url="https://example.invalid",
            api_key="secret",
            model_id="background-model",
            protocol="openai_responses",
            runtime_capabilities=lambda: {
                "context_window": 128000,
                "max_output_tokens": 4096,
            },
        )
        daemon_ctx = {"llm": None}
        with patch(
            "backend.core.llm_config.LLMConfigManager.get_active",
            return_value=configured,
        ):
            runtime = _background_llm_provider(daemon_ctx)
        self.assertIs(daemon_ctx["llm"], runtime)
        self.assertEqual(runtime._model, "background-model")
        self.assertEqual(runtime.protocol.value, "openai_responses")

    def test_background_harvest_returns_result_through_daemon_queue(self):
        events = queue.Queue()
        daemon_ctx = {}
        lesson = SimpleNamespace(id="lesson-background")

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        batch = [{"signal_id": "signal-1"}]
        with patch(
            "backend.core.knowledge.harvest.lease_harvest_signals",
            return_value=batch,
        ), patch(
            "backend.core.knowledge.harvest.mark_harvest_triggered",
        ) as marked, patch(
            "backend.core.knowledge.harvest.harvest_llm_summary",
            return_value=[lesson],
        ), patch(
            "backend.core.knowledge.lesson.LessonManager.save_pending",
        ) as saved, patch(
            "backend.core.knowledge.harvest.complete_harvest",
        ) as completed:
            started = _start_background_harvest(
                daemon_ctx,
                event_queue=events,
                provider=object(),
                workspace_path="C:/workspace",
                project_name="project",
                signal_type="lesson_trigger",
                thread_factory=ImmediateThread,
            )

        self.assertTrue(started)
        self.assertTrue(daemon_ctx["knowledge_harvest_inflight"].startswith("harvest_"))
        marked.assert_called_once_with("project")
        saved.assert_called_once()
        completed.assert_called_once_with(
            "project", ["signal-1"], ["lesson-background"],
        )
        result = events.get_nowait()
        self.assertEqual(result["event"], "knowledge_harvest_result")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 1)

    def test_background_harvest_does_not_start_a_second_worker(self):
        daemon_ctx = {"knowledge_harvest_inflight": "harvest-existing"}
        with patch(
            "backend.core.knowledge.harvest.lease_harvest_signals",
        ) as lease:
            started = _start_background_harvest(
                daemon_ctx,
                event_queue=queue.Queue(),
                provider=object(),
                workspace_path="C:/workspace",
                project_name="project",
                signal_type="lesson_trigger",
            )
        self.assertFalse(started)
        lease.assert_not_called()

    def test_background_harvest_releases_lease_when_worker_cannot_start(self):
        events = queue.Queue()
        daemon_ctx = {}

        def broken_thread(**_kwargs):
            raise RuntimeError("thread unavailable")

        with patch(
            "backend.core.knowledge.harvest.lease_harvest_signals",
            return_value=[{"signal_id": "signal-1"}],
        ), patch(
            "backend.core.knowledge.harvest.mark_harvest_triggered",
        ), patch(
            "backend.core.knowledge.harvest.fail_harvest",
        ) as failed:
            started = _start_background_harvest(
                daemon_ctx,
                event_queue=events,
                provider=object(),
                workspace_path="C:/workspace",
                project_name="project",
                signal_type="lesson_trigger",
                thread_factory=broken_thread,
            )

        self.assertFalse(started)
        self.assertNotIn("knowledge_harvest_inflight", daemon_ctx)
        failed.assert_called_once_with("project", ["signal-1"], "thread unavailable")
        self.assertEqual(events.get_nowait()["reason"], "worker_start_failed")

    def test_native_task_admission_publishes_resolved_llm_for_background_work(self):
        manager = AgentProcessManager()
        events = queue.Queue()

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        command = {
            "action": "chat",
            "task_id": "task-publish-provider",
            "instruction": "perform one bounded action",
            "context_snapshot": {},
        }
        project = SimpleNamespace(name="test-project")

        with tempfile.TemporaryDirectory(prefix="gitgo_provider_publish_") as root:
            workspace = Path(root)
            with StorageRuntime(
                workspace, state_home=workspace / "state",
            ) as storage:
                daemon_ctx = {
                    "apm": manager,
                    "dispatcher": SimpleNamespace(_executors={}),
                    "evq": events,
                    "llm": None,
                    "storage": storage,
                }
                session = SimpleNamespace(workspace_path=workspace)
                with patch(
                    "backend.core.daemon.dispatch._resolve_llm_config",
                    return_value=(
                        "https://example.invalid", "secret", "test-model",
                        "openai_chat", {},
                    ),
                ), patch(
                    "backend.core.daemon.dispatch.threading.Thread", ImmediateThread,
                ), patch(
                    "backend.core.loop.executor.agent_step",
                    side_effect=RuntimeError("stop after admission"),
                ), patch(
                    "backend.core.history.HistoryManager.add_operation",
                ), patch(
                    "backend.core.daemon.dispatch._save_session_checkpoint",
                ):
                    _cmd_task(
                        command,
                        session=session,
                        project=project,
                        daemon_ctx=daemon_ctx,
                        emit=lambda _event: None,
                    )

        self.assertIsNotNone(daemon_ctx["llm"])
        self.assertEqual(daemon_ctx["llm"]._model, "test-model")

    def test_task_thread_crash_is_not_automatically_replayed(self):
        manager = AgentProcessManager()
        events = queue.Queue()
        emitted = []
        calls = []

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        def crash_once(*_args, **_kwargs):
            calls.append("called")
            raise RuntimeError("ambiguous crash after possible side effect")

        command = {
            "action": "chat",
            "task_id": "task-no-replay",
            "instruction": "perform one effect",
            "context_snapshot": {},
        }
        project = SimpleNamespace(name="test-project")

        # Construct storage before replacing threading.Thread: subprocess uses
        # real reader threads while resolving a Git project identity.  More
        # importantly, the test must never trace into the repository running
        # the suite.
        with tempfile.TemporaryDirectory(prefix="gitgo_no_replay_") as root:
            workspace = Path(root)
            with StorageRuntime(
                workspace, state_home=workspace / "state",
            ) as storage:
                daemon_ctx = {
                    "apm": manager,
                    "dispatcher": SimpleNamespace(_executors={}),
                    "evq": events,
                    "llm": object(),
                    "storage": storage,
                }
                session = SimpleNamespace(workspace_path=workspace)
                with patch("backend.core.history.HistoryManager.add_operation"), \
                     patch("backend.core.daemon.dispatch.threading.Thread",
                           ImmediateThread), \
                     patch("backend.core.loop.executor.agent_step", crash_once), \
                     patch("backend.core.daemon.dispatch._save_session_checkpoint"):
                    _cmd_task(
                        command,
                        session=session,
                        project=project,
                        daemon_ctx=daemon_ctx,
                        emit=emitted.append,
                    )

        self.assertEqual(calls, ["called"])
        completion = events.get_nowait()
        outcome = completion["outcome"]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["error"]["code"], "TASK_THREAD_CRASHED")
        self.assertFalse(outcome["error"]["retryable"])
        self.assertTrue(outcome["metadata"]["execution_ambiguous"])
        self.assertTrue(
            outcome["metadata"]["automatic_replay_suppressed"],
        )


if __name__ == "__main__":
    unittest.main()
