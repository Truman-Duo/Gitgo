"""Typed component propagation with durable, non-replaying invocation records.

Components receive only declared services. Business effects must use adapters
to existing tools/decisions/runtime services; this is not another tool executor.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from copy import deepcopy
from time import monotonic
from typing import Callable
import threading
import weakref
from .contracts import bounded_json, digest, validate

_locks = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


def _event_lock(key):
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


@dataclass(frozen=True)
class NodeResult:
    outputs: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryResult:
    result: NodeResult
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class Context:
    project_id: str
    task_id: str
    instance_id: str
    epoch: int
    event_id: str
    boundary: str
    node_id: str
    state: dict
    services: dict[str, Callable]
    cancelled: Callable[[], bool]
    deadline: float

    def call(self, service, payload):
        self.check()
        if service not in self.services:
            raise ValueError("MECHANISM_SERVICE_UNAVAILABLE: " + service)
        # The adapter binds actual task and project identity; a payload cannot
        # retarget another task or manufacture authorization.
        result = self.services[service](self, bounded_json(payload, maximum=262144))
        self.check()
        return bounded_json(result, maximum=262144)

    def check(self):
        if self.cancelled():
            raise RuntimeError("MECHANISM_CANCELLED")
        if monotonic() >= self.deadline:
            raise TimeoutError("MECHANISM_NODE_TIMEOUT")


class Runner:
    def __init__(self, registry, repository, services=None, *, recovery_services=None):
        self.registry, self.repo, self.services = registry, repository, services or {}
        # Runtime assembly supplies only canonical read adapters here. A
        # component cannot gain its ordinary mutating services in recovery.
        self.recovery_services = recovery_services or {}

    def reconcile(self, task_id, instance_id):
        """Observe registered evidence and terminate an interrupted graph.

        Never call its original handlers or continue unstarted downstream
        nodes. ToolPipeline invocation pins require their original recovery.
        Missing/uncertain evidence remains blocked and is safe to inspect again.
        """
        with _event_lock((self.repo.project_id, task_id, instance_id)):
            value, _ = self.repo.instance(task_id, instance_id)
            if not value or not value.get('active_run') or value.get('active_calls'):
                return {'reconciled': False, 'reason': 'no_component_only_interrupted_run'}
            key = value['active_run']
            run = value['runs'][key]
            if run['phase'] != 'reconciliation_required':
                return {'reconciled': False, 'reason': 'invocation_not_interrupted'}
            program = value['compiled']
            try:
                if not set(program['required_capabilities']).issubset(self.services):
                    raise ValueError('MECHANISM_RECOVERY_RUNTIME_UNAVAILABLE')
                with ExitStack() as leases:
                    loaded = {n['id']: leases.enter_context(self.registry.lease(n['use'], n['version'], n['contract_digest']))
                        for n in program['elements']}
                    for node in program['elements']:
                        journal = run['nodes'].get(node['id'])
                        if not journal or journal['phase'] in {'complete', 'reconciled'}:
                            continue
                        element = loaded[node['id']]
                        if element.reconciler is None:
                            raise ValueError('MECHANISM_RECOVERY_ADAPTER_MISSING')
                        if not set(element.recovery_capabilities).issubset(self.recovery_services):
                            raise ValueError('MECHANISM_RECOVERY_READ_SERVICE_MISSING')
                        ctx = Context(self.repo.project_id, task_id, instance_id, run['epoch'], run['event_id'], run['boundary'], node['id'],
                            deepcopy(value['states'].get(node['id'], {})),
                            {k: self.recovery_services[k] for k in element.recovery_capabilities}, lambda: False,
                            monotonic() + min(element.timeout_seconds, 30))
                        recovered = element.reconciler(ctx, deepcopy(node['parameters']), deepcopy(journal))
                        ctx.check()
                        if not isinstance(recovered, RecoveryResult) or not isinstance(recovered.result, NodeResult):
                            raise ValueError('MECHANISM_RECOVERY_EVIDENCE_REQUIRED')
                        refs = recovered.evidence_refs
                        if not isinstance(refs, (tuple,list)) or not 1 <= len(refs) <= 32 or any(not isinstance(r,str) or not r or len(r)>2000 for r in refs):
                            raise ValueError('MECHANISM_RECOVERY_EVIDENCE_REQUIRED')
                        result = recovered.result
                        bounded_json(result.outputs, maximum=262144)
                        bounded_json(result.state)
                        validate(result.state, element.state_schema)
                        expected = {p.name: p for p in element.outputs}
                        if set(result.outputs) - set(expected) or any(p.required and p.name not in result.outputs for p in element.outputs):
                            raise ValueError('MECHANISM_RECOVERY_OUTPUT_INVALID')
                        for port, payload in result.outputs.items():
                            validate(payload, self.registry.schema(expected[port].type_id))
                        def observed(current):
                            if current['active_run'] != key or current['epoch'] != run['epoch']:
                                raise ValueError('MECHANISM_RECOVERY_GENERATION_CHANGED')
                            current['states'][node['id']] = deepcopy(result.state)
                            current['runs'][key]['nodes'][node['id']].update(phase='reconciled', outputs=deepcopy(result.outputs),
                                evidence_refs=list(refs), disposition='observed_without_replay')
                        self.repo.mutate(task_id, instance_id, observed)
                    def finish(current):
                        if current['active_run'] != key or current['epoch'] != run['epoch'] or current.get('active_calls'):
                            raise ValueError('MECHANISM_RECOVERY_GENERATION_CHANGED')
                        current['runs'][key].update(phase='reconciled', disposition='terminated_without_replaying_or_resuming_graph')
                        current['active_run'] = None
                        current['phase'] = 'pending_activation' if current['activation']['phase'] in {'pending_activation','activating'} else 'ready'
                        current.pop('error', None)
                    self.repo.mutate(task_id, instance_id, finish)
                return {'reconciled': True, 'disposition': 'terminated_without_replay'}
            except (ValueError, RuntimeError, OSError, TypeError) as error:
                def blocked(current):
                    current['phase'] = 'blocked'
                    current.setdefault('error', {'effect_state': 'unknown'})['reconciliation_detail'] = str(error)[:400]
                self.repo.mutate(task_id, instance_id, blocked)
                return {'reconciled': False, 'reason': str(error)[:400]}

    def dispatch(self, task_id, instance_id, event_id, boundary, event, *, cancelled=lambda: False, retained=None, freeze_snapshot=False):
        observed, _ = self.repo.instance(task_id, instance_id)
        event_key = digest({"id": event_id, "boundary": boundary})
        previous = (observed or {}).get("runs", {}).get(event_key)
        if previous and observed["phase"] != "blocked":
            if not freeze_snapshot and previous["event_digest"] != digest(event):
                raise ValueError("MECHANISM_EVENT_IDENTITY_CONFLICT")
            return deepcopy(previous)
        # Separate event admission from the configuration fence: a semantic
        # callback may wait for a provider without preventing a mode update
        # from staging. Only this instance's state transitions are serialized.
        lock = _event_lock((self.repo.project_id, task_id, instance_id))
        deadline = monotonic() + 300
        while not lock.acquire(timeout=0.05):
            if cancelled():
                raise RuntimeError("MECHANISM_CANCELLED")
            if monotonic() >= deadline:
                raise TimeoutError("MECHANISM_EVENT_ADMISSION_TIMEOUT")
        try:
            return self._dispatch(task_id, instance_id, event_id, boundary, event,
                cancelled=cancelled, retained=retained, deadline=deadline, freeze_snapshot=freeze_snapshot)
        finally:
            lock.release()

    def _dispatch(self, task_id, instance_id, event_id, boundary, event, *, cancelled, retained, deadline, freeze_snapshot):
        bounded_json(event, maximum=262144)
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise ValueError("MECHANISM_EVENT_ID_REQUIRED")
        instance, _ = self.repo.instance(task_id, instance_id)
        pinned = (retained is not None and retained.task_id == task_id and retained.instance_id == instance_id
                  and instance is not None and instance.get("active_calls", {}).get(retained.unit_id, {}).get("epoch") == retained.epoch)
        if instance is None or (instance["phase"] != "ready" and not pinned):
            raise ValueError("MECHANISM_INSTANCE_NOT_READY")
        program = instance["compiled"]
        event_key = digest({"id": event_id, "boundary": boundary})
        event_digest = digest(event)
        archived = self.repo.completed(task_id, instance_id, event_key)
        if archived:
            if not freeze_snapshot and archived["run"]["event_digest"] != event_digest:
                raise ValueError("MECHANISM_EVENT_IDENTITY_CONFLICT")
            return deepcopy(archived["run"])
        self.repo.archive_completed(task_id, instance_id)
        def start(value):
            previous = value["runs"].get(event_key)
            if previous:
                if not freeze_snapshot and previous["event_digest"] != event_digest:
                    raise ValueError("MECHANISM_EVENT_IDENTITY_CONFLICT")
                return {"created": False, "run": previous}
            if (value["phase"] != "ready" and not pinned) or value["epoch"] != instance["epoch"]:
                raise ValueError("MECHANISM_GENERATION_CHANGED")
            if value["active_run"]:
                raise ValueError("MECHANISM_INSTANCE_BUSY")
            if len(value["runs"]) >= 256:
                raise ValueError("MECHANISM_JOURNAL_CAPACITY")
            run = {"event_digest": event_digest, "event_id": event_id, "boundary": boundary, "phase": "running",
                   "epoch": value["epoch"], "definition_ref": value["effective_ref"], "nodes": {}}
            value["runs"][event_key] = run
            value["active_run"] = event_key
            return {"created": True, "run": run, "states": value["states"], "compiled": value["compiled"]}
        admitted = self.repo.mutate(task_id, instance_id, start)
        run = admitted["run"]
        if not admitted["created"]:
            # Duplicate delivery observes a running/finished operation; it
            # never races the first dispatcher into the same component.
            return deepcopy(run)
        program = admitted["compiled"]
        # An interrupted attempt must be reconciled by its registered adapter.
        # Never infer that its external effect did not occur from a lost reply.
        if run["nodes"]:
            raise ValueError("MECHANISM_INVOCATION_RECONCILIATION_REQUIRED")
        try:
            with ExitStack() as stack:
                elements = retained.elements if pinned else {
                    node["id"]: stack.enter_context(self.registry.lease(node["use"], node["version"], node["contract_digest"]))
                    for node in program["elements"]}
                nodes = {node["id"]: node for node in program["elements"]}
                inputs, outputs = {}, {}
                for name in program["order"]:
                    element = elements[name]
                    if boundary not in nodes[name]["boundaries"]:
                        continue
                    incoming = inputs.get(name, {})
                    if element.entry:
                        incoming = {**incoming, "event": deepcopy(event)} if element.inputs else incoming
                    if not element.entry and any(port.required and port.name not in incoming for port in element.inputs):
                        continue
                    if not element.entry and not incoming:
                        continue
                    for port in element.inputs:
                        if port.name in incoming:
                            validate(incoming[port.name], self.registry.schema(port.type_id))
                    context = Context(self.repo.project_id, task_id, instance_id, run["epoch"], event_id, boundary, name,
                        deepcopy(admitted["states"].get(name, {})),
                        {key: self.services[key] for key in element.capabilities if key in self.services}, cancelled,
                        min(deadline, monotonic() + element.timeout_seconds))
                    if not set(element.capabilities).issubset(context.services):
                        raise ValueError("MECHANISM_REQUIRED_SERVICE_MISSING")
                    def begin(value):
                        record = value["runs"][event_key]
                        if name in record["nodes"]:
                            raise ValueError("MECHANISM_NODE_ALREADY_STARTED")
                        record["nodes"][name] = {"phase": "running", "input_digest": digest(incoming), "effect": element.effect}
                    self.repo.mutate(task_id, instance_id, begin)
                    context.check()
                    result = element.handler(context, deepcopy(nodes[name]["parameters"]), deepcopy(incoming))
                    context.check()
                    if not isinstance(result, NodeResult):
                        raise ValueError("MECHANISM_NODE_RESULT_REQUIRED")
                    bounded_json(result.outputs, maximum=262144)
                    bounded_json(result.state)
                    validate(result.state, element.state_schema)
                    expected = {port.name: port for port in element.outputs}
                    if set(result.outputs) - set(expected):
                        raise ValueError("MECHANISM_OUTPUT_PORT_UNKNOWN")
                    if any(port.required and port.name not in result.outputs for port in element.outputs):
                        raise ValueError("MECHANISM_REQUIRED_OUTPUT_MISSING")
                    for port, payload in result.outputs.items():
                        validate(payload, self.registry.schema(expected[port].type_id))
                    def finish(value):
                        value["states"][name] = deepcopy(result.state)
                        value["runs"][event_key]["nodes"][name].update(phase="complete", outputs=deepcopy(result.outputs))
                    self.repo.mutate(task_id, instance_id, finish)
                    outputs[name] = result.outputs
                    for edge in program["bindings"]:
                        if edge["from"]["node"] != name or edge["from"]["port"] not in result.outputs:
                            continue
                        payload = result.outputs[edge["from"]["port"]]
                        if edge["kind"] == "condition":
                            if payload is not edge["when"]:
                                continue
                            payload = True
                        elif edge["kind"] == "trigger" and payload is not True:
                            continue
                        inputs.setdefault(edge["to"]["node"], {})[edge["to"]["port"]] = deepcopy(payload)
            def complete(value):
                value["runs"][event_key]["phase"] = "complete"
                value["active_run"] = None
                return value["runs"][event_key]
            return self.repo.mutate(task_id, instance_id, complete)
        except BaseException as error:
            # A durable 'running' node is the safety tombstone if even this
            # checkpoint fails. Observer failures cannot replay that callback.
            def block(value):
                value["phase"] = "blocked"
                value["runs"][event_key]["phase"] = "reconciliation_required"
                value["error"] = {"code": str(error).split(":", 1)[0][:120] or type(error).__name__,
                    "detail": str(error)[:400], "effect_state": "unknown"}
                value["runs"][event_key]["error"] = deepcopy(value["error"])
            self.repo.mutate(task_id, instance_id, block)
            raise
