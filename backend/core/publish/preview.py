"""Bounded, immutable integration previews. No checkout, index or target writes."""
from backend.core.sandbox_read import relative_parts
from . import formal
from .git import OID
from .state import PublishError

METHODS = {"ff", "squash", "merge", "rebase", "cherry_pick", "partial"}


def commit(git, tree, parents, message):
    parents = list(dict.fromkeys(parents))
    result = git.run(["commit-tree", tree, *(value for parent in parents for value in ("-p", parent)), "-m", message])
    oid = result.stdout.strip()
    if result.returncode or not OID.fullmatch(oid):
        raise PublishError("PUBLISH_COMMIT_UNAVAILABLE")
    return oid


def merge(git, left, right, *, base=""):
    result = git.run(["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z",
                      *(["--merge-base=" + base] if base else []), left, right])
    if result.returncode not in (0, 1):
        raise PublishError("PUBLISH_MERGE_UNAVAILABLE")
    fields = result.stdout.split("\0")
    tree = fields[0]
    if not OID.fullmatch(tree):
        raise PublishError("PUBLISH_MERGE_UNAVAILABLE")
    paths = list(dict.fromkeys(name for name in fields[1:] if name))
    if len(paths) > 4096 or any(len(name) > 4096 for name in paths):
        raise PublishError("PUBLISH_TREE_LIMIT")
    # Exit status is authoritative, including conflicts without file stages.
    return {"left": left, "right": right, "base": base, "tree_oid": tree,
            "conflicted": result.returncode == 1, "conflict_paths": paths}


def range_oids(git, base, head, *, limit=128):
    result = git.run(["rev-list", "--reverse", "--topo-order", "--max-count=" + str(limit + 1), base + ".." + head])
    rows = result.stdout.split()
    if result.returncode or len(rows) > limit or any(not OID.fullmatch(oid) for oid in rows):
        raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
    return rows


def pin(service, git, target, primary, additional):
    if not isinstance(additional, list) or len(additional) > 7:
        raise PublishError("PUBLISH_INVALID_CONTRIBUTIONS")
    candidates, seen = [], set()
    for args in [primary, *additional]:
        if (not isinstance(args, dict) or set(args) - {"source_role", "source_ref", "base_oid"}
                or not {"source_role", "source_ref"} <= args.keys()):
            raise PublishError("PUBLISH_INVALID_CONTRIBUTIONS")
        candidate, _ = service._source(args["source_role"]).prepare(args["source_ref"], args.get("base_oid", ""))
        candidate["role"] = args["source_role"]
        git.import_candidate(candidate)
        if candidate["oid"] in seen:
            raise PublishError("PUBLISH_DUPLICATE_CONTRIBUTION")
        seen.add(candidate["oid"])
        bases = git.run(["merge-base", "--all", target, candidate["oid"]])
        base_oids = bases.stdout.split()
        if bases.returncode or not base_oids or len(base_oids) > 8 or any(not OID.fullmatch(oid) for oid in base_oids):
            raise PublishError("PUBLISH_UNRELATED_HISTORY")
        if args.get("base_oid") and candidate["base_oid"] not in base_oids:
            raise PublishError("PUBLISH_BASE_CHANGED")
        candidate.update(base_oid=base_oids[0], merge_bases=sorted(base_oids))
        candidates.append(candidate)
    return candidates


def selected_paths(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 512
            or any(not isinstance(name, str) for name in value) or len(set(value)) != len(value)):
        raise PublishError("PUBLISH_INVALID_SELECTION")
    try:
        for name in value:
            if not isinstance(name, str) or "\\" in name or len(name) > 4096:
                raise ValueError()
            relative_parts(name)
    except (ValueError, TypeError, OSError) as exc:
        raise PublishError("PUBLISH_INVALID_SELECTION") from exc
    return value


