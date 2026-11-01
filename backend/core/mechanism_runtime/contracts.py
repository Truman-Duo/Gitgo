"""Developer registration contracts; user configuration contains JSON only."""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Callable


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", value):
        raise ValueError("MECHANISM_IDENTIFIER_INVALID")
    return value


def bounded_json(value, *, maximum=65536):
    if len(encoded(value)) > maximum:
        raise ValueError("MECHANISM_VALUE_CAPACITY")
    def walk(item, depth):
        if depth > 32:
            raise ValueError("MECHANISM_VALUE_DEPTH")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("MECHANISM_JSON_KEYS_REQUIRED")
            for child in item.values():
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif item is not None and type(item) not in (bool, str, int, float):
            raise ValueError("MECHANISM_JSON_VALUE_REQUIRED")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("MECHANISM_FINITE_NUMBER_REQUIRED")
    walk(value, 0)
    return deepcopy(value)


def validate_schema(schema):
    from jsonschema import Draft202012Validator
    bounded_json(schema)
    if not isinstance(schema, dict):
        raise ValueError("MECHANISM_SCHEMA_REQUIRED")
    pending = [schema]
    while pending:
        part = pending.pop()
        if isinstance(part, dict):
            # No resolver/network access or recursive indirection in the
            # exposed schema profile. Registered nominal ports share types.
            if any(key in part for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                raise ValueError("MECHANISM_SCHEMA_REFERENCE_UNSUPPORTED")
            pending.extend(part.values())
        elif isinstance(part, list):
            pending.extend(part)
    Draft202012Validator.check_schema(schema)


def validate(value, schema):
    from jsonschema import Draft202012Validator
    bounded_json(value, maximum=262144)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: str(error.path))
    if errors:
        # Do not expose the value in diagnostics: parameters can contain
        # project-private text, though credentials are never catalog values.
        error = errors[0]
        raise ValueError("MECHANISM_SCHEMA_MISMATCH: " + "/".join(map(str, error.path)) + " " + error.validator)


@dataclass(frozen=True)
class Port:
    name: str
    type_id: str
    required: bool = True

    def record(self):
        return {"name": self.name, "type": self.type_id, "required": self.required}


@dataclass(frozen=True)
class Element:
    identifier: str
    version: int
    configuration: dict
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]
    boundaries: tuple[str, ...]
    handler: Callable = field(repr=False, compare=False)
    state_schema: dict = field(default_factory=lambda: {"type": "object"})
    effect: str = "pure"
    capabilities: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ("task",)
    dependencies: tuple[tuple[str, int], ...] = ()
    conflicts: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    entry: bool = False
    implementation: str = ""
    timeout_seconds: float = 30
    failure: str = "block"
    cancellation: str = "cooperative"
    recovery: str = "reconcile"
    boundary_parameter: str = ""
    description: str = ""
    reconciler: Callable | None = field(default=None, repr=False, compare=False)
    recovery_capabilities: tuple[str, ...] = ()
    reconciliation_implementation: str = ""

    def record(self):
        record = {"id": self.identifier, "version": self.version, "configuration": deepcopy(self.configuration),
            "state_schema": deepcopy(self.state_schema),
            "inputs": [p.record() for p in self.inputs], "outputs": [p.record() for p in self.outputs],
            "boundaries": list(self.boundaries), "effect": self.effect, "capabilities": list(self.capabilities),
            "scopes": list(self.scopes), "dependencies": dict(self.dependencies), "conflicts": list(self.conflicts),
            "writes": list(self.writes), "entry": self.entry, "implementation": self.implementation,
            "timeout_seconds": self.timeout_seconds, "failure": self.failure,
            "cancellation": self.cancellation, "recovery": self.recovery, "boundary_parameter": self.boundary_parameter,
            "description": self.description}
        # Preserve legacy descriptors/digests when no recovery adapter exists.
        if self.reconciler is not None:
            record['reconciliation'] = {'implementation': self.reconciliation_implementation,
                'capabilities': list(self.recovery_capabilities), 'disposition': 'observe_then_terminate_without_replay'}
        return record
