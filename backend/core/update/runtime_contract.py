"""Read-only fingerprints of actual data writers; never load user state.

Code fingerprints are deliberately conservative and Python-minor specific.
They are computed from the loaded runtime, not asserted by release metadata.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from types import CodeType

from .manifest import DataCompatibility
from .tree import encode
from .validation import reject


CONFIG_MODULES = (
    "backend.core.config", "backend.core.migrate", "backend.core.storage.paths",
    "backend.core.storage.runtime",
)
PROVIDER_MODULES = ("backend.core.llm_config", "backend.core.secret_store")


def _canonical_code(code: CodeType, module: str) -> dict:
    # Source and frozen loaders use different absolute filenames. Only that
    # provenance is normalized; bytecode/constants/flags/line tables remain.
    def constant(item):
        if isinstance(item, CodeType):
            return {"code": _canonical_code(item, module)}
        if type(item) is tuple:
            return {"tuple": [constant(value) for value in item]}
        if type(item) is frozenset:
            return {"frozenset": sorted((constant(value) for value in item), key=encode)}
        if type(item) is bytes:
            return {"bytes": item.hex()}
        if item is Ellipsis:
            return {"ellipsis": True}
        if item is None or type(item) in (str, bool, int):
            return {type(item).__name__: item}
        if type(item) in (float, complex):
            return {type(item).__name__: repr(item)}
        reject("UPDATE_RUNTIME_CONTRACT", "Runtime code has an unsupported constant")
    # Marshal preserves string interning/reference flags that change when a
    # code object is frozen/unmarshalled. A typed canonical representation
    # compares actual instructions rather than serializer implementation flags.
    return {"module": module, "name": code.co_name, "qualname": code.co_qualname,
            "argcount": code.co_argcount, "posonlyargcount": code.co_posonlyargcount,
            "kwonlyargcount": code.co_kwonlyargcount, "nlocals": code.co_nlocals,
            "stacksize": code.co_stacksize, "flags": code.co_flags, "code": code.co_code.hex(),
            "constants": [constant(value) for value in code.co_consts], "names": code.co_names,
            "varnames": code.co_varnames, "freevars": code.co_freevars, "cellvars": code.co_cellvars,
            "firstlineno": code.co_firstlineno, "linetable": code.co_linetable.hex(),
            "exceptiontable": code.co_exceptiontable.hex()}


def code_evidence(modules: tuple[str, ...]) -> bytes:
    fingerprints = {}
    for name in modules:
        spec = importlib.util.find_spec(name)
        loader = getattr(spec, "loader", None)
        if loader is None or not hasattr(loader, "get_code"):
            reject("UPDATE_RUNTIME_CONTRACT", "Runtime writer code cannot be inspected")
        code = loader.get_code(name)
        if not isinstance(code, CodeType):
            reject("UPDATE_RUNTIME_CONTRACT", "Runtime writer code is unavailable")
        fingerprints[name] = hashlib.sha256(encode(_canonical_code(code, name))).hexdigest()
    return encode({"format": "gitgo-runtime-code-v1", "python_cache_tag": sys.implementation.cache_tag,
                   "modules": fingerprints})


def runtime_evidence() -> tuple[dict, dict[str, bytes]]:
    """Return SQL and writer-code evidence from this exact runtime, without I/O.

    Importing code may read Python modules. No ConfigManager/LLM manager load,
    native keyring, application database, migration runner or user path lookup.
    """
    from backend.core.storage.migrations import STATE_MIGRATIONS, OBSERVABILITY_MIGRATIONS
    evidence = {}
    histories = {}
    for name, migrations in (("state", STATE_MIGRATIONS), ("observability", OBSERVABILITY_MIGRATIONS)):
        histories[name + "_migrations"] = []
        for item in migrations:
            data = item.sql.encode("utf-8")
            evidence[f"update-evidence/{name}-{item.version}.sql"] = data
            histories[name + "_migrations"].append({"version": item.version,
                                                     "sha256": hashlib.sha256(data).hexdigest()})
    evidence["update-evidence/config.json"] = code_evidence(CONFIG_MODULES)
    evidence["update-evidence/provider.json"] = code_evidence(PROVIDER_MODULES)
    # Bind schema version declarations from the same compiled code as writers.
    from backend.core.config import Config
    from backend.core.secret_store import EncryptedSecretStore
    compatibility = {**histories,
        "config_schema_version": Config.__dataclass_fields__["schema_version"].default,
        "config_migration_sha256": hashlib.sha256(evidence["update-evidence/config.json"]).hexdigest(),
        "provider_schema_version": EncryptedSecretStore.VERSION,
        "provider_migration_sha256": hashlib.sha256(evidence["update-evidence/provider.json"]).hexdigest()}
    DataCompatibility.parse(compatibility)
    return compatibility, evidence


def data_probe(nonce: str) -> dict:
    """Early Host role: memory-only SQLite health and real runtime fingerprints."""
    from .validation import text
    text(nonce, r"[0-9a-f]{32}", 32)
    from backend.core.storage.runtime import validate_in_memory_migrations
    import platform
    validate_in_memory_migrations()
    compatibility, _ = runtime_evidence()
    from backend.version import __version__
    target = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform)
    architecture = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower(),
                                                               platform.machine().lower())
    if target is None:
        reject("UPDATE_TARGET_INVALID", "Runtime platform is unsupported")
    return {"schema_version": 1, "nonce": nonce, "product_id": "gitgo-terminal",
            "version": __version__, "platform": target, "architecture": architecture,
            "data_compatibility": compatibility, "memory_sqlite_check": True}
