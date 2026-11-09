"""Pinned cloc recipe over a bounded copy of the authorized working tree."""
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from backend.core.sandbox import SandboxDenied
from backend.core.sandbox_read import ReadRoot, relative_parts
from backend.core.tools.file_operations import PROTECTED_METADATA_PARTS
from backend.core.tools.workspace_projection import WorkspaceImage, MAX_FILE_BYTES, MAX_TOTAL_BYTES
from .data import encode_json, text_digest

CLOC_VERSION = "2.10"
CLOC_SHA256 = "bf59272455172108072a0a106379f7509fd4349bdcfd85203bac038ccd286d83"
SCAN_VERSIONS = "workbench_code_size_versions"
_FLAGS = ("--quiet", "--json", "--by-file", "--skip-uniqueness",
          "--list-file=.cloc-list", "--ignored=.cloc-ignored", "--config=.cloc-config")
_EXCLUDED = PROTECTED_METADATA_PARTS | {
    ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build",
}
_ARCHIVES = (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".jar", ".war", ".ear",
             ".deb", ".rpm", ".7z", ".rar", ".gz", ".bz2", ".xz", ".z", ".apk", ".whl")
_METRICS = ("blank", "comment", "code")


class ClocUnavailable(ValueError):
    pass


def cloc_command(bundle=None):
    bundle = Path(bundle) if bundle is not None else Path(__file__).resolve().parents[2] / "resources" / "cloc"
    perl = bundle / "perl" / "bin" / ("perl.exe" if os.name == "nt" else "perl")
    script = bundle / "cloc.pl"
    if not perl.is_file() or not script.is_file() or not (bundle / "LICENSE.cloc").is_file():
        raise ClocUnavailable("The trusted cloc 2.10 bundle, Perl runtime or license is missing")
    if hashlib.sha256(script.read_bytes()).hexdigest() != CLOC_SHA256:
        raise ClocUnavailable("Bundled cloc does not match the pinned 2.10 digest")
    return [str(perl), str(script)]


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _directory(root, parts):
    target = root
    for part in parts:
        target /= part
        info = target.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Scan path must be an ordinary directory inside the workspace")
    return target


def _under(name, prefix):
    return not prefix or name == prefix or name.startswith(prefix + "/")


def _excluded(name, excluded):
    return any(part.casefold() in _EXCLUDED for part in name.split("/")) or any(
        _under(name, prefix) for prefix in excluded)


def _git_names(root, include_untracked):
    from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError
    args = ("ls-files", "-z", "--cached")
    if include_untracked:
        args += ("--others", "--exclude-standard")
    try:
        raw = AgentWorktreeManager._git_bytes(root, *args, timeout_seconds=12)
    except WorktreeError as exc:
        raise ValueError("Protected Git inventory is unavailable: " + str(exc)) from exc
    if len(raw) > 2_000_000:
        raise ValueError("Git inventory exceeds the code-size scan budget")
    return sorted({os.fsdecode(name) for name in raw.split(b"\0") if name})


def _directory_names(root, directory, excluded, cancel):
    stack, names, nodes = [directory], [], 0
    while stack:
        cancel()
        folder = stack.pop()
        parts = folder.relative_to(root).parts
        before = _directory(root, parts).lstat()
        with os.scandir(folder) as entries:
            for item in entries:
                nodes += 1
                if nodes > 8192:
                    raise ValueError("Directory inventory exceeds 8192 entries")
                name = Path(item.path).relative_to(root).as_posix()
                if _excluded(name, excluded):
                    continue
                info = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not _is_link(info):
                    stack.append(Path(item.path))
                else:
                    names.append(name)
        after = _directory(root, parts).lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("A directory changed identity during inventory")
    return sorted(names)


