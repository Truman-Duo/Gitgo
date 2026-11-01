"""Versioned component directory with retained invocation leases and disposal."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import inspect
import threading
import asyncio

from .contracts import Element, identifier, digest, validate_schema


class Registry:
    def __init__(self):
        self._types = {}
        self._entries = {}
        self._lock = threading.Condition(threading.RLock())

    def register_type(self, name, schema):
        identifier(name)
        validate_schema(schema)
        with self._lock:
            if name in self._types and self._types[name] != schema:
                raise ValueError("MECHANISM_TYPE_REDEFINITION")
            self._types[name] = deepcopy(schema)

    def schema(self, name):
        with self._lock:
            if name not in self._types:
                raise ValueError("MECHANISM_PORT_TYPE_MISSING: " + name)
            return deepcopy(self._types[name])

    def register(self, element: Element, *, dispose=None):
        identifier(element.identifier)
        if type(element.version) is not int or element.version < 1 or not callable(element.handler):
            raise ValueError("MECHANISM_ELEMENT_INVALID")
        if (element.effect not in {"pure", "projection", "control"} or not element.boundaries
                or not element.scopes or any(s not in {"task", "session", "project"} for s in element.scopes)
                or element.failure != "block" or element.recovery not in {"reconcile", "pure_replay"}
                or element.cancellation != "cooperative" or not 0 < element.timeout_seconds <= 300):
            raise ValueError("MECHANISM_ELEMENT_CONTRACT_INVALID")
        if element.effect != "pure" and element.recovery == "pure_replay":
            raise ValueError("MECHANISM_EFFECT_REPLAY_FORBIDDEN")
        if (element.reconciler is not None and not callable(element.reconciler)) or (element.recovery_capabilities and element.reconciler is None):
            raise ValueError('MECHANISM_RECONCILER_INVALID')
        validate_schema(element.configuration)
        validate_schema(element.state_schema)
        if element.boundary_parameter:
            identifier(element.boundary_parameter)
            if element.boundary_parameter not in element.configuration.get("properties", {}):
                raise ValueError("MECHANISM_BOUNDARY_PARAMETER_MISSING")
        for value in (*element.boundaries, *element.capabilities, *element.recovery_capabilities, *element.conflicts, *element.writes):
            identifier(value)
        if len(dict(element.dependencies)) != len(element.dependencies):
            raise ValueError("MECHANISM_DEPENDENCY_DUPLICATE")
        for name, version in element.dependencies:
            identifier(name)
            if type(version) is not int or version < 1:
                raise ValueError("MECHANISM_DEPENDENCY_VERSION_INVALID")
        for ports in (element.inputs, element.outputs):
            if len({p.name for p in ports}) != len(ports):
                raise ValueError("MECHANISM_PORT_DUPLICATE")
            for port in ports:
                identifier(port.name)
                if type(port.required) is not bool:
                    raise ValueError("MECHANISM_PORT_REQUIRED_INVALID")
                self.schema(port.type_id)
        # A caller retaining its mutable dict cannot silently mutate an
        # already-registered interface after a preview has been compiled.
        implementation = element.implementation or (element.handler.__module__ + "." + element.handler.__qualname__)
        try:
            implementation += ":" + digest(inspect.getsource(element.handler))
        except (OSError, TypeError):
            implementation += ":native"
        reconciliation = ''
        if element.reconciler is not None:
            reconciliation = element.reconciler.__module__ + '.' + element.reconciler.__qualname__
            try:
                reconciliation += ':' + digest(inspect.getsource(element.reconciler))
            except (OSError, TypeError):
                reconciliation += ':native'
        element = replace(element, configuration=deepcopy(element.configuration), state_schema=deepcopy(element.state_schema), implementation=implementation,
            recovery_capabilities=tuple(element.recovery_capabilities), reconciliation_implementation=reconciliation,
            inputs=tuple(element.inputs), outputs=tuple(element.outputs), boundaries=tuple(element.boundaries),
            capabilities=tuple(element.capabilities), scopes=tuple(element.scopes), dependencies=tuple(element.dependencies),
            conflicts=tuple(element.conflicts), writes=tuple(element.writes))
        key = (element.identifier, element.version)
        record = {"element": element, "digest": digest(element.record()), "leases": 0,
                  "phase": "ready", "dispose": dispose, "error": None}
        with self._lock:
            if key in self._entries:
                raise ValueError("MECHANISM_ELEMENT_DUPLICATE")
            self._entries[key] = record

        async def unregister(*, timeout=30):
            if not 0 < timeout <= 300:
                raise ValueError("MECHANISM_DISPOSAL_TIMEOUT_INVALID")
            with self._lock:
                if record["phase"] == "disposed":
                    return
                if record["phase"] == "failed":
                    raise RuntimeError("MECHANISM_DISPOSAL_FAILED")
                if record["phase"] == "ready":
                    record["phase"] = "retiring"
            # Existing invocations retain their exact loaded implementation;
            # retirement immediately refuses new leases and catalog discovery.
            await asyncio.to_thread(self._wait_unleased, record, timeout)
            with self._lock:
                if record["phase"] == "failed":
                    raise RuntimeError("MECHANISM_DISPOSAL_FAILED")
                if record["phase"] in {"cleaning", "disposed"}:
                    leader = False
                else:
                    record["phase"] = "cleaning"
                    leader = True
            if not leader:
                await asyncio.to_thread(self._wait_disposed, record, timeout)
                return
            try:
                if dispose:
                    result = dispose()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=timeout)
            except BaseException:
                with self._lock:
                    record.update(phase="failed", error="MECHANISM_DISPOSAL_FAILED")
                    self._lock.notify_all()
                raise
            with self._lock:
                record["phase"] = "disposed"
                # An old disposer never removes an unrelated new registration.
                if self._entries.get(key) is record:
                    del self._entries[key]
                self._lock.notify_all()
        return unregister

    def _wait_unleased(self, record, timeout):
        with self._lock:
            if not self._lock.wait_for(lambda: record["leases"] == 0, timeout=timeout):
                raise TimeoutError("MECHANISM_INVOCATIONS_STILL_ACTIVE")

    def _wait_disposed(self, record, timeout):
        with self._lock:
            if not self._lock.wait_for(lambda: record["phase"] in {"disposed", "failed"}, timeout=timeout):
                raise TimeoutError("MECHANISM_DISPOSAL_STILL_ACTIVE")
            if record["phase"] == "failed":
                raise RuntimeError("MECHANISM_DISPOSAL_FAILED")

    def resolve(self, name, version=None):
        with self._lock:
            matches = [r for (n, v), r in self._entries.items() if n == name and r["phase"] == "ready"
                       and (version is None or v == version)]
            if not matches:
                raise ValueError("MECHANISM_ELEMENT_UNAVAILABLE: " + str(name))
            record = max(matches, key=lambda r: r["element"].version)
            return {**record["element"].record(), "digest": record["digest"]}

    @contextmanager
    def lease(self, name, version, expected_digest):
        with self._lock:
            record = self._entries.get((name, version))
            if record is None or record["phase"] != "ready" or record["digest"] != expected_digest:
                raise ValueError("MECHANISM_ELEMENT_CHANGED")
            record["leases"] += 1
        try:
            yield replace(record["element"], configuration=deepcopy(record["element"].configuration),
                          state_schema=deepcopy(record["element"].state_schema))
        finally:
            with self._lock:
                record["leases"] -= 1
                self._lock.notify_all()

    def catalog(self):
        with self._lock:
            elements = [self.resolve(n, v) for (n, v), r in sorted(self._entries.items()) if r["phase"] == "ready"]
            types = deepcopy(self._types)
            diagnostics = [{"id": n, "version": v, "phase": r["phase"], "error": r["error"]}
                           for (n, v), r in sorted(self._entries.items()) if r["phase"] != "ready"]
        return {"version": digest({"elements": elements, "types": types}), "elements": elements,
                "types": types, "diagnostics": diagnostics, "authority_granted": False}
