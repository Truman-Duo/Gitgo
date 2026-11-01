"""Snapshot commit plugins without importing their code in the Host."""
import base64
import os
from pathlib import Path
import re

from backend.core.sandbox_read import ReadRoot
from .state import PublishError, digest


def capture(project):
    enabled = (project.commit_format or {}).get("plugins", [])
    if (not isinstance(enabled, list) or len(enabled) > 8
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) for name in enabled)
            or len(set(enabled)) != len(enabled)):
        raise PublishError("PUBLISH_INVALID_PLUGIN_POLICY")
    if not enabled:
        return []
    from backend.core.plugin_loader import discover, _default_search_paths
    # Keep the existing loader's precedence, including its project override.
    registry = discover([*_default_search_paths(), Path(project.workspace_path) / ".gitgo" / "plugins"])
    result, total = [], 0
    try:
        for name in enabled:
            info = registry.get(name)
            if info is None:
                raise PublishError("PUBLISH_PLUGIN_MISSING")
            path = info.filepath
            if path.is_symlink() or path.is_junction():
                raise PublishError("PUBLISH_PLUGIN_SOURCE_UNSUPPORTED")
            package = path.is_dir()
            root = path if package else path.parent
            paths = []
            if package:
                visited = 0
                for directory, dirs, files in os.walk(root, followlinks=False):
                    visited += 1
                    if visited > 64 or len(dirs) + len(files) > 128:
                        raise PublishError("PUBLISH_PLUGIN_SOURCE_LIMIT")
                    for child in [*dirs, *files]:
                        member = Path(directory) / child
                        if member.is_symlink() or member.is_junction():
                            raise PublishError("PUBLISH_PLUGIN_SOURCE_UNSUPPORTED")
                    dirs[:] = [d for d in dirs if d != "__pycache__"]
                    paths.extend(Path(directory) / f for f in files if not f.endswith((".pyc", ".pyo")))
                    if len(paths) > 64:
                        raise PublishError("PUBLISH_PLUGIN_SOURCE_LIMIT")
            else:
                paths = [path]
            contents = {}
            with ReadRoot(root) as acquired:
                for file in sorted(paths):
                    relative = file.relative_to(root).as_posix()
                    with acquired.open(relative) as opened:
                        raw = opened.read_bytes(max_bytes=512 * 1024)
                    total += len(raw)
                    if total > 1024 * 1024:
                        raise PublishError("PUBLISH_PLUGIN_SOURCE_LIMIT")
                    contents[relative] = base64.b64encode(raw).decode("ascii")
            snapshot = {"id": name, "package": package, "entry": "__init__.py" if package else path.name, "files": contents}
            result.append({**snapshot, "digest": digest(snapshot)})
    except (OSError, RuntimeError) as exc:
        raise PublishError("PUBLISH_PLUGIN_SOURCE_UNAVAILABLE") from exc
    return result


def identities(snapshots):
    return [{"id": snapshot["id"], "digest": snapshot["digest"]} for snapshot in snapshots]


def validate_entries(proposal, original):
    """A hook may select/order existing paths, never substitute their identities."""
    known = {row["rel_path"]: row for row in original}
    if not isinstance(proposal, list) or len(proposal) > len(original):
        raise PublishError("PUBLISH_PLUGIN_PROTOCOL_INVALID")
    selected, seen = [], set()
    for row in proposal:
        if not isinstance(row, dict) or not isinstance(row.get("rel_path"), str):
            raise PublishError("PUBLISH_PLUGIN_PROTOCOL_INVALID")
        name = row["rel_path"]
        if name not in known or name in seen or type(row.get("selected")) is not bool:
            raise PublishError("PUBLISH_PLUGIN_PROTOCOL_INVALID")
        if any(row.get(key) != value for key, value in known[name].items() if key != "selected"):
            raise PublishError("PUBLISH_PLUGIN_PROTOCOL_INVALID")
        seen.add(name)
        selected.append({**known[name], "selected": row["selected"]})
    return selected


