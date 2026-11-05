"""Closed native SyncPlugin lifecycle handler; outputs are proposals, never writes."""
import base64
from contextlib import chdir
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

from backend.core.plugin import SyncPlugin
from backend.core.publish.state import digest
from backend.core.sandbox_read import relative_parts


def run_publish_plugins(args):
    root = Path(args["_workspace"]).resolve()
    # Native launch starts in the owned runtime installation. Hooks must see
    # only their disposable project view as cwd, like registered project tests.
    with chdir(root):
        return _run_publish_plugins(args, root)


def _run_publish_plugins(args, root):
    folder = root / ".gitgo" / "publish-plugins"
    instances, observations, phase = [], [], "materialize"
    try:
        for index, snapshot in enumerate(args["bundle"]["plugins"]):
            body = {key: value for key, value in snapshot.items() if key != "digest"}
            if digest(body) != snapshot["digest"]:
                return {"error": "PUBLISH_PLUGIN_SNAPSHOT_INVALID"}
            location = folder / str(index)
            for name, raw in snapshot["files"].items():
                relative_parts(name)
                path = location / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(raw, validate=True))
            module_name = "gitgo_publish_plugin_" + str(index)
            spec = importlib.util.spec_from_file_location(module_name, location / snapshot["entry"],
                submodule_search_locations=[str(location)] if snapshot["package"] else None)
            if spec is None or spec.loader is None:
                return {"error": "PUBLISH_PLUGIN_LOAD_FAILED", "plugin": snapshot["id"]}
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            phase = "import"
            spec.loader.exec_module(module)
            cls = getattr(module, "plugin_class", None)
            if not isinstance(cls, type) or not issubclass(cls, SyncPlugin):
                return {"error": "PUBLISH_PLUGIN_LOAD_FAILED", "plugin": snapshot["id"]}
            instances.append((snapshot["id"], cls()))
        if args.get("phase") == "complete":
            phase = "complete"
            for name, plugin in instances:
                try:
                    plugin.on_sync_complete(deepcopy(args["completion"]))
                    observations.append({"plugin": name, "hook": "on_sync_complete", "outcome": "passed"})
                except Exception as exc:
                    observations.append({"plugin": name, "hook": "on_sync_complete", "outcome": "failed", "error_type": type(exc).__name__})
            if any(row["outcome"] == "failed" for row in observations):
                return {"error": "PUBLISH_PLUGIN_LOAD_OR_HOOK_FAILED", "summary": "Completion callback failed", "observations": observations}
            return {"passed": True, "observations": observations}
        entries, selected = deepcopy(args["entries"]), deepcopy(args["bundle"]["selected"])
        phase = "scan"
        for name, plugin in instances:
            proposal = plugin.on_scan_complete(deepcopy(entries))
            if proposal is not None and not isinstance(proposal, list):
                return {"error": "PUBLISH_PLUGIN_PROTOCOL_INVALID", "plugin": name}
            if proposal:
                from backend.core.publish.plugins import validate_entries
                entries = validate_entries(proposal, entries)
            observations.append({"plugin": name, "hook": "on_scan_complete", "outcome": "passed"})
        phase = "select"
        for name, plugin in instances:
            indices = plugin.on_commit_select(deepcopy(selected))
            if indices is not None and (not isinstance(indices, list) or any(type(i) is not int for i in indices)):
                return {"error": "PUBLISH_PLUGIN_PROTOCOL_INVALID", "plugin": name}
            if indices:
                if len(indices) != len(set(indices)) or any(i < 0 or i >= len(selected) for i in indices):
                    return {"error": "PUBLISH_PLUGIN_PROTOCOL_INVALID", "plugin": name}
                selected = [selected[i] for i in indices]
                break  # The established loader uses the first nonempty recommendation.
        selection = {"paths": sorted(e["rel_path"] for e in entries if e["selected"]),
                     "commits": [c["hash"] for c in selected]}
        if selection != {"paths": sorted(e["rel_path"] for e in args["entries"] if e["selected"]),
                         "commits": [c["hash"] for c in args["bundle"]["selected"]]}:
            return {"passed": True, "selection": selection, "observations": observations}
        message, suggested = args["message"], False
        phase = "message"
        for name, plugin in instances:
            if not suggested:
                proposal = plugin.on_commit_message(deepcopy(selected), deepcopy(args["bundle"]["project_config"]))
                if proposal is not None and not isinstance(proposal, str):
                    return {"error": "PUBLISH_PLUGIN_PROTOCOL_INVALID", "plugin": name}
                if proposal:
                    if len(proposal) > 24000:
                        return {"error": "PUBLISH_PLUGIN_PROTOCOL_INVALID", "plugin": name}
                    message, suggested = proposal, True
        for name, plugin in instances:
            phase = "pre_sync"
            blocked = plugin.on_sync_start(deepcopy(entries), message)
            if blocked is not None and not isinstance(blocked, str):
                return {"error": "PUBLISH_PLUGIN_PROTOCOL_INVALID", "plugin": name}
            observations.append({"plugin": name, "hook": "on_sync_start", "outcome": "blocked" if blocked else "passed"})
            if blocked:
                return {"error": "PUBLISH_PLUGIN_BLOCKED", "plugin": name, "summary": blocked[:4000], "observations": observations}
        return {"passed": True, "message": message, "observations": observations}
    except Exception as exc:
        # Plugin exceptions are failures, never fallbacks to the default message.
        return {"error": getattr(exc, "code", "PUBLISH_PLUGIN_LOAD_OR_HOOK_FAILED"), "summary": phase + ": " + type(exc).__name__}
