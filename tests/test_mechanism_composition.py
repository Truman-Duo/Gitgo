"""Behavior and failure tests through the real project CAS/KV implementation.

Fixtures are component tests, not real-provider acceptance or user approval.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from backend.core.storage import StorageRuntime
from backend.core.mechanism_runtime.contracts import Element, Port
from backend.core.mechanism_runtime.registry import Registry
from backend.core.mechanism_runtime.repository import Repository
from backend.core.mechanism_runtime.compiler import compile_program
from backend.core.mechanism_runtime.coordinator import Coordinator
from backend.core.mechanism_runtime.runner import Runner, NodeResult, RecoveryResult


EMPTY = {"type": "object", "additionalProperties": False}


@pytest.fixture
def composition(tmp_path_factory):
    work = tmp_path_factory / "work"
    work.mkdir()
    with StorageRuntime(work, state_home=tmp_path_factory / "state") as storage:
        registry = Registry()
        for name, schema in {"event": {"type": "object"}, "boolean": {"type": "boolean"},
                             "trigger": {"const": True}, "text": {"type": "string"}}.items():
            registry.register_type(name, schema)
        def source(context, params, inputs):
            return NodeResult({"selected": inputs["event"]["selected"], "text": inputs["event"].get("text", "")})
        registry.register(Element("event", 1, EMPTY, (Port("event", "event"),),
            (Port("selected", "boolean"), Port("text", "text")), ("before_tool",), source, entry=True))
        def record(context, params, inputs):
            context.call("projection", {"label": params["label"], "value": inputs.get("text", "")})
            return NodeResult(state={"count": context.state.get("count", 0) + 1})
        schema = {"type": "object", "properties": {"label": {"type": "string", "default": "first"}}, "additionalProperties": False}
        registry.register(Element("record", 1, schema, (Port("go", "trigger"), Port("text", "text", False)), (),
            ("before_tool",), record, effect="projection", capabilities=("projection",)))
        repo = Repository(storage)
        yield registry, repo, Coordinator(registry, repo, capabilities=("projection",))


def program(*, selected=True, label="first", target="a", version=1):
    return {"schema_version": 2, "id": "reusable", "intent": "fixture: conditionally record a value", "source_refs": ["user:fixture"],
        "elements": [{"id": "source", "use": "event"},
                     {"id": "a", "use": "record", "version": version, "parameters": {"label": label}},
                     {"id": "b", "use": "record", "parameters": {"label": "other"}}],
        "bindings": [{"kind": "condition", "from": {"node": "source", "port": "selected"},
                      "to": {"node": target, "port": "go"}, "when": selected},
                     {"kind": "condition", "from": {"node": "source", "port": "selected"},
                      "to": {"node": "b" if target == "a" else "a", "port": "go"}, "when": not selected}]}


def install(composition, raw=None, *, task="task", name="fixture", policy="follow_compatible", constraints=()):
    registry, repo, coordinator = composition
    compiled = coordinator.preview(raw or program())["compiled"]
    _, head = repo.definition(compiled["id"])
    definition, reference = repo.publish(compiled, {"user:fixture": {"kind": "unit_fixture"}}, expected_head=head)
    repo.create_instance(task, name, definition, reference, policy=policy, sources=definition["sources"], constraints=constraints)
    coordinator.request(task, name, reference, expected_revision=1, sources=definition["sources"])
    return coordinator.settle(task, name)


def test_binding_revision_changes_real_route_and_removed_data_stops_propagating(composition):
    registry, repo, coordinator = composition
    install(composition)
    observations = []
    runner = Runner(registry, repo, {"projection": lambda ctx, value: observations.append((ctx.epoch, value)) or {}})
    runner.dispatch("task", "fixture", "one", "before_tool", {"selected": True})
    assert observations == [(1, {"label": "first", "value": ""})]
    revised = program(target="b")
    revised["bindings"].append({"kind": "data", "from": {"node": "source", "port": "text"}, "to": {"node": "b", "port": "text"}})
    compiled = coordinator.preview(revised)["compiled"]
    _, head = repo.definition("reusable")
    definition, ref = repo.publish(compiled, {"user:fixture": {}}, expected_head=head)
    coordinator.request("task", "fixture", ref, expected_revision=2, sources=definition["sources"])
    coordinator.settle("task", "fixture")
    runner.dispatch("task", "fixture", "two", "before_tool", {"selected": True, "text": "real data"})
    assert observations[-1] == (2, {"label": "other", "value": "real data"})
    assert repo.instance("task", "fixture")[0]["states"] == {"source": {}, "a": {"count": 1}, "b": {"count": 1}}
    assert runner.dispatch("task", "fixture", "two", "before_tool", {"selected": True, "text": "real data"})["phase"] == "complete"
    assert len(observations) == 2


@pytest.mark.parametrize("case", ["type", "missing_port", "cycle", "unknown_param", "capability", "dependency", "conflict", "write"])
def test_invalid_compositions_fail_before_execution(composition, case):
    registry, _, coordinator = composition
    raw = program()
    if case == "type":
        raw["bindings"][0]["kind"] = "data"
        raw["bindings"][0].pop("when")
    elif case == "missing_port":
        raw["bindings"][0]["to"]["port"] = "missing"
    elif case == "cycle":
        def cyclic(context, params, inputs): return NodeResult({"out": inputs["event"]})
        registry.register(Element("cyclic", 1, EMPTY, (Port("event", "event"),), (Port("out", "event"),),
            ("before_tool",), cyclic, entry=True))
        raw["elements"] = [{"use": "cyclic"}]
        raw["bindings"] = [{"kind": "data", "from": {"node": "cyclic", "port": "out"}, "to": {"node": "cyclic", "port": "event"}}]
    elif case == "unknown_param":
        raw["elements"][1]["parameters"]["code"] = "arbitrary code"
    elif case == "capability":
        coordinator = Coordinator(registry, composition[1])
    else:
        def callback(*args): return NodeResult()
        kwargs = {"dependencies": (("uninstalled", 1),)} if case == "dependency" else {"conflicts": ("event",)} if case == "conflict" else {"writes": ("shared.projection",)}
        registry.register(Element("extension", 1, EMPTY, (), (), ("before_tool",), callback, **kwargs))
        raw["elements"].append({"use": "extension"})
        if case == "write":
            raw["elements"].append({"id": "second", "use": "extension"})
    with pytest.raises(ValueError):
        coordinator.preview(raw)
    if case == "cycle":
        with pytest.raises(ValueError, match="UNBOUNDED_CYCLE"):
            coordinator.preview(raw)


def test_stale_preview_cannot_compile_against_new_catalog(composition):
    registry, _, coordinator = composition
    raw = program()
    raw["catalog_version"] = registry.catalog()["version"]
    registry.register_type("new_type", {"type": "integer"})
    with pytest.raises(ValueError, match="CATALOG_CHANGED"):
        coordinator.preview(raw)


def test_same_instance_duplicate_concurrent_event_has_one_effect(composition):
    registry, repo, _ = composition
    install(composition)
    entered, release = threading.Event(), threading.Event()
    effects = []
    def effect(ctx, value):
        effects.append(value)
        entered.set()
        assert release.wait(4)
        return {}
    runner = Runner(registry, repo, {"projection": effect})
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(runner.dispatch, "task", "fixture", "duplicate", "before_tool", {"selected": True})
        assert entered.wait(4)
        duplicate = runner.dispatch("task", "fixture", "duplicate", "before_tool", {"selected": True})
        assert duplicate["phase"] == "running"
        release.set()
        assert first.result()["phase"] == "complete"
    assert len(effects) == 1


def test_external_effect_with_lost_reply_blocks_without_replay_even_after_reopen(composition):
    registry, repo, coordinator = composition
    install(composition)
    effects = []
    def uncertain(ctx, value):
        effects.append(value)
        raise OSError("reply lost after effect")
    runner = Runner(registry, repo, {"projection": uncertain})
    with pytest.raises(OSError):
        runner.dispatch("task", "fixture", "uncertain", "before_tool", {"selected": True})
    reopened = Repository(repo.storage)
    recovered = Coordinator(registry, reopened, capabilities=("projection",)).reconcile("task", "fixture")
    assert recovered["phase"] == "blocked" and recovered["error"]["effect_state"] == "unknown"
    with pytest.raises(ValueError, match="NOT_READY"):
        Runner(registry, reopened, {"projection": uncertain}).dispatch("task", "fixture", "uncertain", "before_tool", {"selected": True})
    assert len(effects) == 1


def test_active_call_retains_old_version_update_waits_and_next_call_uses_new(composition):
    registry, repo, coordinator = composition
    install(composition)
    entered, release = threading.Event(), threading.Event()
    observations = []
    def effect(ctx, payload):
        observations.append((ctx.epoch, payload["label"]))
        if not release.is_set():
            entered.set()
            assert release.wait(4)
        return {}
    runner = Runner(registry, repo, {"projection": effect})
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(runner.dispatch, "task", "fixture", "old", "before_tool", {"selected": True})
        assert entered.wait(4)
        compiled = coordinator.preview(program(label="new"))["compiled"]
        _, head = repo.definition("reusable")
        definition, reference = repo.publish(compiled, {"user:fixture": {}}, expected_head=head)
        coordinator.request("task", "fixture", reference, expected_revision=2, sources=definition["sources"])
        assert coordinator.settle("task", "fixture")["phase"] == "pending_activation"
        release.set()
        assert running.result()["epoch"] == 1
    assert coordinator.settle("task", "fixture")["epoch"] == 2
    runner.dispatch("task", "fixture", "new", "before_tool", {"selected": True})
    assert observations == [(1, "first"), (2, "new")]


def test_follow_compatible_updates_other_task_pin_keeps_old_and_constraints_cannot_drop(composition):
    registry, repo, coordinator = composition
    first = install(composition)
    definition, ref = repo.definition("reusable")
    repo.create_instance("pinned", "fixture", definition, ref, policy="pinned", sources=definition["sources"],
        constraints=[{"id": "required", "required_elements": ["event"]}])
    coordinator.request("pinned", "fixture", ref, expected_revision=1, sources=definition["sources"])
    coordinator.settle("pinned", "fixture")
    compiled = coordinator.preview(program(label="revised"))["compiled"]
    repo.publish(compiled, {"user:fixture": {}}, expected_head=ref)
    report = coordinator.maintain()
    assert {v["status"] for v in report["affected"]} == {"pending", "pinned"}
    assert all(v["affected_nodes"] == ["a"] for v in report["affected"])
    assert next(v for v in report["affected"] if v["task_id"] == "task")["phase"] == "ready"
    assert repo.instance("task", "fixture")[0]["definition_revision"] == 2
    assert repo.instance("pinned", "fixture")[0]["definition_revision"] == 1
    current = repo.instance("pinned", "fixture")[0]
    with pytest.raises(ValueError, match="DETACH_DENIED"):
        coordinator.request("pinned", "fixture", ref, expected_revision=current["revision"], action="detach", sources=definition["sources"])


def test_registry_retirement_waits_for_real_invocation_and_awaits_cleanup(composition):
    registry, _, _ = composition
    async def scenario():
        cleaned = []
        async def dispose():
            await asyncio.sleep(0)
            cleaned.append("done")
        def handler(*args): return NodeResult()
        unregister = registry.register(Element("resource", 1, EMPTY, (), (), ("before_tool",), handler), dispose=dispose)
        descriptor = registry.resolve("resource")
        with registry.lease("resource", 1, descriptor["digest"]):
            pending = asyncio.create_task(unregister())
            await asyncio.sleep(0.02)
            assert not pending.done() and not cleaned
            with pytest.raises(ValueError): registry.resolve("resource")
        await pending
        await unregister()
        assert cleaned == ["done"]
    asyncio.run(scenario())


def test_failed_async_disposal_is_visible_and_duplicate_retirements_do_not_replay(composition):
    registry, _, _ = composition
    async def scenario():
        calls = []
        async def dispose():
            calls.append("effect")
            await asyncio.sleep(0.01)
            raise OSError("cleanup result unknown")
        def handler(*args): return NodeResult()
        unregister = registry.register(Element("cleanup_failure", 1, EMPTY, (), (), ("before_tool",), handler), dispose=dispose)
        results = await asyncio.gather(unregister(), unregister(), return_exceptions=True)
        assert all(isinstance(result, Exception) for result in results)
        assert calls == ["effect"]
        assert registry.catalog()["diagnostics"][0]["phase"] == "failed"
        with pytest.raises(RuntimeError, match="DISPOSAL_FAILED"):
            await unregister()
        assert calls == ["effect"]
    asyncio.run(scenario())


def test_missing_component_service_blocks_initial_activation_without_callback(composition):
    registry,repo,coordinator=composition
    compiled=coordinator.preview(program())['compiled']
    definition,reference=repo.publish(compiled,{'user:fixture':{'kind':'unit_fixture'}},expected_head=None)
    repo.create_instance('task','not-initialized',definition,reference,policy='pinned',sources=definition['sources'])
    missing=Coordinator(registry,repo,capabilities=())
    missing.request('task','not-initialized',reference,expected_revision=1,sources=definition['sources'])
    with pytest.raises(ValueError,match='ACTIVATION_SERVICE_MISSING'):
        missing.settle('task','not-initialized')
    value=repo.instance('task','not-initialized')[0]
    assert value['phase']=='blocked' and value['epoch']==0
    assert value['activation']['phase']=='blocked' and not value['runs'] and not value['states']
    with pytest.raises(ValueError,match='NOT_READY'):
        Runner(registry,repo,{}).dispatch('task','not-initialized','one','before_tool',{'selected':True})


def test_interrupted_preparation_recovers_one_activation_without_running_callbacks(composition, monkeypatch):
    registry, repo, coordinator = composition
    value = install(composition)
    definition, ref = repo.definition("reusable")
    staged = coordinator.request("task", "fixture", ref, expected_revision=value["revision"], sources=definition["sources"])
    actual = repo.mutate
    def interrupt(task, instance, change, **kwargs):
        result = actual(task, instance, change, **kwargs)
        if change.__name__ == "preparing":
            raise SystemExit("test: process stopped after durable prepare")
        return result
    monkeypatch.setattr(repo, "mutate", interrupt)
    with pytest.raises(SystemExit):
        coordinator.settle("task", "fixture")
    assert repo.instance("task", "fixture")[0]["phase"] == "activating"
    monkeypatch.setattr(repo, "mutate", actual)
    recovered = Coordinator(registry, Repository(repo.storage), capabilities=("projection",)).reconcile("task", "fixture")
    assert recovered["phase"] == "ready" and recovered["epoch"] == value["epoch"] + 1
    assert not recovered["runs"] and not recovered["states"]


def test_incompatible_follow_update_is_proposed_not_silently_applied(composition):
    registry, repo, coordinator = composition
    before = install(composition)
    def new_handler(context, params, inputs): return NodeResult(state={"new": True})
    registry.register(Element("record", 2, EMPTY, (Port("go", "trigger"),), (), ("before_tool",), new_handler))
    raw = program(version=2)
    raw["elements"][1]["parameters"] = {}
    raw["elements"][2]["version"] = 1
    compiled = coordinator.preview(raw)["compiled"]
    _, head = repo.definition("reusable")
    repo.publish(compiled, {"user:fixture": {}}, expected_head=head)
    report = coordinator.maintain()
    assert report["affected"][0]["status"] == "needs_review"
    current = repo.instance("task", "fixture")[0]
    assert current["effective_ref"] == before["effective_ref"] and current["epoch"] == before["epoch"]
    assert len(current["maintenance"]) == 1
    coordinator.maintain()
    assert len(repo.instance("task", "fixture")[0]["maintenance"]) == 1


def test_many_events_compact_live_journal_but_old_deliveries_never_replay(composition):
    registry, repo, _ = composition
    install(composition)
    effects = []
    runner = Runner(registry, repo, {"projection": lambda ctx, payload: effects.append(ctx.event_id) or {}})
    for i in range(40):
        runner.dispatch("task", "fixture", str(i), "before_tool", {"selected": True})
    assert len(repo.instance("task", "fixture")[0]["runs"]) < 32
    assert runner.dispatch("task", "fixture", "0", "before_tool", {"selected": True})["phase"] == "complete"
    assert len(effects) == 40
    with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
        runner.dispatch("task", "fixture", "0", "before_tool", {"selected": False})


def test_real_source_bound_application_projects_into_actual_provider_envelope_and_detaches(tmp_path_factory):
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.models import RingLevel
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.task_maintenance import source_receipts
    from backend.core.loop.provider_route_runtime import ProviderRouteRuntime
    from backend.core.mechanism_runtime.application import Application
    manager = AgentProcessManager()
    with closing(SessionStore(tmp_path_factory, state_home=tmp_path_factory / "state", migrate_legacy=False)) as store:
        manager.storage = store._storage
        manager.session_store = store
        process = manager.fork(None, "supervisor", ToolRegistry(["harness_mechanism"]), 20, RingLevel.RING_0,
            workspace_path=str(tmp_path_factory), task_id="current-task", actor_kind="supervisor", capability_profile_id="supervisor.control")
        process.session.append_user("Fixture: compose a reusable note before model requests, activate it here, then disable it.")
        app = Application(process)
        process._harness_application = app
        sources = list(source_receipts(process))
        raw = {"schema_version": 2, "id": "note", "intent": "fixture reusable context note", "source_refs": sources,
            "elements": [{"id": "entry", "use": "runtime.event", "parameters": {"boundaries": ["before_context"]}},
                         {"id": "note", "use": "context.project", "parameters": {"content": "Preserve the documented public interface."}}],
            "bindings": [{"kind": "trigger", "from": {"node": "entry", "port": "go"}, "to": {"node": "note", "port": "go"}}]}
        with pytest.raises(ValueError, match="ACTUAL_USER_SOURCE_REQUIRED"):
            app.invoke({"operation": "preview", "program": {**raw, "source_refs": ["user:invented"]}})
        preview = app.invoke({"operation": "preview", "program": raw})
        with pytest.raises(ValueError, match="PREVIEW_STALE"):
            app.invoke({"operation": "publish", "program": raw, "expected_head": None, "preview_digest": "invented"})
        from backend.core.loop.protocol_tools import harness_composition
        from backend.core.loop.execution_context import ExecutionContext
        from backend.core.loop.tool_pipeline import ToolPipeline
        bindings = SimpleNamespace(process=process, llm_provider=None, on_stream_event=None, tools={}, manager=manager, dispatcher=None)
        harness_composition.install(bindings)
        publish_args = {"operation": "publish", "program": raw, "expected_head": None, "preview_digest": preview["preview_digest"]}
        result = ToolPipeline().execute({"name": "harness_mechanism", "args": publish_args},
            bindings.tools["harness_mechanism"], ExecutionContext(process, process.session, str(tmp_path_factory), storage=store._storage), "publish-fixture", 0)
        assert not result.is_error, result.formatted
        published = result.data
        assert published["phase"] == "saved" and not app.invoke({"operation": "status"})["instances"]
        active = app.invoke({"operation": "instantiate", "definition_id": "note", "version_policy": "pinned", "source_refs": sources})["instance"]
        assert active["phase"] == "ready"
        # Nonmatching lifecycle boundaries do not produce empty journal growth.
        app.dispatch("before_tool", "unsubscribed", {"tool_name": "read_file"})
        assert not app.repo.instance(app.task, "note")[0]["runs"]
        app.dispatch("before_context", "provider-request-one", {"task_id": app.task})
        route = ProviderRouteRuntime(process, process.session, None, "", None, lambda: None)
        route.prepare_envelope([])
        wire = process.session.to_provider_messages(dynamic_envelope=route.envelope)
        assert "Preserve the documented public interface." in json_text(wire)
        assert app.task == "current-task"
        app.invoke({"operation": "detach", "instance_id": "note", "source_refs": sources, "expected_revision": active["revision"]})
        route.prepare_envelope([])
        assert "Preserve the documented public interface." not in route.envelope


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize("schema_type,value,accepted", [(["string", "null"], None, True),
    (["string", "null"], "sha256:fixture", True), (["string", "null"], 3, False),
    ("null", None, True), ("string", None, False), (["number", "null"], 3, True),
    (["number", "null"], True, False)])
def test_tool_parameter_type_union_matches_json_types(schema_type, value, accepted):
    from backend.core.loop.tool_pipeline import _validate_args
    schema = {"type": "object", "properties": {"head": {"type": schema_type}}, "required": ["head"]}
    if accepted:
        _validate_args({"head": value}, schema)
    else:
        with pytest.raises(ValueError):
            _validate_args({"head": value}, schema)
