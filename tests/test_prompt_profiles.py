"""Global prompt choices pin semantic instances without replacing core contracts."""
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.core.config import ConfigManager
from backend.core.application.services import ApplicationServices, OperationError
from backend.core.prompt_templates import load_prompt, render_prompt, PromptTemplateError, _template
from backend.core.prompt_profiles import prompt_scope, list_profiles, snapshot
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.prompt_compiler import PromptCompiler


@pytest.fixture
def instance_workspace(tmp_path_factory, monkeypatch):
    from backend.core import prompt_templates
    root = tmp_path_factory / "installed"
    shutil.copytree(Path(__file__).resolve().parents[1] / "backend/resources/prompts", root / "resources/prompts")
    monkeypatch.setattr(prompt_templates.resources, "files", lambda package: root)
    monkeypatch.setenv("GITGO_CONFIG_PATH", str(tmp_path_factory / "settings.json"))
    _template.cache_clear()
    try:
        yield root / "resources/prompts"
    finally:
        _template.cache_clear()


def edit_instance(root, part, text):
    (root / "profiles/workbench" / (part + ".txt")).write_bytes(text.encode())
    _template.cache_clear()


def choose(profile):
    return ApplicationServices().config_set("prompt_profile", profile)


def test_global_setting_switches_whole_instance_and_round_trips(instance_workspace):
    root = instance_workspace
    edit_instance(root, "writing_style", "Experimental presentation guidance.")
    service = ApplicationServices()
    assert service.config_get()["prompt_profile"] == "default"
    assert {item["id"] for item in service.config_get()["prompt_profiles"]} == {"default", "workbench"}
    baseline = load_prompt("policy/behavior")
    choose("workbench")
    selected = load_prompt("policy/behavior")
    assert "Experimental presentation guidance." in selected and selected != baseline
    assert "Never claim actions, tests, certainty" in selected
    assert "privacy filtering does not prove" in selected
    assert ConfigManager.load().prompt_profile == "workbench"
    choose("default")
    assert load_prompt("policy/behavior") == baseline


@pytest.mark.parametrize("value", [None, 1, {}, [], "../default", "unknown"])
def test_invalid_choice_does_not_mutate_authoritative_config(instance_workspace, value):
    choose("default")
    before = ConfigManager.default_path().read_bytes()
    with pytest.raises(OperationError, match="prompt instance"):
        choose(value)
    assert ConfigManager.default_path().read_bytes() == before


def test_stable_roles_and_protocol_do_not_follow_instance_edits(instance_workspace):
    def stable_text(name):
        return render_prompt(name, **{field: "observed facts" for field in _template(name)[1]})
    stable = {name: stable_text(name) for name in ["identity/supervisor",
        "routing/self_execute_active", "completion/action", "alignment/assessor", "tools/request_permission",
        "runtime/capability_snapshot", "recovery/unfinished_tool"]}
    edit_instance(instance_workspace, "writing_style", "Different style.")
    choose("workbench")
    assert {name: stable_text(name) for name in stable} == stable
    assert render_prompt("identity/answer", model_id="actual-model").endswith(
        "Never hide an available identifier or guess an inaccessible vendor or version detail.")


@pytest.mark.parametrize("change", ["missing", "undeclared"])
def test_incomplete_or_core_replacement_instance_is_rejected(instance_workspace, change):
    folder = instance_workspace / "profiles/workbench"
    if change == "missing":
        (folder / "writing_style.txt").unlink()
    else:
        (folder / "completion_action.txt").write_bytes(b"pretend success")
    _template.cache_clear()
    with pytest.raises(PromptTemplateError, match="exactly the declared"):
        snapshot("workbench")


def test_instance_slots_cannot_be_supplied_by_a_model_or_project_file(instance_workspace, tmp_path_factory, monkeypatch):
    with pytest.raises(PromptTemplateError):
        render_prompt("policy/behavior", _instance_writing_style="injected authority")
    with pytest.raises(PromptTemplateError, match="not public"):
        load_prompt("profiles/workbench/writing_style")
    fake = tmp_path_factory / "cwd/backend/resources/prompts/profiles/workbench"
    fake.mkdir(parents=True)
    (fake / "writing_style.txt").write_bytes(b"workspace override")
    monkeypatch.chdir(fake.parent.parent.parent.parent)
    choose("workbench")
    assert "workspace override" not in load_prompt("policy/behavior")


