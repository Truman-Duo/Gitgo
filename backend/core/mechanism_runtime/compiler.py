"""One bounded compiler for preview and activation; edges carry real semantics."""
from __future__ import annotations

from copy import deepcopy
from .contracts import bounded_json, digest, identifier, validate


def _parameters(raw, schema):
    value = deepcopy(raw)
    if isinstance(value, dict):
        for key, prop in schema.get("properties", {}).items():
            if key not in value and "default" in prop:
                value[key] = deepcopy(prop["default"])
            if key in value:
                value[key] = _parameters(value[key], prop)
    validate(value, schema)
    return value


def compile_program(raw, registry, *, capabilities=(), scope="task"):
    bounded_json(raw, maximum=262144)
    if not isinstance(raw, dict) or set(raw) - {"schema_version", "id", "intent", "source_refs", "elements", "bindings", "catalog_version", "scope"}:
        raise ValueError("MECHANISM_PROGRAM_FIELDS_INVALID")
    if raw.get("schema_version") != 2:
        raise ValueError("MECHANISM_PROGRAM_VERSION_UNSUPPORTED")
    identifier(raw.get("id"))
    intent = raw.get("intent")
    sources = raw.get("source_refs")
    if not isinstance(intent, str) or not intent.strip() or len(intent) > 4000:
        raise ValueError("MECHANISM_INTENT_REQUIRED")
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 32
            or any(not isinstance(s, str) or not s or len(s) > 180 for s in sources)):
        raise ValueError("MECHANISM_USER_SOURCES_REQUIRED")
    if scope not in {"task", "session", "project"} or raw.get("scope", scope) != scope:
        raise ValueError("MECHANISM_SCOPE_MISMATCH")
    directory = registry.catalog()
    if raw.get("catalog_version", directory["version"]) != directory["version"]:
        raise ValueError("MECHANISM_CATALOG_CHANGED")
    items = raw.get("elements")
    bindings = raw.get("bindings", [])
    if not isinstance(items, list) or not 1 <= len(items) <= 64 or not isinstance(bindings, list) or len(bindings) > 128:
        raise ValueError("MECHANISM_GRAPH_CAPACITY")
    nodes, descriptors = {}, {}

    def add(item):
        if not isinstance(item, dict) or set(item) - {"id", "use", "version", "parameters", "on"}:
            raise ValueError("MECHANISM_NODE_INVALID")
        name = identifier(item.get("id", item.get("use")))
        if name in nodes:
            raise ValueError("MECHANISM_NODE_DUPLICATE")
        matches = [e for e in directory["elements"] if e["id"] == item.get("use")
                   and (item.get("version") is None or e["version"] == item["version"])]
        if not matches:
            raise ValueError("MECHANISM_ELEMENT_UNAVAILABLE: " + str(item.get("use")))
        element = deepcopy(max(matches, key=lambda e: e["version"]))
        if scope not in element["scopes"]:
            raise ValueError("MECHANISM_ELEMENT_SCOPE_DENIED")
        if not set(element["capabilities"]).issubset(capabilities):
            raise ValueError("MECHANISM_CAPABILITY_UNAVAILABLE: " + element["id"])
        if not set(element.get('reconciliation', {}).get('capabilities', [])).issubset(capabilities):
            raise ValueError('MECHANISM_RECOVERY_CAPABILITY_UNAVAILABLE: ' + element['id'])
        parameters = _parameters(item.get("parameters", {}), element["configuration"])
        boundaries = parameters.get(element["boundary_parameter"]) if element["boundary_parameter"] else element["boundaries"]
        if not isinstance(boundaries, list) or not boundaries or not set(boundaries).issubset(element["boundaries"]):
            raise ValueError("MECHANISM_BOUNDARY_INVALID")
        if 'on' in item:
            selected = item['on']
            if (not isinstance(selected, list) or not selected or any(not isinstance(b, str) for b in selected)
                    or len(set(selected)) != len(selected) or not set(selected).issubset(boundaries)):
                raise ValueError('MECHANISM_CALL_BOUNDARY_INVALID')
            boundaries = selected
        nodes[name] = {"id": name, "use": element["id"], "version": element["version"], "contract_digest": element["digest"],
                       "entry": element["entry"],
                       "boundaries": list(boundaries),
                       "interface": {key: deepcopy(element[key]) for key in ("configuration", "state_schema", "inputs", "outputs", "boundaries", "effect", "capabilities", "scopes")},
                       "parameters": parameters}
        if 'on' in item:
            nodes[name]['on'] = list(boundaries)
        descriptors[name] = element

    for item in items:
        add(item)
    pending = list(nodes)
    while pending:
        node = pending.pop(0)
        for name, version in descriptors[node]["dependencies"].items():
            if any(n["use"] == name and n["version"] == version for n in nodes.values()):
                continue
            if len(nodes) >= 64:
                raise ValueError("MECHANISM_DEPENDENCY_CAPACITY")
            key = "dependency." + name
            if key in nodes:
                raise ValueError("MECHANISM_DEPENDENCY_ID_CONFLICT")
            add({"id": key, "use": name, "version": version})
            pending.append(key)
    uses = {n["use"] for n in nodes.values()}
    writers = {}
    for name, element in descriptors.items():
        if set(element["conflicts"]) & uses:
            raise ValueError("MECHANISM_ELEMENT_CONFLICT")
        for field in element["writes"]:
            if field in writers:
                raise ValueError("MECHANISM_EXCLUSIVE_WRITE_CONFLICT: " + field)
            writers[field] = name

    edges = {name: set() for name in nodes}
    inputs, compiled = {}, []
    for raw_edge in bindings:
        if not isinstance(raw_edge, dict) or set(raw_edge) - {"kind", "from", "to", "when"}:
            raise ValueError("MECHANISM_BINDING_INVALID")
        kind = raw_edge.get("kind")
        if kind not in {"data", "trigger", "condition"}:
            raise ValueError("MECHANISM_BINDING_KIND_REQUIRED")
        for endpoint in (raw_edge.get("from"), raw_edge.get("to")):
            if not isinstance(endpoint, dict) or set(endpoint) != {"node", "port"} or endpoint["node"] not in nodes:
                raise ValueError("MECHANISM_BINDING_ENDPOINT_INVALID")
        source, target = raw_edge["from"], raw_edge["to"]
        output = next((p for p in descriptors[source["node"]]["outputs"] if p["name"] == source["port"]), None)
        input_port = next((p for p in descriptors[target["node"]]["inputs"] if p["name"] == target["port"]), None)
        if not output or not input_port:
            raise ValueError("MECHANISM_PORT_MISSING")
        if not set(nodes[source["node"]]["boundaries"]) & set(nodes[target["node"]]["boundaries"]):
            raise ValueError("MECHANISM_BINDING_BOUNDARY_MISMATCH")
        if kind == "data" and output["type"] != input_port["type"]:
            raise ValueError("MECHANISM_BINDING_TYPE_MISMATCH")
        if kind == "trigger" and (output["type"] != "trigger" or input_port["type"] != "trigger"):
            raise ValueError("MECHANISM_TRIGGER_TYPE_MISMATCH")
        if kind == "condition" and (output["type"] != "boolean" or input_port["type"] != "trigger" or type(raw_edge.get("when", True)) is not bool):
            raise ValueError("MECHANISM_CONDITION_TYPE_MISMATCH")
        if kind != "condition" and "when" in raw_edge:
            raise ValueError("MECHANISM_BINDING_CONDITION_UNEXPECTED")
        key = (target["node"], target["port"])
        if key in inputs:
            raise ValueError("MECHANISM_INPUT_MULTIPLE_WRITERS")
        inputs[key] = source
        edges[source["node"]].add(target["node"])
        compiled.append({"kind": kind, "from": deepcopy(source), "to": deepcopy(target),
                         **({"when": raw_edge.get("when", True)} if kind == "condition" else {})})

    # Dependencies order lifecycle mounting but never manufacture data edges.
    for name, element in descriptors.items():
        for dependency, version in element["dependencies"].items():
            matches = [key for key, value in nodes.items() if value["use"] == dependency and value["version"] == version]
            if len(matches) != 1:
                raise ValueError("MECHANISM_DEPENDENCY_AMBIGUOUS")
            edges[matches[0]].add(name)
        for port in element["inputs"]:
            if port["required"] and (name, port["name"]) not in inputs and not element["entry"]:
                raise ValueError("MECHANISM_INPUT_UNBOUND: " + name + "." + port["name"])

    active, visited, order = set(), set(), []
    def visit(name):
        if name in active:
            raise ValueError("MECHANISM_UNBOUNDED_CYCLE")
        if name in visited:
            return
        active.add(name)
        for target in sorted(edges[name]):
            visit(target)
        active.remove(name)
        visited.add(name)
        order.append(name)
    for name in sorted(nodes):
        visit(name)
    if not any(element["entry"] for element in descriptors.values()):
        raise ValueError("MECHANISM_EVENT_ENTRY_REQUIRED")
    result = {"schema_version": 2, "id": raw["id"], "intent": intent.strip(), "source_refs": list(dict.fromkeys(sources)),
        "scope": scope, "catalog_version": directory["version"], "elements": sorted(nodes.values(), key=lambda n: n["id"]),
        "bindings": sorted(compiled, key=lambda e: (e["from"]["node"], e["from"]["port"], e["to"]["node"], e["to"]["port"])),
        "order": list(reversed(order)), "required_capabilities": sorted({c for e in descriptors.values()
            for c in [*e["capabilities"], *e.get('reconciliation', {}).get('capabilities', [])]}),
        "exclusive_writes": sorted(writers), "authority_granted": False}
    return {**result, "digest": digest(result)}
