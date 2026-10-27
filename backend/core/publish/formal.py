"""Fixed Formal trees, existing privacy rules and numbered commit templates."""
from dataclasses import asdict
import hashlib
import re

from backend.core.authorship import (is_ai_config_file, private_content_fingerprint,
    should_exclude_outbound_file, scan_privacy, strip_authorship_from_message, strip_authorship_from_code)
from backend.core.operations.security import scan_diff_for_secrets
from backend.core.tools.workspace_projection import WorkspaceImage, MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES
from backend.core.sandbox_read import relative_parts
from backend.core.template_manager import TemplateManager
from .git import OID
from .state import PublishError, digest


def policy(project):
    from .checks import normalize
    from .plugins import capture, identities
    settings = project.commit_format or {}
    template = TemplateManager.get_template(settings.get("template_name", "default"))
    if template is None:
        raise PublishError("PUBLISH_TEMPLATE_UNAVAILABLE")
    if (not isinstance(template.header_format, str) or not isinstance(template.body_format, str)
            or not isinstance(template.prefix_override or settings.get("prefix", "PROJ"), str)):
        raise PublishError("PUBLISH_COMMIT_FORMAT_INVALID")
    return {"commit_format": settings, "template": asdict(template),
            "outbound_policy": project.outbound_policy or {}, "authorship": project.authorship or {},
            "publish_checks": normalize(project.publish_checks), "plugins": identities(capture(project))}


def tree_files(git, oid):
    """Read immutable blobs by full object identity, never from a checkout."""
    listing = git.binary(["ls-tree", "-r", "-l", "-z", oid])
    rows, total, names = [], 0, {}
    for row in filter(None, listing.split(b"\0")):
        metadata, raw_name = row.split(b"\t", 1)
        mode, kind, blob, size = metadata.split()
        try:
            name = raw_name.decode("utf8")
            relative_parts(name)
            if "\\" in name or any(part in {"", ".", ".."} for part in name.split("/")):
                raise ValueError("Nonportable tree name")
            count = int(size)
        except (ValueError, UnicodeError, OSError) as exc:
            raise PublishError("PUBLISH_TREE_UNSUPPORTED") from exc
        if mode not in {b"100644", b"100755"} or kind != b"blob" or not OID.fullmatch(blob.decode("ascii")):
            raise PublishError("PUBLISH_TREE_UNSUPPORTED")
        # A case alias is not a reproducible Windows test/export input.
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            folded = spelling.casefold()
            if folded in names and names[folded] != spelling:
                raise PublishError("PUBLISH_TREE_UNSUPPORTED")
            names[folded] = spelling
        if count > MAX_FILE_BYTES or len(rows) >= MAX_FILES:
            raise PublishError("PUBLISH_TREE_LIMIT")
        total += count
        if total > MAX_TOTAL_BYTES:
            raise PublishError("PUBLISH_TREE_LIMIT")
        rows.append((name, mode.decode(), blob.decode(), count))
    if not rows:
        raise PublishError("PUBLISH_EMPTY_TREE")
    raw = git.binary(["cat-file", "--batch"], b"".join(row[2].encode() + b"\n" for row in rows))
    position, files = 0, []
    for name, mode, blob, count in rows:
        end = raw.find(b"\n", position)
        if end < 0 or raw[position:end] != f"{blob} blob {count}".encode():
            raise PublishError("PUBLISH_TREE_UNAVAILABLE")
        content = raw[end + 1:end + 1 + count]
        position = end + count + 2
        if len(content) != count or raw[position - 1:position] != b"\n":
            raise PublishError("PUBLISH_TREE_UNAVAILABLE")
        actual = hashlib.new("sha1" if len(blob) == 40 else "sha256", f"blob {count}\0".encode() + content).hexdigest()
        if actual != blob:
            raise PublishError("PUBLISH_TREE_UNAVAILABLE")
        files.append((name, mode, blob, content))
    if position != len(raw):
        raise PublishError("PUBLISH_TREE_UNAVAILABLE")
    return files


def image(files):
    directories = set()
    for name, *_ in files:
        parts = name.split("/")
        directories.update("/".join(parts[:n]) for n in range(1, len(parts)))
    return WorkspaceImage(tuple((name, raw, 0o755 if mode == "100755" else 0o644)
                                for name, mode, _, raw in files), tuple(sorted(directories)))


def filtered_tree(git, files, configuration, method):
    outbound = configuration["outbound_policy"]
    fingerprints = {private_content_fingerprint(raw) for name, _, _, raw in files if is_ai_config_file(name)}
    selected, omitted = [], []
    for row in files:
        name, _, _, raw = row
        if any(p.casefold() in {".git", ".gitgo"} for p in name.split("/")) or should_exclude_outbound_file(
                name, raw, known_fingerprints=fingerprints, exclude_patterns=outbound.get("exclude_paths")):
            omitted.append(hashlib.sha256(name.encode()).hexdigest())
        else:
            authorship = configuration["authorship"]
            if authorship.get("mode", "mixed") != "mixed" and authorship.get("strip_code_comments") and b"\0" not in raw:
                try:
                    transformed = strip_authorship_from_code(raw.decode("utf8"), aggressive=True).encode("utf8")
                except UnicodeError:
                    transformed = raw  # The established filter operates on UTF-8 text only.
                if transformed != raw:
                    if method == "ff":
                        raise PublishError("PUBLISH_FF_REQUIRES_FILTERING")
                    blob = git.binary(["hash-object", "-w", "--stdin"], transformed).decode().strip()
                    if not OID.fullmatch(blob):
                        raise PublishError("PUBLISH_TREE_UNAVAILABLE")
                    row = (name, row[1], blob, transformed)
            selected.append(row)
    if omitted and method == "ff":
        raise PublishError("PUBLISH_FF_REQUIRES_FILTERING")
    if not selected:
        raise PublishError("PUBLISH_EMPTY_TREE")
    return write_tree(git, selected), selected, omitted


