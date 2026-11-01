"""Shadow-copy data upgrades coordinated by the installation's sole ledger.

Only the installation coordinator supplies resource bindings, while holding
its exclusive runtime lease. Signed migration code operates on NEW copies.
The program ledger is the commit decision: an interrupted/uncommitted program
transaction restores original data; a committed one keeps the new generation.
The journal records fixed sibling slots and identities, never executable text.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import stat

from .persistence import atomic_control, read_control, sync_directory, private_namespace
from .tree import tree_digest, sync_tree
from .validation import fields, reject, text


JOURNAL = "data-upgrade.json"
MAX_RESOURCES = 256


def _canonical(path):
    if (not path.is_absolute() or path != path.resolve()
            or any(parent.is_symlink() for parent in (path, *path.parents))):
        reject("UPDATE_DATA_PATH", "Data resources must use canonical unredirected paths")
    return path


def _identity(path):
    if not path.exists():
        if path.is_symlink(): reject("UPDATE_DATA_CHANGED", "Data slot is redirected")
        return None
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        reject("UPDATE_DATA_PATH", "Bind individual data files; maintenance lock directories must stay in place")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        reject("UPDATE_DATA_PATH", "Data resource is not an independent regular file or directory")
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        value = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
        if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
            reject("UPDATE_DATA_CHANGED", "Data resource changed during observation")
    if path.stat().st_ino != before.st_ino:
        reject("UPDATE_DATA_CHANGED", "Data resource was replaced during observation")
    return {"kind": "file", "sha256": value}


def _copy(source, target):
    if source.is_dir():
        if os.name == "nt":
            from .windows_storage import copy_tree
            copy_tree(source, target)
        else:
            shutil.copytree(source, target, symlinks=True)
        sync_tree(target)
    else:
        if os.name == "nt":
            from .windows_storage import durable_copy
            durable_copy(source, target)
        else:
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush(); os.fsync(outgoing.fileno())
            target.chmod(0o600)
    sync_directory(target.parent)


def _move(source, target):
    if target.exists() or target.is_symlink():
        reject("UPDATE_DATA_CHANGED", "Data generation slot is occupied")
    if os.name == "nt":
        from .windows_storage import move
        move(source, target)
    else:
        os.rename(source, target)
    sync_directory(source.parent)


class DataUpgrade:
    def __init__(self, directory, transaction_id, resources):
        self.directory = _canonical(Path(directory))
        self.transaction_id = text(transaction_id, r"[0-9a-f]{32}", 32)
        if type(resources) is not dict or len(resources) > MAX_RESOURCES:
            reject("UPDATE_DATA_PATH", "Explicit bounded data resource bindings are required")
        self.resources = {}
        for name, path in sorted(resources.items()):
            text(name, r"[a-z][a-z0-9_.-]{0,63}", 64)
            path = _canonical(Path(path))
            if path == self.directory or path.is_relative_to(self.directory) or self.directory.is_relative_to(path):
                reject("UPDATE_DATA_PATH", "Data and installation journal must be separate")
            for other in self.resources.values():
                if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                    reject("UPDATE_DATA_PATH", "Data resources must not overlap")
            self.resources[name] = path

    def _slots(self, source):
        prefix = "." + source.name + ".gitgo-upgrade-" + self.transaction_id
        return {name: source.parent / (prefix + "." + name) for name in ("new", "old", "failed")}

    def _read(self):
        value = fields(read_control(self.directory / JOURNAL), {"schema_version", "transaction_id", "state", "resources"})
        if (value["schema_version"] != 1 or type(value["schema_version"]) is not int
                or value["transaction_id"] != self.transaction_id
                or value["state"] not in {"PREPARED", "VALIDATED", "PUBLISHING", "PUBLISHED", "RESTORED"}
                or type(value["resources"]) is not list or len(value["resources"]) > MAX_RESOURCES):
            reject("UPDATE_DATA_JOURNAL", "Data upgrade journal identity is invalid")
        names = set()
        for item in value["resources"]:
            fields(item, {"name", "source", "before", "after"})
            name = item["name"]
            if name in names or name not in self.resources or item["source"] != str(self.resources[name]):
                reject("UPDATE_DATA_JOURNAL", "Journal differs from locally bound data resources")
            names.add(name)
            for identity in (item["before"], item["after"]):
                if identity is not None:
                    fields(identity, {"kind", "sha256"})
                    if identity["kind"] not in {"file", "directory"}:
                        reject("UPDATE_DATA_JOURNAL", "Unknown data resource kind")
                    text(identity["sha256"], r"[0-9a-f]{64}", 64)
        if names != set(self.resources):
            reject("UPDATE_DATA_JOURNAL", "Data resource set changed")
        return value

    def _save(self, value, state):
        value["state"] = state
        atomic_control(self.directory / JOURNAL, value)

    def prepare(self):
        if (self.directory / JOURNAL).exists():
            reject("UPDATE_DATA_JOURNAL", "Data upgrade preparation already exists")
        entries = []
        for name, source in self.resources.items():
            slots = self._slots(source)
            if any(p.exists() or p.is_symlink() for p in slots.values()):
                reject("UPDATE_DATA_CHANGED", "Data generation slots already exist")
            before = _identity(source)
            if before is not None:
                _copy(source, slots["new"])
                if _identity(source) != before or _identity(slots["new"]) != before:
                    reject("UPDATE_DATA_CHANGED", "Data changed during shadow preparation")
            entries.append({"name": name, "source": str(source), "before": before, "after": None})
        value = {"schema_version": 1, "transaction_id": self.transaction_id, "state": "PREPARED", "resources": entries}
        self._save(value, "PREPARED")
        return {name: self._slots(source)["new"] for name, source in self.resources.items()}

    def validate(self):
        value = self._read()
        if value["state"] != "PREPARED":
            reject("UPDATE_DATA_JOURNAL", "Data upgrade is not awaiting validation")
        for item in value["resources"]:
            source = self.resources[item["name"]]
            if _identity(source) != item["before"]:
                reject("UPDATE_DATA_CHANGED", "Original data changed during migration")
            after = _identity(self._slots(source)["new"])
            shadow = self._slots(source)["new"]
            if after is not None:
                if after["kind"] == "directory": sync_tree(shadow)
                else:
                    with shadow.open("r+b") as handle:
                        handle.flush(); os.fsync(handle.fileno())
            # No migration can silently delete an entire product resource.
            # Removed features may archive their fields/tables inside it.
            if item["before"] is not None and (after is None or after["kind"] != item["before"]["kind"]):
                reject("UPDATE_DATA_LOSS", "Migration removed or changed the resource boundary")
            item["after"] = after
        self._save(value, "VALIDATED")

    def publish(self, *, checkpoint=lambda _: None):
        value = self._read()
        if value["state"] != "VALIDATED":
            reject("UPDATE_DATA_JOURNAL", "Only validated shadows may replace data")
        for item in value["resources"]:
            source = self.resources[item["name"]]
            if (_identity(source) != item["before"]
                    or _identity(self._slots(source)["new"]) != item["after"]):
                reject("UPDATE_DATA_CHANGED", "Data changed before publication")
        self._save(value, "PUBLISHING")
        checkpoint("data_publish_intent")
        for item in value["resources"]:
            source = self.resources[item["name"]]; slots = self._slots(source)
            if item["before"] is not None:
                _move(source, slots["old"])
                checkpoint("data_old_saved:" + item["name"])
            if item["after"] is not None:
                _move(slots["new"], source)
                checkpoint("data_new_published:" + item["name"])
        self._save(value, "PUBLISHED")
        checkpoint("data_published")

    def recover(self, *, committed, checkpoint=lambda _: None, locks_held=False):
        if not committed and not locks_held:
            with storage_maintenance(self.resources):
                return self._recover(committed=committed, checkpoint=checkpoint)
        return self._recover(committed=committed, checkpoint=checkpoint)

    def _recover(self, *, committed, checkpoint):
        """The caller supplies the decision read from the program ledger."""
        value = self._read()
        if committed:
            if value["state"] != "PUBLISHED":
                reject("UPDATE_DATA_JOURNAL", "Program committed before data publication")
            # New user work may already have changed the live data. Never
            # compare it with migration-time bytes or restore old data here.
            for item in value["resources"]:
                if item["before"] is not None and _identity(self._slots(self.resources[item["name"]])["old"]) != item["before"]:
                    reject("UPDATE_DATA_CHANGED", "Retained pre-upgrade data changed")
            return
        if value["state"] in {"PREPARED", "VALIDATED", "RESTORED"}:
            for item in value["resources"]:
                if _identity(self.resources[item["name"]]) != item["before"]:
                    reject("UPDATE_DATA_CHANGED", "Unpublished or restored data changed")
            self._save(value, "RESTORED")
            return
        for item in reversed(value["resources"]):
            source = self.resources[item["name"]]; slots = self._slots(source)
            live, old, new, failed = (_identity(p) for p in (source, slots["old"], slots["new"], slots["failed"]))
            before, after = item["before"], item["after"]
            allowed = [(before, None, after, None), (None, before, after, None),
                       (after, before, None, None), (None, before, None, after),
                       (before, None, None, after)]
            if (live, old, new, failed) not in allowed:
                reject("UPDATE_DATA_CHANGED", "Data generation identities are ambiguous")
            if old == before and before is not None:
                if live == after and after is not None and new is None and failed is None:
                    _move(source, slots["failed"])
                    checkpoint("data_failed_saved:" + item["name"])
                    live = None
                if live is not None:
                    reject("UPDATE_DATA_CHANGED", "Live data differs from the uncommitted generation")
                _move(slots["old"], source)
                checkpoint("data_old_restored:" + item["name"])
            elif live != before or old is not None:
                # Absent original resources may only have an exact newly
                # published generation; preserve it in the failed slot.
                if before is None and live == after and after is not None and old is None and failed is None:
                    _move(source, slots["failed"])
                else:
                    reject("UPDATE_DATA_CHANGED", "Data recovery evidence is ambiguous")
        self._save(value, "RESTORED")


@contextmanager
def storage_maintenance(resources):
    """Reuse the existing storage barrier; never bypass active project users."""
    from backend.core.storage.maintenance import StorageLease
    with ExitStack() as stack:
        parents = {path.parent for path in resources.values() if path.parent.exists()}
        if parents:
            try: common = Path(os.path.commonpath(list(parents)))
            except ValueError: common = None
            if common is not None:
                try:
                    stack.enter_context(private_namespace(common))
                    parents = set()
                except (OSError, ValueError): pass
            for parent in sorted(parents): stack.enter_context(private_namespace(parent))
        roots = {path.parent for name, path in resources.items() if name.startswith("db_")}
        for root in sorted(roots):
            # This stable lock stays in its directory. Database families move;
            # neither a project namespace nor its admission inode is renamed.
            stack.enter_context(StorageLease(root, exclusive=True))
        yield