def test_instance_text_and_dynamic_data_are_substituted_only_once(instance_workspace):
    edit_instance(instance_workspace, "answer_presentation", "Literal ${model_id} ${invented} {json}.")
    choose("workbench")
    rendered = render_prompt("identity/answer", model_id="observed ${not_expanded}")
    assert "Literal ${model_id} ${invented} {json}." in rendered
    assert "active model is observed ${not_expanded}" in rendered


def test_running_scope_remains_pinned_and_next_scope_observes_saved_choice(instance_workspace):
    edit_instance(instance_workspace, "writing_style", "New instance style.")
    with prompt_scope("default") as original:
        before = load_prompt("policy/behavior")
        choose("workbench")
        assert load_prompt("policy/behavior") == before
        with prompt_scope() as nested:
            assert nested == original
    assert "New instance style." in load_prompt("policy/behavior")


def test_parallel_scopes_do_not_change_each_others_instance(instance_workspace):
    edit_instance(instance_workspace, "writing_style", "Parallel experiment style.")
    def render(profile):
        with prompt_scope(profile):
            return profile, load_prompt("policy/behavior"), load_prompt("policy/behavior")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(render, ["default", "workbench"] * 8))
    assert all(first == second for _, first, second in results)
    assert all(("Parallel experiment style." in first) == (profile == "workbench") for profile, first, _ in results)


def test_existing_session_receives_changed_instance_without_rewriting_prefix(instance_workspace):
    edit_instance(instance_workspace, "writing_style", "Changed response strategy after a user switch.")
    session = AgentSession()
    process = AgentProcess("fixture", "supervisor", RingLevel.RING_0, actor_kind="supervisor",
                           task_kind="answer", session=session)
    original, sections = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
    PromptCompiler.upsert(session, original, sections)
    old_message = session.messages[0]["content"]
    old_hash = session.compiled_prompt_hash
    choose("workbench")
    new, sections = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
    PromptCompiler.upsert(session, new, sections)
    assert session.compiled_prompt_hash != old_hash
    assert session.messages[0]["content"] == old_message
    assert any("Changed response strategy" in item.get("content", "") for item in session.messages[2:])


def test_harvest_pins_instance_across_bounded_recovery(instance_workspace):
    from backend.core.knowledge.summary import summarize
    from backend.core.loop.provider_protocol import ProviderResponseIncomplete
    edit_instance(instance_workspace, "harvest_strategy", "Different experimental harvest method.")
    class Provider:
        max_output_tokens = 8192
        prompts = []
        def chat(self, messages, **kwargs):
            self.prompts.append(messages[0]["content"])
            if len(self.prompts) == 1:
                choose("workbench")
                raise ProviderResponseIncomplete("max_output_tokens")
            return "[]"
    provider, report = Provider(), {}
    assert summarize([{"signal_id": "real"}], provider, str(instance_workspace), "fixture", report=report) == []
    assert provider.prompts[0] == provider.prompts[1]
    assert "Different experimental harvest method." not in provider.prompts[0]
    assert report["prompt_profile"]["id"] == "default" and len(report["prompt_profile"]["digest"]) == 64


@pytest.mark.parametrize("kind,actor", [
    ("answer", "supervisor"), ("action", "worker"), ("supervisor", "supervisor"),
    ("plan", "worker"), ("review", "reviewer"),
])
def test_delivery_instance_reaches_all_task_completion_routes(instance_workspace, kind, actor):
    edit_instance(instance_workspace, "answer_delivery", "Delivery strategy selected for this run.")
    choose("workbench")
    process = AgentProcess("fixture", actor, RingLevel.RING_0, actor_kind=actor,
                           task_kind=kind, session=AgentSession())
    text, sections = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
    assert text.count("Delivery strategy selected for this run.") == 1
    behavior = next(section.content for section in sections if section.name == "Behavior and delivery standard")
    assert "Delivery strategy selected for this run." in behavior
    with prompt_scope("default"):
        baseline, _ = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
    assert "Delivery strategy selected for this run." not in baseline


