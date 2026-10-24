"""Legacy argument adapters for the trusted file service, not native writes."""
from copy import deepcopy
from dataclasses import replace
import codecs
import hashlib

from . import workspace_tools
from .file_operations import FILE_ALIASES, metadata_mutation_error


def _failure(code, **facts):
    return {"error": code, "effect_state": "not_committed", "execution_state": "not_started", **facts}


def file_write(args):
    mode = args.get("mode", "w")
    if mode not in {"w", "a"}:
        return _failure("INVALID_MODE", mode=mode, allowed=["w", "a"])
    try:
        encoding = codecs.lookup(str(args.get("encoding", "utf-8"))).name
    except LookupError:
        return _failure("UNSUPPORTED_ENCODING", encoding=str(args.get("encoding")))
    if encoding != "utf-8":
        return _failure("UNSUPPORTED_ENCODING", encoding=encoding, supported=["utf-8"])
    prepared = {**args, "create_only": False}
    try:
        if mode == "a":
            from backend.core.sandbox_read import ReadRoot
            workspace = workspace_tools._workspace(args)
            target = workspace_tools._confined_path(workspace, args.get("path", ""), must_exist=False,
                allowed_roots=workspace_tools._allowed_roots(args))
            denied = metadata_mutation_error(workspace, args.get("path", "")) or metadata_mutation_error(workspace, target)
            if denied:
                return denied
            if target.exists():
                # The legacy interface cannot turn a blind append into a CAS
                # by silently supplying a version the model has never seen.
                with ReadRoot(target.parent) as root, root.open(target.name) as opened:
                    raw = opened.read_bytes()
                actual = hashlib.sha256(raw).hexdigest()
                if not args.get("expected_sha256"):
                    return _failure("EXPECTED_HASH_REQUIRED", path=str(target), actual_sha256=actual)
                if args["expected_sha256"] != actual:
                    return _failure("FILE_CHANGED", path=str(target), actual_sha256=actual)
                prepared["content"] = raw.decode("utf-8") + str(args.get("content", ""))
    except (OSError, UnicodeError) as exc:
        return _failure("READ_ERROR", detail=str(exc))
    # Only source acquisition above is known to have no target effects. An
    # exception from the commit service must retain Pipeline's effect-unknown
    # handling rather than being relabelled as a pre-write read failure.
    result = workspace_tools.write_file(prepared)
    if mode == "a" and not result.get("error"):
        result["action"] = "appended"
    return result


def file_edit(args):
    if not args.get("expected_sha256"):
        return _failure("EXPECTED_HASH_REQUIRED", path=str(args.get("path", "")))
    return workspace_tools.edit_file(args)


def file_delete(args):
    return workspace_tools.delete_file(args)


def build_legacy_file_tools(tools):
    """Old sessions keep their names; current capability profiles stay canonical."""
    result = {}
    for alias, canonical in FILE_ALIASES.items():
        original = tools[canonical]
        schema = deepcopy(original.parameters)
        if alias == "file_write":
            schema["properties"].pop("create_only", None)
            schema["properties"].update(mode={"type": "string", "enum": ["w", "a"], "default": "w"},
                encoding={"type": "string", "description": "UTF-8 only; aliases share the canonical text candidate contract."})
        result[alias] = replace(original, name=alias, runner_name=alias, parameters=schema,
            description="Compatibility alias for " + canonical + ". " + original.description,
            execute=globals()[alias])
    return result
