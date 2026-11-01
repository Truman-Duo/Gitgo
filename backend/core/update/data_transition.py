"""Bind product-owned resources and run the verified candidate's data adapter.

This coordinator is part of the updater, not the Host transport or the Agent.
It never invents workspace identities or migrates business output files.
"""
import os
from pathlib import Path
import uuid

from .data_upgrade import DataUpgrade, JOURNAL, _canonical
from .manifest import DataCompatibility
from .persistence import atomic_control, read_control
from .transaction import _compat_wire
from .validation import fields, reject


ENGINE_VERSION = "0.2.0"


def executing_engine_version(registration):
    from .bootstrap_contract import ENGINE_DELEGATION_APIS
    return ENGINE_VERSION if registration.get("bootstrap_api") in ENGINE_DELEGATION_APIS else registration["updater_version"]


def resource_bindings(source, target):
    from backend.core.config import ConfigManager
    from backend.core.llm_config import LLMConfigManager
    from backend.core.storage.paths import _default_state_home
    result = {}
    if (source.config_schema_version, source.config_migration_sha256) != (target.config_schema_version, target.config_migration_sha256):
        result["config"] = ConfigManager.default_path()
    if (source.provider_schema_version, source.provider_migration_sha256) != (target.provider_schema_version, target.provider_migration_sha256):
        result.update(providers=LLMConfigManager._config_path().resolve(), secrets=LLMConfigManager._secret_path().resolve())
    # Config evidence also covers storage paths/runtime. Rehearse all known
    # databases when it changes, even if SQL migration numbers did not change.
    writer_changed = source.config_migration_sha256 != target.config_migration_sha256
    changed = [name for name in ("state", "observability") if writer_changed or
               getattr(source, name + "_migrations") != getattr(target, name + "_migrations")]
    if changed:
        homes = [_default_state_home()]
        if os.name == "nt" and not os.environ.get("GITGO_STATE_HOME") and os.environ.get("LOCALAPPDATA"):
            legacy = Path(os.environ["LOCALAPPDATA"]) / "Gitgo/state"
            if legacy.resolve() != homes[0] and legacy.exists(): homes.append(legacy.resolve())
        for index, home in enumerate(homes):
            stores = list((home / "projects").glob("*"))
            shared = home / "shared/knowledge"
            if shared.exists(): stores.append(shared)
            for store in sorted(stores):
                _canonical(store)
                if not store.is_dir(): continue
                if store != shared:
                    try: identity = uuid.UUID(store.name).hex
                    except ValueError: reject("UPDATE_DATA_PATH", "Unrecognized project storage identity")
                else: identity = "shared"
                for name in changed:
                    path = store / (name + ".sqlite3")
                    if not path.exists(): continue
                    key = f"db_{index}_{identity}_{name}"
                    result[key] = path
                    for suffix in ("wal", "shm"):
                        result[key + "_" + suffix] = Path(str(path) + "-" + suffix)
    return result


def prepare_transition(store, directory, source, target, bundle, *, resources=None):
    resources = resource_bindings(source, target) if resources is None else resources
    for path in resources.values():
        if not any(path.is_relative_to(root) for root in store.protected_paths):
            reject("UPDATE_DATA_PATH", "Data resource is outside the installation's protected profile")
    upgrade = DataUpgrade(directory, directory.name, resources)
    shadows = upgrade.prepare()
    plan = directory / "data-migrate-plan.json"
    nonce = uuid.uuid4().hex
    atomic_control(plan, {"schema_version": 1, "nonce": nonce, "transaction_id": directory.name,
                         "source": _compat_wire(source), "resources": {key: str(path) for key, path in shadows.items()}})
    from .health import migrate_candidate_data
    migrate_candidate_data(bundle, directory / "data-probe", plan, nonce)
    upgrade.validate()
    return upgrade


def reopen_transition(store, transaction):
    directory = store.root / "transactions" / transaction["id"]
    path = directory / JOURNAL
    if not path.exists() and not path.is_symlink(): return None
    value = fields(read_control(path), {"schema_version", "transaction_id", "state", "resources"})
    if type(value["resources"]) is not list or len(value["resources"]) > 256:
        reject("UPDATE_DATA_JOURNAL", "Data resource set is invalid")
    resources = {}
    for item in value["resources"]:
        if type(item) is not dict or type(item.get("name")) is not str or type(item.get("source")) is not str:
            reject("UPDATE_DATA_JOURNAL", "Data resource binding is invalid")
        path = _canonical(Path(item["source"]))
        if item["name"] in resources or not any(path.is_relative_to(root) for root in store.protected_paths):
            reject("UPDATE_DATA_PATH", "Data recovery resource differs from registered profile boundaries")
        resources[item["name"]] = path
    upgrade = DataUpgrade(directory, transaction["id"], resources)
    upgrade._read()
    return upgrade