def _capture(root, directory, excluded, git, untracked, cancel):
    parts = directory.relative_to(root).parts
    prefix = "/".join(parts)
    _directory(root, parts)
    names = _git_names(root, untracked) if git else _directory_names(root, directory, excluded, cancel)
    selected = [name for name in names if _under(name, prefix) and not _excluded(name, excluded)]
    if len(selected) > 512:
        raise ValueError("Code-size scans are limited to 512 paths. Choose a narrower directory.")
    files, skipped, size = {}, {}, 0
    with ReadRoot(root) as acquired:
        for name in selected:
            cancel()
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                skipped[name.encode("utf-8", errors="backslashreplace").decode()] = "filename_not_utf8"
                continue
            try:
                parts = relative_parts(name)
                _directory(root, parts[:-1])
                info = (root.joinpath(*parts)).lstat()
                if _is_link(info) or not stat.S_ISREG(info.st_mode):
                    skipped[name] = "non_regular_or_link"
                    continue
                if name.casefold().endswith(_ARCHIVES):
                    skipped[name] = "archive_excluded"
                    continue
                with acquired.open(name) as opened:
                    raw = opened.read_bytes(max_bytes=MAX_FILE_BYTES)
            except FileNotFoundError:
                skipped[name] = "missing"
                continue
            except (OSError, ValueError, RuntimeError):
                skipped[name] = "unreadable_or_outside_scope"
                continue
            size += len(raw)
            if size > MAX_TOTAL_BYTES:
                raise ValueError("Code-size inputs exceed the private workspace byte budget")
            files[name] = (hashlib.sha256(raw).hexdigest(), raw)
    return files, skipped


def _image(files):
    mapping, contents = {}, []
    for index, (path, (_, raw)) in enumerate(files.items()):
        # Keep ASCII language-significant basenames and extensions. Mapping
        # restores the original paths without relying on Perl's path encoding.
        basename = re.sub(r"[^a-zA-Z0-9_. -]", "_", path.rsplit("/", 1)[-1])
        staged = f"scan-inputs/{index:04d}/{basename}"
        mapping[staged] = path
        contents.append((staged, raw, 0o600))
    contents.append((".cloc-list", "\n".join(mapping).encode("ascii"), 0o600))
    contents.append((".cloc-config", b"", 0o600))
    return WorkspaceImage(tuple(contents), ()), mapping


def execute_cloc(args):
    try:
        return _run_cloc(cloc_command(), Path(args["_workspace"]), args["staged_paths"])
    except ClocUnavailable as exc:
        return {"error": "WORKBENCH_UNAVAILABLE", "detail": str(exc)}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"error": "WORKBENCH_INVALID_DATA", "detail": str(exc)[:500]}


def _run_cloc(command, workspace, staged_paths):
    from backend.core.tools.execution_profile import safe_environment
    options = {"cwd": workspace, "capture_output": True, "timeout": 40, "input": b"",
               "env": safe_environment()}
    # Do not let a user/system Perl configuration select extra code or modules.
    options["env"] = {k: v for k, v in options["env"].items() if not k.upper().startswith("PERL")}
    options["env"]["PATH"] = str(Path(command[0]).parent)
    version = subprocess.run([*command, "--version"], **options)
    if version.returncode or version.stdout.strip() != CLOC_VERSION.encode():
        raise ValueError("The managed cloc executable did not report version 2.10")
    if not staged_paths:
        return {"counts": {}, "unprocessed": {}}
    run = subprocess.run([*command, *_FLAGS], **options)
    if run.returncode or len(run.stdout) > 350_000:
        raise ValueError("cloc failed or exceeded its report budget")
    report = json.loads(run.stdout) if run.stdout.strip() else {}
    ignored = {}
    ignored_file = workspace / ".cloc-ignored"
    if ignored_file.is_file():
        rows = json.loads(ignored_file.read_text(encoding="utf-8"))
        # cloc 2.10 serializes an empty ignored report as [{}].
        if rows != [{}]:
            ignored = {_normal_path(row["file"]): row["reason"].strip()[:160] for row in rows}
    counts = {_normal_path(path): row for path, row in report.items() if path not in {"header", "SUM"}}
    if not counts.keys() <= set(staged_paths) or (not report and not ignored):
        raise ValueError("cloc report did not identify the declared inputs")
    unprocessed = {path: ignored.get(path) or "cloc_did_not_classify_or_count" for path in staged_paths if path not in counts}
    totals = report.get("SUM")
    if counts and (not isinstance(totals, dict) or totals.get("nFiles") != len(counts)
                   or any(totals.get(metric) != sum(row[metric] for row in counts.values()) for metric in _METRICS)):
        raise ValueError("cloc summary disagrees with its per-file counts")
    return {"counts": counts, "unprocessed": unprocessed}


def _normal_path(name):
    return name.replace("\\", "/").removeprefix("./")


