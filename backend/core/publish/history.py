"""Read bounded Git ancestry and patches for a saved Publish scope.

No ref resolution, fetch, storage writer, arbitrary object lookup or project
program is part of these queries. Parent edges come from hashed commit bytes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager, nullcontext

from .git import OID
from .state import PublishError, digest, encoded, find_item, identity, read_ledger
from . import history_cache

MAX_COMMIT_BYTES = 65536
MAX_PAGE_BYTES = 8 * 1024 * 1024


def history_scope(project, item_id, head="candidate"):
    """Derive the one authorized repository without opening its Git objects."""
    ledger, _ = read_ledger(project.workspace_path)
    item = find_item(ledger, item_id)
    if head == "formal":
        plan = item["plan"]
        if plan.get("destination") != "formal":
            raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
        role, path, repository = "formal", project.backup_path, plan["repository"]
        results = [op for op in item.get("operations", []) if op.get("kind") == "formal"
                   and op.get("state") == "observed" and op.get("binding") == item["binding"]]
        oid = results[-1]["result_oid"] if results else plan["expected_target_oid"]
    else:
        if head == "candidate":
            source = item["candidate"]
        elif isinstance(head, str) and re.fullmatch(r"contribution:[0-7]", head):
            sources = item["plan"].get("contributions", [])
            index = int(head.split(":")[1])
            if index >= len(sources):
                raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
            source = sources[index]
        else:
            raise PublishError("PUBLISH_INVALID_HISTORY_HEAD")
        role, repository, oid = source["role"], source["repository"], source["oid"]
        if role not in {"workspace", "trial"}:
            raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
        path = project.workspace_path if role == "workspace" else project.trial_path
    if not path or identity(path) != repository:
        raise PublishError("PUBLISH_HISTORY_SCOPE_CHANGED")
    if not isinstance(oid, str) or not OID.fullmatch(oid):
        raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
    scope = {"item_id": item_id, "head": head, "role": role, "repository": repository, "head_oid": oid}
    return {**scope, "snapshot_id": digest({**scope, "binding": item["binding"]})}


@contextmanager
def _session(repository):
    from backend.core.loop.git_execution import protected_git_read_session, GitBoundaryError
    try:
        with protected_git_read_session(repository) as session:
            yield session
    except GitBoundaryError as exc:
        raise PublishError(exc.code) from exc
    except OSError as exc:
        raise PublishError("PUBLISH_HISTORY_UNAVAILABLE") from exc


def _run(repository, args, *, input_text="", limit=MAX_PAGE_BYTES, truncate=False, session=None):
    """Use the existing pinned Git boundary and drain pipes with a hard bound."""
    from backend.core.loop.git_execution import GitBoundaryError
    from backend.core.process_control import spawn_managed, terminate_tree, close_job, finish_scope, creation_flags
    from backend.core.sandbox_io import BoundedCommunication
    from backend.core.sandbox import SandboxDenied
    try:
        with (nullcontext(session) if session is not None else _session(repository)) as (command_for, environment):
            command = command_for(["git", *args])
            # Replacement refs and lazy fetch are disabled by protected_git.
            # Legacy grafts must not invent ancestry for these access checks.
            environment = {**environment, "GIT_GRAFT_FILE": os.devnull}
            options = {"cwd": repository, "env": environment, "stdin": subprocess.PIPE,
                       "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
                       "encoding": "utf8", "close_fds": True, "creationflags": creation_flags()}
            if os.name == "posix" and hasattr(command, "pass_fds"):
                options["pass_fds"] = command.pass_fds
            proc = spawn_managed(command, **options)
            capture = BoundedCommunication(proc, limit=limit)
            truncated = False
            try:
                capture.communicate(input=input_text, timeout=15)
            except SandboxDenied as exc:
                if exc.code != "SANDBOX_OUTPUT_LIMIT" or not truncate or len(capture.output[0]) < limit:
                    raise PublishError("PUBLISH_HISTORY_OUTPUT_LIMIT") from exc
                truncated = True
            except subprocess.TimeoutExpired as exc:
                raise PublishError("PUBLISH_HISTORY_TIMEOUT") from exc
            finally:
                if proc.poll() is None or capture.exceeded.is_set():
                    terminate_tree(proc, getattr(proc, "_gitgo_job_handle", None))
                else:
                    finish_scope(proc)
                close_job(getattr(proc, "_gitgo_job_handle", None))
                proc._gitgo_job_handle = None
                for thread in capture.threads:
                    thread.join(timeout=2)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    stream.close()
            return proc.returncode, bytes(capture.output[0]), truncated
    except GitBoundaryError as exc:
        raise PublishError(exc.code) from exc
    except OSError as exc:
        raise PublishError("PUBLISH_HISTORY_UNAVAILABLE") from exc


def _require(repository, args, **kwargs):
    code, raw, truncated = _run(repository, args, **kwargs)
    if code and not truncated:
        raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
    return raw, truncated


def commit_bytes(repository, oid):
    """Read one fixed commit with the existing bounded, pinned Git reader."""
    if not isinstance(oid, str) or OID.fullmatch(oid) is None:
        raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
    raw, _ = _require(repository, ["cat-file", "commit", oid], limit=MAX_COMMIT_BYTES)
    algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
    if algorithm(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != oid:
        raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
    return raw


def _commits(repository, oids, session=None):
    if not oids:
        return []
    raw, _ = _require(repository, ["cat-file", "--batch"], input_text="\n".join(oids) + "\n", session=session)
    rows, offset = [], 0
    for oid in oids:
        end = raw.find(b"\n", offset)
        header = raw[offset:end].split(b" ")
        if end < 0 or len(header) != 3 or header[:2] != [oid.encode(), b"commit"] or not header[2].isdigit():
            raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
        size = int(header[2])
        if size > MAX_COMMIT_BYTES:
            raise PublishError("PUBLISH_HISTORY_OUTPUT_LIMIT")
        content = raw[end + 1:end + 1 + size]
        offset = end + size + 2
        if len(content) != size or raw[offset - 1:offset] != b"\n":
            raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
        algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        if algorithm(b"commit " + str(size).encode() + b"\0" + content).hexdigest() != oid:
            raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
        headers, separator, message = content.partition(b"\n\n")
        parents, author = [], None
        for line in headers.split(b"\n"):
            if line.startswith(b"parent "):
                parent = line[7:].decode("ascii", errors="replace")
                if not OID.fullmatch(parent):
                    raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
                parents.append(parent)
            elif line.startswith(b"author "):
                author = re.fullmatch(rb"author (.*) <([^<>]*)> (-?\d{1,16}) ([+-]\d{4})", line)
        if not separator or author is None:
            raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
        subject = " ".join(message.decode("utf8", errors="replace").split("\n\n", 1)[0].splitlines())[:512]
        rows.append({"oid": oid, "parents": parents, "subject": subject,
                     "author": {"name": author[1].decode("utf8", errors="replace")[:512],
                                "email": author[2].decode("utf8", errors="replace")[:512],
                                "timestamp": int(author[3])}})
    if offset != len(raw):
        raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
    return rows


def _header(scope):
    return {key: value for key, value in scope.items() if key != "repository"}


def _stable(project, scope):
    if history_scope(project, scope["item_id"], scope["head"]) != scope:
        raise PublishError("PUBLISH_SNAPSHOT_CHANGED")


def history_page(project, *, item_id, head="candidate", limit=50, cursor=""):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise PublishError("PUBLISH_INVALID_LIMIT")
    if not isinstance(cursor, str) or len(cursor) > 2048:
        raise PublishError("PUBLISH_INVALID_CURSOR")
    scope = history_scope(project, item_id, head)
    offset = 0
    if cursor:
        try:
            value = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            if (not isinstance(value, dict) or set(value) != {"snapshot", "offset"}
                    or type(value["offset"]) is not int or not 0 <= value["offset"] <= 1_000_000):
                raise ValueError
            offset = value["offset"]
        except (ValueError, UnicodeError) as exc:
            raise PublishError("PUBLISH_INVALID_CURSOR") from exc
        if value["snapshot"] != scope["snapshot_id"]:
            raise PublishError("PUBLISH_SNAPSHOT_CHANGED")
    cache_key = ("history", scope["repository"], scope["snapshot_id"], offset, limit)
    cached = history_cache.get(cache_key)
    if cached is not None:
        return cached
    with _session(scope["repository"]) as session:
        raw, _ = _require(scope["repository"], ["log", "--topo-order", "--format=%H",
            f"--max-count={limit + 1}", f"--skip={offset}", scope["head_oid"], "--"], limit=16384, session=session)
        oids = raw.decode("ascii", errors="replace").splitlines()
        if len(oids) > limit + 1 or any(not OID.fullmatch(oid) for oid in oids):
            raise PublishError("PUBLISH_HISTORY_UNAVAILABLE")
        rows = _commits(scope["repository"], oids[:limit], session=session)
    next_cursor = base64.urlsafe_b64encode(encoded({"snapshot": scope["snapshot_id"],
        "offset": offset + len(rows)})).decode("ascii") if len(oids) > limit else None
    _stable(project, scope)
    result = {**_header(scope), "schema_version": 1, "relation": "git_parent", "commits": rows,
              "next_cursor": next_cursor, "scope": "available_local_ancestry", "cache": "miss"}
    history_cache.put(cache_key, result)
    return result


def diff_view(project, *, item_id, base_oid, head_oid="", head="candidate", limit_bytes=262144, snapshot_id=""):
    if type(limit_bytes) is not int or not 1024 <= limit_bytes <= 1048576:
        raise PublishError("PUBLISH_INVALID_LIMIT")
    scope = history_scope(project, item_id, head)
    if snapshot_id and snapshot_id != scope["snapshot_id"]:
        raise PublishError("PUBLISH_SNAPSHOT_CHANGED")
    head_oid = head_oid or scope["head_oid"]
    if (not isinstance(head_oid, str) or not OID.fullmatch(head_oid)
            or not isinstance(base_oid, str) or base_oid and not OID.fullmatch(base_oid)):
        raise PublishError("PUBLISH_INVALID_SOURCE")
    cache_key = ("diff", scope["repository"], scope["snapshot_id"], head_oid, base_oid, limit_bytes)
    cached = history_cache.get(cache_key)
    if cached is not None:
        return cached
    repository = scope["repository"]
    with _session(repository) as session:
        for oid in dict.fromkeys([head_oid, *([base_oid] if base_oid else [])]):
            code, _, _ = _run(repository, ["merge-base", "--is-ancestor", oid, scope["head_oid"]], limit=16384, session=session)
            if code:
                # Do not distinguish missing, private/unrelated and non-commit objects.
                raise PublishError("PUBLISH_HISTORY_OUTSIDE_SCOPE")
        options = ["--no-color", "--no-renames", "--unified=3", "--src-prefix=a/", "--dst-prefix=b/"]
        if base_oid:
            args = ["diff", *options, base_oid, head_oid, "--"]
        else:
            if _commits(repository, [head_oid], session=session)[0]["parents"]:
                raise PublishError("PUBLISH_HISTORY_BASE_REQUIRED")
            args = ["diff-tree", "--root", "--no-commit-id", "-r", "-p", *options, head_oid, "--"]
        raw, truncated = _require(repository, args, limit=limit_bytes + 1, truncate=True, session=session)
    truncated = truncated or len(raw) > limit_bytes
    text = raw[:limit_bytes].decode("utf8", errors="replace")
    _stable(project, scope)
    result = {**_header(scope), "scope_head_oid": scope["head_oid"], "head_oid": head_oid,
              "base_oid": base_oid, "text": text, "truncated": truncated, "limit_bytes": limit_bytes, "cache": "miss"}
    history_cache.put(cache_key, result)
    return result
