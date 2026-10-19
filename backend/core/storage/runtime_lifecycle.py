"""Canonical runtime selection, activation and evidenced crash reconciliation.

All operations use the existing Host gate, runtime lease and two-slot journal.
A missing creation identity stays unknown; directory discovery cannot supply it.
"""
from dataclasses import dataclass

from backend.core.sandbox_read import ObjectIdentity
from .host_records import require, guid
from .runtime_manifest import digest
from .native_windows_process import WindowsProcessLibrary, WindowsStorageProcess, StorageProcessIdentity
from .native_windows_tree import RuntimeAbsence, TreePurpose
from .runtime_control import RuntimeControl, RuntimeInstallPlan, require_whole_tail
from .runtime_installation import PinnedRuntime


@dataclass(frozen=True)
class RuntimeReference:
    generation: str
    manifest: str
    epoch: int

    def __post_init__(self):
        guid(self.generation)
        digest(self.manifest)
        require(type(self.epoch) is int and 1 <= self.epoch < 1 << 63, "bounded activation epoch required")


class SelectedRuntime:
    """Retained code pins and lease; not storage work or execution admission."""
    def __init__(self, control, pins, reference):
        self._control, self._pins, self.reference = control, pins, reference

    def require_live(self, reference=None):
        control = self._control
        host = control._host
        host._require_identity()
        require(getattr(control, "_selected", None) is self and not host._closed
                and getattr(control, "_lease_mode", None) == "shared"
                and self._pins.ready and not host._scope._sealed,
                "live selected runtime ownership required")
        require(reference is None or type(reference) is RuntimeReference and reference == self.reference,
                "selected runtime reference changed")
        return self._pins.manifest


def claim(control, *, exclusive):
    require(type(control) is RuntimeControl and not control._used, "fresh canonical runtime control required")
    control._host.anchor
    control._used = True
    control._host._ready = False
    control._lease(exclusive=exclusive)
    prior = control._read()
    require_whole_tail(prior, additional=0)
    return prior, prior.head.state.value()


def entry_plan(control, value, slot):
    require(type(slot) is int and slot in (0, 1), "bounded runtime slot required")
    entry = value["entries"][slot]
    require(entry is not None, "registered runtime generation required")
    plan = RuntimeInstallPlan.for_host(control._anchor, slot, entry["generation"])
    plan.require_entry(entry)
    return entry, plan


def verify_target(control, entry, plan):
    require(entry["root"] is not None and entry["manifest"] is not None, "recorded runtime identity required")
    target = plan.target_manifest(control._anchor, ObjectIdentity(**entry["root"]))
    require(target.sha256 == entry["manifest"], "installed runtime manifest changed")
    pins = control._host._new(PinnedRuntime, control._host._registry.trees,
        target.to_bytes(), target.sha256, control._anchor.account.sid)
    pins.pin()
    return pins


def observe_absence(control, generation):
    host = control._host
    absence = host._new(RuntimeAbsence, host._registry.trees, TreePurpose.ABSENCE, control._anchor.account.sid)
    result = absence.observe(host._members["runtimes"], dict(control._anchor.members)["runtimes"], generation)
    # A successful observation cannot refund capacity while its owner remains.
    require(host._release(absence), "runtime absence owner closure is incomplete")
    return result


def activate(control, slot):
    prior, value = claim(control, exclusive=True)
    control._idle(value)
    control._namespace(value)
    entry, plan = entry_plan(control, value, slot)
    require(entry["phase"] == "READY", "verified ready runtime required")
    require(value["epoch"] < (1 << 63)-1, "runtime epoch exhausted")
    verify_target(control, entry, plan)
    for index, other in enumerate(value["entries"]):
        if other is not None:
            entry_plan(control, value, index)
            if other["phase"] == "ACTIVE":
                other["phase"] = "RETIRING"
    entry["phase"] = "ACTIVE"
    value.update(active=entry["generation"], epoch=value["epoch"]+1)
    control._update(prior, value)
    control._host._ready = True
    return RuntimeReference(entry["generation"], entry["manifest"], value["epoch"])


