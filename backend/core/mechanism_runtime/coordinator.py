"""Persisted generation activation, inherited restrictions and bounded maintenance.

The coordinator never grants tool access, answers a decision or rolls back an
external effect. Its admission boundary selects one coherent component program.
In-flight runners retain the old program; later calls settle a pending version.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from .compiler import compile_program
from .contracts import digest, validate


def difference(before, after):
    old = {n["id"]: n for n in (before or {}).get("elements", [])}
    new = {n["id"]: n for n in after["elements"]}
    return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
        "bindings_before": deepcopy((before or {}).get("bindings", [])), "bindings_after": deepcopy(after["bindings"]),
        "capabilities_added": sorted(set(after["required_capabilities"]) - set((before or {}).get("required_capabilities", []))),
        "mandatory_checks_removed": False, "authority_granted": False}


def compatible(before, after):
    # This checks mechanical compatibility only. Changed intent and removal of
    # obligations still require the application's existing semantic review.
    if set(after["required_capabilities"]) - set(before["required_capabilities"]):
        return False
    old = {n["id"]: n for n in before["elements"]}
    for node in after["elements"]:
        prior = old.get(node["id"])
        if prior and (prior["use"] != node["use"] or prior["interface"] != node["interface"]):
            return False
    return True


def affected_nodes(before, after):
    delta = difference(before, after)
    touched = set(delta["added"] + delta["removed"] + delta["changed"])
    old = {digest(edge): edge for edge in before["bindings"]}
    new = {digest(edge): edge for edge in after["bindings"]}
    for key in old.keys() ^ new.keys():
        edge = old.get(key) or new[key]
        touched.add(edge["to"]["node"])
    edges = [*old.values(), *new.values()]
    for _ in range(len(before["elements"]) + len(after["elements"])):
        downstream = {edge["to"]["node"] for edge in edges if edge["from"]["node"] in touched}
        if downstream <= touched:
            break
        touched.update(downstream)
    return sorted(touched)


class Coordinator:
    def __init__(self, registry, repository, *, capabilities=()):
        self.registry, self.repo, self.capabilities = registry, repository, tuple(capabilities)

    def preview(self, raw, *, previous=None):
        compiled = compile_program(raw, self.registry, capabilities=self.capabilities, scope=raw.get("scope", "task"))
        return {"compiled": compiled, "diff": difference(previous, compiled), "preview_digest": compiled["digest"],
                "catalog_version": compiled["catalog_version"], "authority_granted": False}

    def check_constraints(self, program, constraints):
        for constraint in constraints:
            if not isinstance(constraint, dict) or set(constraint) - {"id", "denied_capabilities", "required_elements"} or not constraint.get("id"):
                raise ValueError("MECHANISM_INHERITED_CONSTRAINT_INVALID")
            if set(constraint.get("denied_capabilities", [])) & set(program["required_capabilities"]):
                raise ValueError("MECHANISM_INHERITED_CAPABILITY_DENIED")
            if not set(constraint.get("required_elements", [])).issubset(n["use"] for n in program["elements"]):
                raise ValueError("MECHANISM_REQUIRED_ELEMENT_MISSING")

    def request(self, task_id, instance_id, reference, *, expected_revision, action="activate", sources, user_control=None, update_state=None):
        if action not in {"activate", "suspend", "detach"} or not sources:
            raise ValueError("MECHANISM_ACTIVATION_REQUEST_INVALID")
        definition = self.repo.definition_ref(reference)
        def stage(value):
            control = value.get("user_control") or {}
            if action == "activate" and control.get("action") in {"suspend", "detach"}:
                if not any(source.get("authority_sequence", -1) > control["authority_sequence"] for source in sources.values()):
                    raise ValueError("MECHANISM_NEWER_USER_CONTROL: a later user source is required to reactivate this instance")
            if value["phase"] == "blocked" and (value.get("active_run") or value.get("active_calls")):
                raise ValueError("MECHANISM_RECONCILIATION_REQUIRED")
            self.check_constraints(definition["compiled"], value["required_constraints"])
            if action == "detach" and value["required_constraints"]:
                raise ValueError("MECHANISM_REQUIRED_CONSTRAINT_DETACH_DENIED")
            value["revision"] += 1
            value["desired_ref"] = reference
            value["phase"] = "pending_activation"
            value["activation"] = {"id": digest({"revision": value["revision"], "reference": reference, "action": action}),
                "phase": "pending_activation", "action": action, "from_epoch": value["epoch"], "sources": deepcopy(sources)}
            if user_control is not None:
                if (action not in {"suspend", "detach"} or type(user_control.get("authority_sequence")) is not int
                        or user_control["authority_sequence"] < 0 or not user_control.get("source_ref")):
                    raise ValueError("MECHANISM_USER_CONTROL_INVALID")
                value["user_control"] = {**deepcopy(user_control), "action": action}
            if update_state is not None:
                update_state(value)
            return deepcopy(value)
        return self.repo.mutate(task_id, instance_id, stage, expected_revision=expected_revision)

    def settle(self, task_id, instance_id):
        value, _ = self.repo.instance(task_id, instance_id)
        if value is None:
            raise ValueError("MECHANISM_INSTANCE_MISSING")
        if value["phase"] not in {"pending_activation", "activating"}:
            return value
        if value["active_run"] or value.get("active_calls"):
            return value
        activation_id = value["activation"]["id"]
        def preparing(current):
            if current["activation"]["id"] != activation_id or current["active_run"] or current.get("active_calls"):
                raise ValueError("MECHANISM_ACTIVATION_SUPERSEDED")
            current["phase"] = "activating"
            current["activation"]["phase"] = "activating"
        self.repo.mutate(task_id, instance_id, preparing)
        try:
            definition = self.repo.definition_ref(value["desired_ref"])
            program = definition["compiled"]
            self.check_constraints(program, value["required_constraints"])
            if not set(program["required_capabilities"]).issubset(self.capabilities):
                raise ValueError("MECHANISM_ACTIVATION_SERVICE_MISSING")
            with ExitStack() as leases:
                for node in program["elements"] if value["activation"]["action"] == "activate" else []:
                    component = leases.enter_context(self.registry.lease(node["use"], node["version"], node["contract_digest"]))
                    if node["id"] in value["states"]:
                        validate(value["states"][node["id"]], component.state_schema)
                # Shared exclusive projections cannot silently overwrite another
                # ready program in this task. The project owner serializes writes.
                for other in self.repo.list_instances(task_id, limit=100) if value["activation"]["action"] == "activate" else []:
                    if other["id"] != instance_id and other["phase"] in {"ready", "pending_activation", "activating"}:
                        if set(program["exclusive_writes"]) & set(other["compiled"]["exclusive_writes"]):
                            raise ValueError("MECHANISM_INSTANCE_WRITE_CONFLICT")
                def commit(current):
                    if current["activation"]["id"] != activation_id or current["active_run"] or current.get("active_calls"):
                        raise ValueError("MECHANISM_ACTIVATION_SUPERSEDED")
                    current.update(compiled=deepcopy(program), effective_ref=value["desired_ref"],
                        definition_revision=definition["revision"], epoch=current["epoch"] + 1,
                        phase={"activate": "ready", "suspend": "suspended", "detach": "detached"}[current["activation"]["action"]])
                    current["activation"]["phase"] = current["phase"]
                    current["activation"]["effective_epoch"] = current["epoch"]
                    current.pop("error", None)
                    return current
                return self.repo.mutate(task_id, instance_id, commit)
        except Exception as error:
            def failed(current):
                if current["activation"]["id"] == activation_id:
                    current["phase"] = "blocked"
                    current["error"] = {"code": str(error).split(":", 1)[0], "effect_state": "not_started"}
                    current["activation"]["phase"] = "blocked"
            self.repo.mutate(task_id, instance_id, failed)
            raise

    def reconcile(self, task_id, instance_id):
        value, _ = self.repo.instance(task_id, instance_id)
        if value and (value["active_run"] or value.get("active_calls")):
            def block(current):
                current["phase"] = "blocked"
                current["error"] = {"code": "MECHANISM_INTERRUPTED_INVOCATION", "effect_state": "unknown"}
                if current["active_run"]:
                    current["runs"][current["active_run"]]["phase"] = "reconciliation_required"
                for call in current.get("active_calls", {}).values():
                    call["phase"] = "unknown"
            self.repo.mutate(task_id, instance_id, block)
            return self.repo.instance(task_id, instance_id)[0]
        return self.settle(task_id, instance_id)

    def maintain(self, *, after="", limit=100, owner_task_id=None):
        """Bounded derived impact scan with continuation, never a global replan.

        Definitions retain exact dependency descriptors. The index is derived
        from these authoritative heads and may be rebuilt after interruption.
        """
        rows = self.repo.scan("instance/", after=after, limit=limit)
        affected = []
        for key, value, _ in rows:
            if value["phase"] in {"detached", "suspended", "saved"}:
                continue
            definition, reference = self.repo.definition(value["definition_id"])
            old = value["compiled"]
            missing = []
            for node in old["elements"]:
                try:
                    descriptor = self.registry.resolve(node["use"], node["version"])
                    if descriptor["digest"] != node["contract_digest"]:
                        missing.append(node["id"])
                except ValueError:
                    missing.append(node["id"])
            safety_error = "MECHANISM_DEPENDENCY_UNAVAILABLE" if missing else None
            if not safety_error and (value["effective_ref"] == reference or value["desired_ref"] == reference):
                continue
            status = "pinned" if value["version_policy"] == "pinned" else "pending" if compatible(old, definition["compiled"]) else "needs_review"
            event_id = digest({"effective": value["effective_ref"], "target": reference, "safety": safety_error})
            nodes = sorted(missing) if missing else affected_nodes(old, definition["compiled"])
            observation = {"id": event_id, "target_ref": reference, "status": status,
                           "affected_nodes": nodes, "error": safety_error}
            previous = value["maintenance"].get(event_id) or {}
            retrying = not safety_error and status == "pending" and previous.get("status") == "inspect_required"
            def record(current):
                if event_id not in current["maintenance"] and len(current["maintenance"]) >= 64:
                    raise ValueError("MECHANISM_MAINTENANCE_CAPACITY")
                current["maintenance"][event_id] = observation
                current["maintenance_head"] = event_id
                if safety_error:
                    current["phase"] = "blocked"
                    current["error"] = {"code": safety_error, "effect_state": "not_started"}
            if not retrying and (value.get("maintenance_head") != event_id or previous != observation
                    or (safety_error and value["phase"] != "blocked")):
                self.repo.mutate(value["task_id"], value["id"], record)
            phase = "blocked" if safety_error else value["phase"]
            if status == "pending" and not safety_error:
                try:
                    pending = self.request(value["task_id"], value["id"], reference, expected_revision=value["revision"],
                                           sources=definition["sources"])
                    phase = pending["phase"]
                    # Project impact detection can stage another task, but only
                    # that task's adapter knows its actual available services.
                    if owner_task_id is None or value["task_id"] == owner_task_id:
                        phase = self.settle(value["task_id"], value["id"])["phase"]
                    if retrying:
                        self.repo.mutate(value["task_id"], value["id"], record)
                except (ValueError, RuntimeError) as error:
                    observation = {**observation, "status": "inspect_required", "error": str(error)[:400]}
                    if previous != observation:
                        self.repo.mutate(value["task_id"], value["id"], record)
                    affected.append({"task_id": value["task_id"], "instance_id": value["id"], "event_id": event_id,
                        **{key: observation[key] for key in ("status", "error", "affected_nodes")}})
                    continue
            affected.append({"task_id": value["task_id"], "instance_id": value["id"], "event_id": event_id,
                             "status": status, "phase": phase, "affected_nodes": nodes,
                             **({"error": safety_error} if safety_error else {})})
        return {"affected": affected, "after": rows[-1][0] if len(rows) == limit else "", "complete": len(rows) < limit}
