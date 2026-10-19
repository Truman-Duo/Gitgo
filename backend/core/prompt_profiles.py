"""Release-owned semantic instances; stable prompt shells own contracts."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
from importlib import resources
import hashlib
import json
import re
from types import MappingProxyType
from .prompt_templates import PromptTemplateError

_selected = ContextVar("gitgo_prompt_profile", default=None)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")


@lru_cache(maxsize=1)
def registry():
    try:
        value = json.loads(resources.files("backend").joinpath("resources", "prompts", "profiles.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PromptTemplateError("prompt instance registry unavailable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or set(value) != {"schema_version", "slots", "profiles"}:
        raise PromptTemplateError("invalid prompt instance registry")
    slots, profiles = value["slots"], value["profiles"]
    if not isinstance(slots, dict) or not isinstance(profiles, dict) or not profiles or "default" not in profiles:
        raise PromptTemplateError("invalid prompt instance registry")
    for name, fields in slots.items():
        if not re.fullmatch(r"[a-z0-9_]+(?:/[a-z0-9_]+)*", name) or name.startswith("profiles/") or not isinstance(fields, dict):
            raise PromptTemplateError("invalid prompt instance resource")
        if not fields or any(not key.startswith("_instance_") or not isinstance(part, str) or not _IDENTIFIER.fullmatch(part)
                             for key, part in fields.items()):
            raise PromptTemplateError("invalid prompt instance slot")
    for name, info in profiles.items():
        if (not _IDENTIFIER.fullmatch(name) or not isinstance(info, dict)
                or set(info) != {"label", "description"} or any(not isinstance(v, str) or not 1 <= len(v) <= 300 for v in info.values())):
            raise PromptTemplateError("invalid prompt instance identity")
    return value


def snapshot(profile_id):
    if not isinstance(profile_id, str) or not _IDENTIFIER.fullmatch(profile_id):
        raise PromptTemplateError("invalid prompt instance identity")
    return _snapshot(profile_id)


@lru_cache(maxsize=32)
def _snapshot(profile_id):
    info = registry()
    if profile_id not in info["profiles"]:
        raise PromptTemplateError("unknown prompt instance")
    names = {part for fields in info["slots"].values() for part in fields.values()}
    folder = resources.files("backend").joinpath("resources", "prompts", "profiles", profile_id)
    try:
        actual = {item.name for item in folder.iterdir()}
        if actual != {name + ".txt" for name in names}:
            raise PromptTemplateError("prompt instance must contain exactly the declared semantic fragments")
        fragments = {name: folder.joinpath(name + ".txt").read_text(encoding="utf-8") for name in sorted(names)}
    except (OSError, UnicodeError) as exc:
        raise PromptTemplateError("prompt instance unavailable") from exc
    if any(len(text) > 16000 for text in fragments.values()):
        raise PromptTemplateError("prompt instance fragment exceeds limit")
    # No external paths, inheritance, per-resource replacement or silent fallback.
    digest = hashlib.sha256(json.dumps({"id": profile_id, "fragments": fragments},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return profile_id, MappingProxyType(fragments), digest


def configured_profile():
    from .config import ConfigManager
    # Read the authoritative user config without implicit cwd legacy migration.
    return ConfigManager.load(ConfigManager.default_path(), strict=True).prompt_profile


def current_snapshot():
    return _selected.get() or snapshot(configured_profile())


@contextmanager
def prompt_scope(profile_id=None):
    current = _selected.get()
    chosen = current if profile_id is None and current is not None else snapshot(
        configured_profile() if profile_id is None else profile_id)
    token = _selected.set(chosen)
    try:
        yield chosen
    finally:
        _selected.reset(token)


def with_prompt_profile(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with prompt_scope():
            return function(*args, **kwargs)
    return wrapped


def list_profiles():
    return [{"id": key, **info, "digest": snapshot(key)[2]}
            for key, info in registry()["profiles"].items()]


def instance_values(name, template):
    fields = {match.group("named") or match.group("braced") for match in template.pattern.finditer(template.template)}
    selected = {key: part for key, part in registry()["slots"].get(name, {}).items() if key in fields}
    if not selected:
        return {}
    _, fragments, _ = current_snapshot()
    return {key: fragments[part] for key, part in selected.items()}


def clear_profile_cache():
    registry.cache_clear()
    _snapshot.cache_clear()