def scan(runtime, args, *, timeout, diagnostics):
    from backend.core.errors import error_payload
    try:
        cloc_command()
    except ClocUnavailable as exc:
        return error_payload("WORKBENCH_UNAVAILABLE", message=str(exc), next_actions=[
            {"action": "provide_managed_cloc_bundle", "effect": "Stage the reviewed platform bundle through the existing build process."}])
    root = Path(runtime.workspace).resolve(strict=True)
    parts = () if args["path"] == "." else relative_parts(args["path"])
    excluded = sorted({"/".join(relative_parts(value)) for value in args.get("exclude", [])})
    if parts and _excluded("/".join(parts), excluded):
        raise ValueError("The requested directory is excluded from this scan")
    directory = _directory(root, parts)
    git = os.path.lexists(root / ".git")
    untracked = args.get("include_untracked", False)
    if not git and untracked:
        raise ValueError("include_untracked only applies to Git workspaces")
    deadline = time.monotonic() + timeout

    def cancel():
        if runtime.ctx.is_cancelled() or runtime.ctx.process.cancel_requested or not runtime.is_current():
            raise SandboxDenied("TOOL_CANCELLED", "Task changed or was cancelled during code-size capture")
        if time.monotonic() >= deadline:
            raise SandboxDenied("TOOL_TIMEOUT", "Code-size scan exhausted its time budget")

    original, skipped = _capture(root, directory, excluded, git, untracked, cancel)
    image, mapping = _image(original)
    cancel()
    frame = runtime.preview("workbench_cloc", {"staged_paths": list(mapping)}, deadline - time.monotonic(),
        trusted_arguments={}, copy_inputs=False, diagnostics=diagnostics, input_image=image)
    result = runtime._confirmed(frame)
    if result.get("error"):
        return result if result.get("error_info") else error_payload("WORKBENCH_INVALID_DATA",
            message=result.get("detail") or "cloc execution failed")
    counted, omitted = result["counts"], result["unprocessed"]
    if set(counted).intersection(omitted) or set(counted) | set(omitted) != set(mapping):
        raise ValueError("Native cloc output coverage does not match the input manifest")

    after, after_skipped = _capture(root, directory, excluded, git, untracked, cancel)
    cancel()
    observed_at = time.time_ns()
    versions = {name: value[0] for name, value in original.items()}
    after_versions = {name: value[0] for name, value in after.items()}
    changed = sorted(name for name in versions.keys() | after_versions.keys() | skipped.keys() | after_skipped.keys()
                     if (versions.get(name), skipped.get(name)) != (after_versions.get(name), after_skipped.get(name)))
    state = ("unstable" if changed else "incomplete" if any(
        reason in {"missing", "unreadable_or_outside_scope", "filename_not_utf8"}
        for reason in skipped.values()) else "stable")
    rows, languages = [], defaultdict(lambda: {"files": 0, **{metric: 0 for metric in _METRICS}})
    for staged, item in counted.items():
        language = item["language"]
        if (not isinstance(language, str) or not language or
                any(type(item.get(metric)) is not int or item[metric] < 0 for metric in _METRICS)):
            raise ValueError("cloc returned invalid per-file metrics")
        row = {"path": mapping[staged], "language": language, **{metric: item[metric] for metric in _METRICS}}
        rows.append(row)
        languages[language]["files"] += 1
        for metric in _METRICS:
            languages[language][metric] += row[metric]
    scope = {"path": "/".join(parts) or ".", "mode": "git_tracked" if git else "directory",
             "include_untracked": untracked, "excluded_prefixes": excluded,
             "excluded_directory_names": sorted(_EXCLUDED), "excluded_archive_suffixes": list(_ARCHIVES),
             "options": list(_FLAGS), "duplicates": "retain_each_path"}
    scope_key = text_digest(encode_json(scope))
    digest = text_digest(encode_json({"files": versions, "skipped": skipped}))
    record = {"kind": "gitgo.workbench.cloc.v1", "snapshot_state": state,
              "cloc_version": CLOC_VERSION, "cloc_source_sha256": CLOC_SHA256,
              "scope": scope, "scope_key": scope_key, "snapshot_sha256": digest,
              "input_sha256_by_path": versions, "changed_during_scan": changed,
              "files": sorted(rows, key=lambda row: row["path"]), "languages": dict(languages),
              "unprocessed": {**skipped, **{mapping[path]: reason for path, reason in omitted.items()}},
              "coverage": "Recognized files in the declared snapshot. The scan did not execute project code."}
    return runtime.publish(record, {"files_counted": len(rows), "scope_key": scope_key},
                           scan_version=(scope_key, digest, observed_at))