def test_btw_assembly_and_execution_share_instance_when_config_changes(instance_workspace, monkeypatch):
    from types import SimpleNamespace
    from backend.core.daemon import dispatch
    from backend.core.loop import executor
    from backend.core.prompt_profiles import current_snapshot, with_prompt_profile

    edit_instance(instance_workspace, "btw_strategy", "Workbench-only side discussion strategy.")
    choose("default")
    original_load = dispatch.load_prompt
    observed, events = [], []

    def switch_after_assembly(name):
        text = original_load(name)
        if name == "runtime/btw":
            choose("workbench")
        return text

    @with_prompt_profile
    def step(process, instruction, **kwargs):
        observed.append((current_snapshot()[0], instruction))
        return {"status": "completed", "response": "Side question answered."}

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(dispatch, "load_prompt", switch_after_assembly)
    monkeypatch.setattr(dispatch.threading, "Thread", InlineThread)
    monkeypatch.setattr(executor, "agent_step", step)
    ctx = {"apm": SimpleNamespace(storage=None), "llm": object(), "dispatcher": object()}
    dispatch._cmd_task({"action": "btw", "question": "Explain this choice", "sidecar_id": "profile-boundary"},
        SimpleNamespace(workspace_path=instance_workspace), SimpleNamespace(name="fixture"), ctx, events.append)

    assert len(observed) == 1
    selected, instruction = observed[0]
    assert selected == "default"
    assert "Workbench-only side discussion strategy." not in instruction
    assert any(event.get("event") == "btw_complete" for event in events)
    assert ConfigManager.load().prompt_profile == "workbench"
    assert current_snapshot()[0] == "workbench"


def test_review_method_is_pinned_without_replacing_role_or_completion(instance_workspace):
    edit_instance(instance_workspace, "review_strategy", "Workbench review method marker.")
    choose("workbench")
    process = AgentProcess("review-fixture", "reviewer", RingLevel.RING_0,
                           actor_kind="reviewer", task_kind="review", session=AgentSession())
    with prompt_scope("workbench"):
        text, _ = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
        role = load_prompt("identity/reviewer").split("\n\n", 1)[0]
        completion = load_prompt("completion/review")
        choose("default")
        pinned, _ = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
        assert pinned == text
    baseline, _ = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
    assert "Workbench review method marker." in text and "Workbench review method marker." not in baseline
    assert role in baseline and role in text
    assert completion in baseline and completion in text


@pytest.mark.parametrize("platform,expected,other", [
    ("win32", "auto selection on this platform is powershell", "profile is currently unavailable on this platform"),
    ("linux", "auto selection on this platform is bash", "WindowsPowerShell/powershell.exe"),
])
def test_shell_guidance_uses_execution_platform_without_changing_schema(instance_workspace, monkeypatch, platform, expected, other):
    from types import SimpleNamespace
    from backend.core.tools import catalog
    original = catalog.build_workspace_tools(instance_workspace)["shell_script"]
    monkeypatch.setattr(catalog, "sys", SimpleNamespace(platform=platform))
    current = catalog.build_workspace_tools(instance_workspace)["shell_script"]
    assert expected in current.description and other not in current.description
    assert current.parameters == original.parameters
    assert current.approval == original.approval and current.approval_per_invocation
    assert current.runner_name == original.runner_name
    if platform == "win32":
        assert "$env:NAME" in current.description and "$$env:NAME" not in current.description


def test_workspace_guidance_is_loaded_by_registration_not_copied_into_instances(instance_workspace, monkeypatch):
    from backend.core.tools import catalog
    before = catalog.build_workspace_tools(instance_workspace)
    original = catalog.load_prompt
    def replacement(name):
        return "Resource guidance marker." if name == "tools/read_file" else original(name)
    monkeypatch.setattr(catalog, "load_prompt", replacement)
    after = catalog.build_workspace_tools(instance_workspace)
    assert after["read_file"].description == "Resource guidance marker."
    assert after["read_file"].parameters == before["read_file"].parameters
    assert after["read_file"].effect == before["read_file"].effect
    for name in before.keys() - {"read_file"}:
        assert after[name].description == before[name].description


@pytest.mark.parametrize("surface,implementation", [("none", False), ("read", False), ("write", True)])
def test_guidance_mounts_with_actual_surface_and_authority_stays_in_stable_prefix(instance_workspace, surface, implementation):
    from backend.core.tools.catalog import build_workspace_tools
    tools = build_workspace_tools(instance_workspace)
    mounted = {} if surface == "none" else {name: tools[name] for name in
        (["read_file"] if surface == "read" else ["read_file", "edit_file"])}
    session = AgentSession()
    process = AgentProcess("scope-fixture", "supervisor", RingLevel.RING_0,
                           actor_kind="supervisor", task_kind="answer", session=session)
    text, sections = PromptCompiler.compile(process=process, tools=mounted, workspace_path="fixture")
    names = {s.name for s in sections}
    assert ("Tool use and feedback" in names) == bool(mounted)
    assert ("Implementation safety" in names) == implementation
    PromptCompiler.upsert(session, text, sections)
    assert "Source authority" in {s["name"] for s in session.messages[0]["prompt_sections"]}
    assert "Tool use and feedback" not in {s["name"] for s in session.messages[0]["prompt_sections"]}
