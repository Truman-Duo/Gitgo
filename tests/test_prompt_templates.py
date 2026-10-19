"""Runtime resource contracts, data substitution and packaged import boundaries."""
import json
from pathlib import Path
from string import Template
import subprocess
import sys
import zipfile

import pytest

from backend.core import prompt_templates
from backend.core.prompt_templates import PromptTemplateError, load_prompt, render_prompt


def test_every_bundled_resource_has_valid_slots_and_preserves_inserted_data():
    root = Path(__file__).resolve().parents[1] / "backend/resources/prompts"
    paths = [path for path in root.rglob("*") if path.suffix in {".txt", ".md"} and path.name != "README.md" and "profiles" not in path.relative_to(root).parts]
    assert paths and any(path.suffix == ".md" for path in paths)
    for path in paths:
        name = path.relative_to(root).as_posix() if path.suffix == ".md" else path.relative_to(root).with_suffix("").as_posix()
        _, fields = prompt_templates._template(name)
        data = "literal ${not_a_slot} {json} 中文"
        text = render_prompt(name, **{field: data for field in fields})
        assert all(data in text for _ in fields), name


def test_slot_contract_rejects_missing_and_unexpected_arguments_without_echoing_values():
    with pytest.raises(PromptTemplateError, match="missing=.*model_id"):
        load_prompt("identity/answer")
    with pytest.raises(PromptTemplateError, match="unexpected=.*secret") as caught:
        render_prompt("identity/answer", model_id="fixture", secret="private-value")
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("name", ["../identity/answer", "identity/../answer", "/identity/answer", "C:/prompt", "identity\\answer"])
def test_resource_names_cannot_escape_the_package(name):
    with pytest.raises(PromptTemplateError, match="invalid prompt resource identifier"):
        load_prompt(name)


def test_missing_or_invalid_resource_is_an_explicit_error(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(prompt_templates.resources, "files", lambda package: tmp_path_factory)
    prompt_templates._template.cache_clear()
    try:
        with pytest.raises(PromptTemplateError, match="bundled prompt unavailable"):
            load_prompt("missing")
        root = tmp_path_factory / "resources/prompts"
        root.mkdir(parents=True)
        (root / "invalid.txt").write_text("broken ${slot", encoding="utf-8")
        with pytest.raises(PromptTemplateError, match="invalid placeholder"):
            load_prompt("invalid")
    finally:
        prompt_templates._template.cache_clear()


def test_working_directory_cannot_override_a_bundled_prompt(tmp_path_factory, monkeypatch):
    expected = load_prompt("completion/answer")
    forged = tmp_path_factory / "backend/resources/prompts/completion"
    forged.mkdir(parents=True)
    (forged / "answer.txt").write_text("untrusted override", encoding="utf-8")
    monkeypatch.chdir(tmp_path_factory)
    prompt_templates._template.cache_clear()
    assert load_prompt("completion/answer") == expected


def test_packaged_resources_load_in_a_fresh_process_from_an_unrelated_directory(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    archive = tmp_path_factory / "installed backend.zip"
    assets = root / "backend/resources/prompts"
    names = sorted(path.relative_to(assets).as_posix() if path.suffix == ".md" else path.relative_to(assets).with_suffix("").as_posix()
                   for path in assets.rglob("*") if path.suffix in {".txt", ".md"} and path.name != "README.md" and "profiles" not in path.relative_to(assets).parts)
    assert any(name.endswith(".md") for name in names)
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("backend/__init__.py", "")
        package.writestr("backend/core/__init__.py", "")
        package.write(root / "backend/core/prompt_templates.py", "backend/core/prompt_templates.py")
        package.write(root / "backend/core/prompt_profiles.py", "backend/core/prompt_profiles.py")
        for path in assets.rglob("*"):
            if path.is_file():
                package.write(path, "backend/resources/prompts/" + path.relative_to(assets).as_posix())
    unrelated = tmp_path_factory / "unrelated cwd"
    unrelated.mkdir()
    code = ("import sys,json; sys.path.insert(0,sys.argv[1]); "
            "from backend.core import prompt_templates as p; "
            "from backend.core.prompt_profiles import prompt_scope; "
            "names=json.loads(sys.argv[2]); "
            "\nwith prompt_scope('workbench'):\n "
            "texts=[p.render_prompt(n,**{f:'literal ${data}' for f in p._template(n)[1]}) for n in names]; "
            "assert all(texts); print(len(texts))")
    result = subprocess.run([sys.executable, "-B", "-c", code, str(archive), json.dumps(names)],
                            cwd=unrelated, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(len(names))


def test_resource_content_change_uses_existing_hash_and_append_only_update(monkeypatch):
    from backend.core.loop.models import AgentProcess, RingLevel
    from backend.core.loop.session import AgentSession
    from backend.core.loop.prompt_compiler import PromptCompiler
    session = AgentSession()
    process = AgentProcess("fixture", "supervisor", RingLevel.RING_0, actor_kind="supervisor",
                           task_kind="answer", session=session)
    text, sections = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
    PromptCompiler.upsert(session, text, sections)
    original_message = session.messages[0]["content"]
    original_hash = session.compiled_prompt_hash
    original_loader = prompt_templates._template
    def changed(name):
        if name == "policy/behavior":
            return Template("A reviewed replacement behavior instruction."), frozenset()
        return original_loader(name)
    monkeypatch.setattr(prompt_templates, "_template", changed)
    text, sections = PromptCompiler.compile(process=process, tools={}, workspace_path="fixture")
    PromptCompiler.upsert(session, text, sections)
    assert session.compiled_prompt_hash != original_hash
    assert session.messages[0]["content"] == original_message
    assert any("A reviewed replacement behavior instruction." in item.get("content", "")
               for item in session.messages[2:])


def test_markdown_identifier_is_explicit_and_never_probes_another_format(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(prompt_templates.resources, "files", lambda package: tmp_path_factory)
    root = tmp_path_factory / "resources/prompts/fixture"
    root.mkdir(parents=True)
    (root / "help.md").write_text("# Help\n\nValue: ${value}", encoding="utf-8")
    prompt_templates._template.cache_clear()
    try:
        assert render_prompt("fixture/help.md", value="literal ${data}") == "# Help\n\nValue: literal ${data}"
        with pytest.raises(PromptTemplateError, match="bundled prompt unavailable"):
            load_prompt("fixture/help")
        (root / "other.txt").write_text("legacy text", encoding="utf-8")
        with pytest.raises(PromptTemplateError, match="bundled prompt unavailable"):
            load_prompt("fixture/other.md")
        for name in ("fixture/help.md/../../other", "fixture/help.md.txt", "../help.md", "profiles/default/writing_style.md"):
            with pytest.raises(PromptTemplateError):
                load_prompt(name)
    finally:
        prompt_templates._template.cache_clear()