def write_tree(git, files):
    root = {}
    for name, mode, blob, _ in files:
        node = root
        parts = name.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise PublishError("PUBLISH_INVALID_SELECTION")
        if parts[-1] in node:
            raise PublishError("PUBLISH_INVALID_SELECTION")
        node[parts[-1]] = (mode, blob)
    def write(node):
        entries = []
        for name, value in sorted(node.items()):
            mode, kind, oid = ("040000", "tree", write(value)) if isinstance(value, dict) else (value[0], "blob", value[1])
            entries.append(f"{mode} {kind} {oid}\t{name}".encode("utf8") + b"\0")
        oid = git.binary(["mktree", "-z"], b"".join(entries)).decode().strip()
        if not OID.fullmatch(oid):
            raise PublishError("PUBLISH_TREE_UNAVAILABLE")
        return oid
    return write(root)


def privacy(files, configuration, message=""):
    outbound = configuration["outbound_policy"]
    approved = set(outbound.get("approved_fingerprints") or [])
    blockers = []
    for name, _, _, raw in [*files, ("commit-message.txt", "100644", "", message.encode())]:
        # Same content scanner as Alpha3; binary bytes are decoded for secret
        # markers without claiming a semantic inspection of binary formats.
        content = raw.decode("utf8", errors="replace")
        alerts = scan_privacy(name, content, level=max(2, int(outbound.get("content_level", 2))),
                              deep_scan=bool(outbound.get("deep_scan")))
        blockers.extend(a["rule"] for a in alerts if a.get("level") == "error" and not any(
            e.get("sha256") in approved for e in a.get("evidence", [])))
        lines = content.splitlines()
        diff = f"+++ b/{name}\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)
        blockers.extend(a["rule_id"] for a in scan_diff_for_secrets(diff, config=outbound)
                        if a.get("match_fingerprint") not in approved)
    return {"kind": "outbound_privacy", "scope": "final_tree_and_message", "outcome": "failed" if blockers else "passed",
            "summary": ", ".join(sorted(set(blockers))) or "No blocking privacy finding"}


def commit_message(project, git, target, candidate, configuration, workspace, *, selected=None, revisions=None):
    from backend.core.operations.git import build_commit_template, _find_next_number, validate_commit_message
    from backend.core.operations.models import CommitInfo
    from backend.core.sync_session.state import reserve_formal_number
    prefix = configuration["template"].get("prefix_override") or configuration["commit_format"].get("prefix", "PROJ")
    if not re.fullmatch(r"[A-Z0-9]+", prefix):
        raise PublishError("PUBLISH_COMMIT_FORMAT_INVALID")
    if selected is None:
        subjects = git.run(["log", "--format=%s", "--reverse", "--max-count=129", target + ".." + candidate])
        rows = subjects.stdout.splitlines()
        if subjects.returncode:
            raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
    else:
        if not 1 <= len(selected) <= 128 or any(not OID.fullmatch(oid) for oid in selected):
            raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
        rows = []
        for oid in selected:
            subject = git.run(["show", "-s", "--format=%s", oid])
            if subject.returncode:
                raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
            rows.append(subject.stdout.strip())
    if not rows or len(rows) > 128:
        raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
    commits = []
    for subject in rows:
        subject = re.sub(r"^\[[A-Z0-9]+-\d+\]\s*", "", subject)
        match = re.match(r"^(feat|fix|docs|style|refactor|perf|test|chore)(?:\(([^)]*)\))?:\s*(.*)", subject)
        commits.append(CommitInfo(hash="", subject=match[3] if match else subject,
                                  type=match[1] if match else "chore", scope=match[2] if match else None, body=""))
    floor = max([int(configuration["commit_format"].get("number_start", 0)), *(
        _find_next_number(prefix=prefix, git_runner=git.runner, workspace_path=workspace, revision=oid)
        for oid in set(revisions or [target, candidate]))])
    number = reserve_formal_number(workspace, floor)
    try:
        message = build_commit_template(commits, project, git_runner=git.runner, number=number, require_template=True)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise PublishError("PUBLISH_COMMIT_FORMAT_INVALID") from exc
    message = "\n".join(line for line in message.splitlines() if not line.startswith("#")).strip()
    if configuration["authorship"].get("mode", "mixed") != "mixed" and configuration["authorship"].get("strip_commit_coauthors", True):
        message = strip_authorship_from_message(message)
    if validate_commit_message(message) or len(message) > 24000:
        raise PublishError("PUBLISH_COMMIT_FORMAT_INVALID")
    return message, number


def policy_binding(project):
    return digest(policy(project))