def validate_selection(selection, bundle):
    if not isinstance(selection, dict) or set(selection) != {"paths", "commits"}:
        raise PublishError("PUBLISH_PLUGIN_PROTOCOL_INVALID")
    for key, allowed in (("paths", [e["rel_path"] for e in bundle["entries"] if e["selected"]]),
                         ("commits", [c["hash"] for c in bundle["selected"]])):
        values = selection[key]
        if (not isinstance(values, list) or not values or len(values) > len(allowed)
                or any(not isinstance(value, str) or value not in allowed for value in values)
                or len(set(values)) != len(values)):
            raise PublishError("PUBLISH_PLUGIN_INVALID_SELECTION")
    return selection


def entries(before, after):
    """JSON-compatible FileEntry observations from fixed object trees."""
    import hashlib
    old = {name: (blob, raw) for name, _, blob, raw in before}
    new = {name: (blob, raw) for name, _, blob, raw in after}
    return [{"rel_path": name, "status": "new" if name not in old else "deleted" if name not in new else "modified",
             "old_path": "", "selected": True,
             "workspace_hash": hashlib.sha256(new[name][1]).hexdigest() if name in new else "",
             "backup_hash": hashlib.sha256(old[name][1]).hexdigest() if name in old else ""}
            for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name)]


def selected_commits(git, target, candidate, *, oids=None):
    raw = (b"".join(git.binary(["show", "-s", "--format=%H%x00%s%x00%b", "-z", oid]) for oid in oids)
           if oids is not None else git.binary(["log", "--format=%H%x00%s%x00%b", "-z", "--reverse", "--max-count=129", target + ".." + candidate]))
    if len(raw) > 256 * 1024:
        raise PublishError("PUBLISH_PLUGIN_INPUT_LIMIT")
    fields = raw.decode("utf8").split("\0")
    if fields[-1] != "" or (len(fields) - 1) % 3 or not 1 <= (len(fields) - 1) // 3 <= 128:
        raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
    result = []
    for index in range(0, len(fields) - 1, 3):
        oid, subject, body = fields[index:index + 3]
        clean = re.sub(r"^\[[A-Z0-9]+-\d+\]\s*", "", subject)
        match = re.match(r"^(feat|fix|docs|style|refactor|perf|test|chore)(?:\(([^)]*)\))?:\s*(.*)", clean)
        result.append({"hash": oid, "subject": match[3] if match else clean, "type": match[1] if match else "chore",
                       "scope": match[2] if match else None, "body": body})
    return result


def validate_message(message, plan, configuration, files):
    from backend.core.operations.git import validate_commit_message
    from .formal import privacy, strip_authorship_from_message
    if not isinstance(message, str) or not message.strip() or len(message) > 24000:
        raise PublishError("PUBLISH_COMMIT_FORMAT_INVALID")
    message = message.strip()
    if plan["method"] == "ff":
        if message != plan["commit_message"]:
            raise PublishError("PUBLISH_PLUGIN_REQUIRES_SQUASH")
    else:
        prefix = configuration["template"].get("prefix_override") or configuration["commit_format"].get("prefix", "PROJ")
        numbered = re.match(r"\[" + re.escape(prefix) + r"-(\d+)\]", message)
        if validate_commit_message(message) or not numbered or int(numbered[1]) != plan["number"]:
            raise PublishError("PUBLISH_COMMIT_FORMAT_INVALID")
    if (configuration["authorship"].get("mode", "mixed") != "mixed"
            and configuration["authorship"].get("strip_commit_coauthors", True)
            and strip_authorship_from_message(message) != message):
        raise PublishError("PUBLISH_PLUGIN_AUTHORSHIP_BLOCKED")
    if privacy(files, configuration, message)["outcome"] != "passed":
        raise PublishError("PUBLISH_PLUGIN_PRIVACY_BLOCKED")
    return message