def build(git, target, candidates, method, *, selected_commits=None, paths=None):
    """Return fixed intermediate trees and source identities, even on conflict."""
    if method not in METHODS:
        raise PublishError("PUBLISH_INVALID_METHOD")
    if (len(candidates) > 1 and method not in {"squash", "merge"}
            or selected_commits and method != "cherry_pick" or paths and method != "partial"):
        raise PublishError("PUBLISH_INVALID_SELECTION")
    first = candidates[0]
    series, steps, chosen = [], [], []
    if method == "ff":
        if git.run(["merge-base", "--is-ancestor", target, first["oid"]]).returncode:
            raise PublishError("PUBLISH_TARGET_DIVERGED")
        return {"tree_oid": first["tree_oid"], "steps": [], "series": [], "selected_commits": range_oids(git, target, first["oid"])}
    current = target
    if method in {"squash", "merge"}:
        for candidate in candidates:
            # Whole contributions use Git's recursive merge-base computation.
            step = merge(git, current, candidate["oid"])
            steps.append(step)
            chosen.extend(range_oids(git, target, candidate["oid"]))
            if step["conflicted"]:
                break
            current = commit(git, step["tree_oid"], [current, candidate["oid"]], "Gitgo private merge preview")
    else:
        if len(first["merge_bases"]) != 1:
            raise PublishError("PUBLISH_BASE_AMBIGUOUS")
        available = range_oids(git, first["base_oid"], first["oid"], limit=32)
        if method == "partial":
            paths = selected_paths(paths)
            before = {row[0]: row for row in formal.tree_files(git, first["base_oid"])}
            after = {row[0]: row for row in formal.tree_files(git, first["oid"])}
            changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
            if not set(paths) <= changed:
                raise PublishError("PUBLISH_INVALID_SELECTION")
            subset = dict(before)
            for name in paths:
                if name in after:
                    subset[name] = after[name]
                else:
                    subset.pop(name, None)
            partial_tree = formal.write_tree(git, list(subset.values()))
            steps.append(merge(git, target, partial_tree, base=first["base_oid"]))
            chosen = available
        else:
            chosen = available if method == "rebase" else selected_commits
            if (not isinstance(chosen, list) or not 1 <= len(chosen) <= 32
                    or any(not isinstance(oid, str) or oid not in available for oid in chosen)
                    or len(set(chosen)) != len(chosen)):
                raise PublishError("PUBLISH_INVALID_SELECTION")
            previous = first["base_oid"]
            for oid in chosen:
                parents = git.run(["rev-list", "--parents", "-n", "1", oid])
                row = parents.stdout.split()
                if parents.returncode or len(row) != 2 or method == "rebase" and row[1] != previous:
                    raise PublishError("PUBLISH_NONLINEAR_SELECTION")
                step = merge(git, current, oid, base=row[1])
                step["source_oid"] = oid
                steps.append(step)
                if step["conflicted"]:
                    break
                series.append({"source_oid": oid, "tree_oid": step["tree_oid"]})
                current = commit(git, step["tree_oid"], [current], "Gitgo private replay preview")
                previous = oid
    if not steps:
        raise PublishError("PUBLISH_NO_FORMAL_CHANGES")
    chosen = list(dict.fromkeys(chosen))
    if len(chosen) > 128:
        raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
    return {"tree_oid": steps[-1]["tree_oid"], "steps": steps, "series": series, "selected_commits": chosen,
            "conflicted": steps[-1]["conflicted"], "conflict_paths": steps[-1]["conflict_paths"]}


def audit_history(git, target, candidates, configuration):
    """Retained history must pass privacy and numbered-message rules in full."""
    from backend.core.operations.git import validate_commit_message
    import re
    commits = list(dict.fromkeys(oid for candidate in candidates for oid in range_oids(git, target, candidate["oid"])))
    if len(commits) > 128:
        raise PublishError("PUBLISH_COMMIT_RANGE_UNSUPPORTED")
    prefix = configuration["template"].get("prefix_override") or configuration["commit_format"].get("prefix", "PROJ")
    numbers, scanned_bytes = set(), 0
    for oid in commits:
        files = formal.tree_files(git, oid)
        scanned_bytes += sum(len(row[3]) for row in files)
        if scanned_bytes > 256 * 1024 * 1024:
            raise PublishError("PUBLISH_HISTORY_LIMIT")
        formal.filtered_tree(git, files, configuration, "ff")
        message = git.run(["show", "-s", "--format=%B", oid])
        numbered = re.match(r"\[" + re.escape(prefix) + r"-(\d+)\]", message.stdout)
        if message.returncode or validate_commit_message(message.stdout) or not numbered or numbered[1] in numbers:
            raise PublishError("PUBLISH_COMMIT_FORMAT_INVALID")
        numbers.add(numbered[1])
        if (configuration["authorship"].get("mode", "mixed") != "mixed"
                and configuration["authorship"].get("strip_commit_coauthors", True)
                and formal.strip_authorship_from_message(message.stdout) != message.stdout):
            raise PublishError("PUBLISH_FF_REQUIRES_FILTERING")
        if formal.privacy(files, configuration, message.stdout)["outcome"] != "passed":
            raise PublishError("PUBLISH_HISTORY_PRIVACY_BLOCKED")
