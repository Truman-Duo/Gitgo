"""Keyboard configuration validation, using the frontend's action catalog."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def action_catalog() -> dict:
    path = Path(__file__).resolve().parents[1] / "resources" / "keyboard" / "actions.json"
    return {row["id"]: row for row in json.loads(path.read_text(encoding="utf-8"))}


def validate_keyboard_bindings(value: object) -> dict:
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1 or not isinstance(value.get("overrides"), dict)
            or set(value) != {"version", "overrides"}):
        raise ValueError("INPUT_BINDING_VERSION_UNSUPPORTED")
    catalog = action_catalog()
    named = {"escape", "return", "tab", "upArrow", "downArrow", "leftArrow", "rightArrow",
             "pageUp", "pageDown", "backspace", "delete", "home", "end"}
    overrides = {}
    for action, binding in value["overrides"].items():
        if (action not in catalog or not isinstance(binding, dict)
                or set(binding) - {"key", "ctrl", "shift", "meta", "super"}
                or not isinstance(binding.get("key"), str)
                or any(type(binding[k]) is not bool for k in ("ctrl", "shift", "meta", "super") if k in binding)):
            raise ValueError(f"INPUT_BINDING_INVALID: {action}")
        key = binding["key"]
        if key not in named and not (len(key) == 1 and 32 <= ord(key) <= 126):
            raise ValueError(f"INPUT_BINDING_INVALID: {action}")
        if ("*" in catalog[action]["contexts"] and len(key) == 1
                and not any(binding.get(k) for k in ("ctrl", "meta", "super"))):
            raise ValueError(f"INPUT_BINDING_TEXT_CONFLICT: {action}")
        overrides[action] = {**binding, "key": key.lower() if len(key) == 1 else key}
    effective = {action: overrides.get(action, row["default"]) for action, row in catalog.items()}
    def signature(binding):
        return (binding["key"].lower(), *(bool(binding.get(k)) for k in ("ctrl", "shift", "meta", "super")))

    def variants(action, binding):
        return ([binding, *catalog[action].get("aliases", [])]
                if signature(binding) == signature(catalog[action]["default"]) else [binding])

    entries = list(effective.items())
    for i, (action, binding) in enumerate(entries):
        for other, other_binding in entries[:i]:
            contexts, other_contexts = catalog[action]["contexts"], catalog[other]["contexts"]
            overlap = "*" in contexts or "*" in other_contexts or bool(set(contexts) & set(other_contexts))
            if overlap and any(signature(a) == signature(b) for a in variants(action, binding)
                               for b in variants(other, other_binding)):
                raise ValueError(f"INPUT_BINDING_CONFLICT: {action} / {other}")
    return {"version": 1, "overrides": overrides}
