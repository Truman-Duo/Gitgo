"""Candidate-owned data conversion on shadow resources; no model/keychain I/O.

Future incompatible formats add compiled migration adapters here or in their
own storage/config component. No arbitrary shell/Python/SQL from a manifest.
"""
import json
from pathlib import Path

from .manifest import DataCompatibility
from .runtime_contract import runtime_evidence
from .validation import fields, reject, text
from backend.core.config_extensions import preserve_fields


def _json(path):
    if path.stat().st_size > 16 * 1024**2:
        reject("UPDATE_DATA_SIZE", "Configuration exceeds migration bounds")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: reject("UPDATE_DATA_INVALID", "Duplicate configuration keys")
            value[key] = item
        return value
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if type(value) is not dict: reject("UPDATE_DATA_INVALID", "Configuration must be an object")
    return value


def migrate_shadows(resources, source):
    """Run only inside already authenticated, independently pinned candidate."""
    target, _ = runtime_evidence()
    target = DataCompatibility.parse(target)
    if "config" in resources and resources["config"].exists():
        from backend.core.config import Config, _serialize_config
        path = resources["config"]; raw = _json(path)
        if source.config_schema_version > target.config_schema_version:
            reject("UPDATE_DATA_ADAPTER_REQUIRED", "A configuration downgrade adapter is required")
        converted = _serialize_config(Config.from_dict(raw))
        if converted["schema_version"] != target.config_schema_version:
            reject("UPDATE_DATA_ADAPTER_REQUIRED", "Configuration schema migration is unavailable")
        # Unused/retired fields stay available; removing a feature does not
        # silently erase its configuration. The full original is retained too.
        path.write_text(json.dumps(preserve_fields(raw, converted), ensure_ascii=False, indent=2), encoding="utf-8")
    if "providers" in resources and resources["providers"].exists():
        raw = _json(resources["providers"])
        if type(raw.get("providers", [])) is not list:
            reject("UPDATE_DATA_INVALID", "Provider configuration is invalid")
    if "secrets" in resources and resources["secrets"].exists():
        from backend.core.secret_store import EncryptedSecretStore
        # Validate the encrypted envelope only. This never reads a native
        # credential, hydrates a Provider, or creates a replacement secret.
        EncryptedSecretStore._read_payload(resources["secrets"])
    if source.provider_schema_version != target.provider_schema_version:
        reject("UPDATE_DATA_ADAPTER_REQUIRED", "A credential format adapter is required")
    if any(name.startswith("db_") for name in resources):
        import shutil
        from backend.core.storage.upgrade import migrate_shadow_database
        for name, path in resources.items():
            if not name.startswith("db_") or name.endswith(("_wal", "_shm")) or not path.exists():
                continue
            kind = "observability" if name.endswith("_observability") else "state"
            # The old WAL must be replayed into the NEW main database. SHM is
            # reconstructible and is not reused as an authoritative index.
            wal = resources.get(name + "_wal")
            if wal is not None and wal.exists():
                shutil.copyfile(wal, Path(str(path) + "-wal"))
            migrate_shadow_database(path, kind)
            for suffix in ("wal", "shm"):
                shadow = resources.get(name + "_" + suffix)
                if shadow is not None and shadow.exists():
                    shadow.write_bytes(b"")


def main(arguments):
    import argparse
    from backend.core.protocol_io import dump_protocol_json
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args(arguments)
    from .persistence import read_control
    plan = fields(read_control(Path(args.plan)), {"schema_version", "nonce", "transaction_id", "source", "resources"})
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        reject("UPDATE_DATA_INVALID", "Unsupported migration request")
    text(plan["nonce"], r"[0-9a-f]{32}", 32)
    text(plan["transaction_id"], r"[0-9a-f]{32}", 32)
    resources = {}
    if type(plan["resources"]) is not dict or len(plan["resources"]) > 256:
        reject("UPDATE_DATA_INVALID", "Migration resource set is invalid")
    for name, raw in plan["resources"].items():
        text(name, r"[a-z][a-z0-9_.-]{0,63}", 64)
        path = Path(raw)
        if (not path.is_absolute() or path != path.resolve() or path.is_symlink()
                or not path.name.endswith(".gitgo-upgrade-" + plan["transaction_id"] + ".new")):
            reject("UPDATE_DATA_PATH", "Migration can only target shadow generations")
        resources[name] = path
    migrate_shadows(resources, DataCompatibility.parse(plan["source"]))
    print(dump_protocol_json({"schema_version": 1, "nonce": plan["nonce"], "migration_verified": True}), flush=True)
    return 0
