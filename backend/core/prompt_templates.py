"""Bundled prompt text; callers retain state, selection and authorization.

Resources are release-owned and independent of cwd. The user config selects
declared semantic instances; stable shells remain shared. A running process
caches resources; restart it after editing files. Missing/broken templates
are explicit errors, never an inline fallback or a project-provided override.
Markdown resources use explicit .md identifiers; extensionless legacy identifiers
select .txt only. The loader never probes alternate formats.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources
import re
from string import Template


class PromptTemplateError(RuntimeError):
    """A bundled prompt or its caller's variable contract is invalid."""


@lru_cache(maxsize=256)
def _template(name: str) -> tuple[Template, frozenset[str]]:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+(?:/[a-z0-9_]+)*(?:\.md)?", name):
        raise PromptTemplateError("invalid prompt resource identifier")
    if name.startswith("profiles/"):
        raise PromptTemplateError("instance fragments are not public prompt resources")
    try:
        text = resources.files("backend").joinpath(
            "resources", "prompts", name if name.endswith(".md") else name + ".txt",
        ).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PromptTemplateError(f"bundled prompt unavailable: {name}") from exc
    template = Template(text)
    fields = set()
    for match in template.pattern.finditer(text):
        if match.group("invalid") is not None:
            raise PromptTemplateError(f"invalid placeholder in prompt: {name}")
        field = match.group("named") or match.group("braced")
        if field:
            fields.add(field)
    instance_fields = {field for field in fields if field.startswith("_instance_")}
    if instance_fields:
        from .prompt_profiles import registry
        if instance_fields != set(registry()["slots"].get(name, {})):
            raise PromptTemplateError(f"undeclared prompt instance slots: {name}")
    return template, frozenset(fields - instance_fields)


def render_prompt(name: str, **values: object) -> str:
    """Substitute declared slots once; inserted data is never reinterpreted."""
    template, fields = _template(name)
    if fields != values.keys():
        missing = sorted(fields - values.keys())
        unexpected = sorted(values.keys() - fields)
        raise PromptTemplateError(
            f"prompt variables mismatch: {name}; missing={missing}; unexpected={unexpected}",
        )
    if any(field.startswith("_instance_") for field in values):
        raise PromptTemplateError("instance slots are not caller-supplied facts")
    if "_instance_" in template.template:
        from .prompt_profiles import instance_values
        values = {**values, **instance_values(name, template)}
    return template.substitute(values)


def load_prompt(name: str) -> str:
    """Load a prompt without dynamic slots, preserving its original whitespace."""
    return render_prompt(name)

_clear_templates = _template.cache_clear
def clear_prompt_cache():
    _clear_templates()
    from .prompt_profiles import clear_profile_cache
    clear_profile_cache()
_template.cache_clear = clear_prompt_cache