def select(control):
    prior, value = claim(control, exclusive=False)
    control._idle(value)
    control._namespace(value)
    slot = next((i for i, entry in enumerate(value["entries"])
                 if entry is not None and entry["generation"] == value["active"]), None)
    require(slot is not None, "an active installed runtime is required")
    entry, plan = entry_plan(control, value, slot)
    require(entry["phase"] == "ACTIVE", "active runtime pointer is inconsistent")
    pins = verify_target(control, entry, plan)
    # Readable bytes may survive an unacknowledged previous activation flush.
    # Establish the exact current authority before issuing a selected reference.
    host = control._host
    host._scope.flush_bound(host._root, "slot-0063.json", dict(control._anchor.members)["slot-0063.json"], 8192)
    require(control._read().raw == prior.raw, "runtime selection authority changed")
    selected = SelectedRuntime(control, pins,
        RuntimeReference(entry["generation"], entry["manifest"], value["epoch"]))
    control._selected = selected
    host._ready = True
    return selected


def cancel_unstarted(control, slot):
    prior, value = claim(control, exclusive=True)
    control._idle(value)
    control._namespace(value)
    entry, _ = entry_plan(control, value, slot)
    require(entry["phase"] == "RESERVED" and entry["worker"] is None,
            "only a reservation with no launch attempt can be cancelled")
    require(observe_absence(control, entry["generation"]), "unstarted generation unexpectedly exists")
    entry["phase"] = "RETIRING"
    control._update(prior, value)
    control._host._ready = True


def release_capacity(control, slot):
    prior, value = claim(control, exclusive=True)
    control._idle(value)
    control._namespace(value)
    entry, _ = entry_plan(control, value, slot)
    require(entry["phase"] == "ABSENT" and entry["worker"] is not None
            and entry["worker"]["phase"] == "RETIRED" and entry["worker"]["mode"] == "cleanup",
            "committed cleanup closure and absence required")
    require(observe_absence(control, entry["generation"]), "runtime name reappeared before capacity release")
    value["entries"][slot] = None
    control._update(prior, value)
    control._host._ready = True


def retire_ready(control, slot):
    prior, value = claim(control, exclusive=True)
    control._idle(value)
    control._namespace(value)
    entry, plan = entry_plan(control, value, slot)
    require(entry["phase"] == "READY", "only an inactive ready runtime can retire directly")
    verify_target(control, entry, plan)
    entry["phase"] = "RETIRING"
    control._update(prior, value)
    control._host._ready = True


def recover(control, slot, processes):
    require(type(processes) is WindowsProcessLibrary, "trusted native process observations required")
    prior, value = claim(control, exclusive=True)
    control._idle(value, except_slot=slot)
    control._namespace(value)
    entry, plan = entry_plan(control, value, slot)
    event = entry["worker"]
    require(event is not None and event["phase"] in {"CREATED", "ACCEPTED"} and event["pid"] > 0,
            "unknown native creation cannot be replayed or inferred from a directory")
    observers = []
    for pid, created in ((event["parent_pid"], event["parent_created"]), (event["pid"], event["created"])):
        observer = control._host._new(WindowsStorageProcess, processes)
        observers.append(observer)
        require(observer.ended(StorageProcessIdentity(pid, created)), "exact runtime owner process is still alive")
    for observer in observers:
        require(observer.retire(terminate=False), "native recovery observation closure is incomplete")
        require(control._host._release(observer), "native recovery owner remains retained")
    if event["mode"] == "install":
        if entry["phase"] == "MATERIALIZED":
            verify_target(control, entry, plan)
            entry["phase"] = "READY"
        else:
            require(entry["phase"] in {"RESERVED", "BUILDING"}, "unexpected installation recovery phase")
            entry["phase"] = "RETIRING"
    else:
        require(event["mode"] == "cleanup" and entry["phase"] == "RETIRING", "unexpected cleanup recovery phase")
        if observe_absence(control, entry["generation"]):
            entry["phase"] = "ABSENT"
    entry["worker"] = dict(event, phase="RETIRED")
    control._update(prior, value)
    control._host._ready = True
    return entry["phase"]
