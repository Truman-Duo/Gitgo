"""Project bounded text patches outside the formal workspace.

Git parses/applies data in an owned, repository-free scratch directory. No
project configuration, index, hooks, filters or formal target are passed to it.
Only immutable candidate bytes cross back to the common file commit service.
"""
from __future__ import annotations

import codecs
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from .file_mutation import FileMutation
from .file_operations import protected_metadata_part
from .file_mutation_batch import FileMutationBatch

_MAX_PATCH = 2 * 1024 * 1024
_MAX_BYTES = 16 * 1024 * 1024
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class UnsupportedPatch(ValueError):
    pass


def _header(raw):
    value = raw.split("\t", 1)[0]
    if value.startswith('"'):
        if not value.endswith('"'):
            raise ValueError("incomplete quoted path")
        value = codecs.escape_decode(value[1:-1].encode("utf-8"))[0].decode("utf-8")
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    parts = value.split("/")
    if (not value or PurePosixPath(value).is_absolute() or "\\" in value or ":" in value
            or any(not x or x in {".", ".."} or protected_metadata_part(x) for x in parts)
            or any(ord(c) < 32 for c in value)):
        raise ValueError("unsafe or protected patch path")
    return value


def _paths(patch):
    # Consume hunk line counts so a removed line resembling a file header is
    # never interpreted as another target. Git remains the syntax authority.
    lines = patch.splitlines()
    paths = set()
    i = pairs = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise ValueError("missing paired file header")
            old, new = _header(line[4:]), _header(lines[i + 1][4:])
            if old is None and new is None:
                raise ValueError("both paths are absent")
            paths.update(x for x in (old, new) if x is not None)
            pairs += 1
            i += 2
        elif line.startswith("@@ "):
            match = _HUNK.match(line)
            if match is None or not pairs:
                raise ValueError("invalid hunk header")
            old = int(match[2]) if match[2] is not None else 1
            new = int(match[4]) if match[4] is not None else 1
            i += 1
            while old or new:
                if i >= len(lines):
                    raise ValueError("incomplete hunk")
                content = lines[i]
                if content.startswith("\\ No newline at end of file"):
                    i += 1
                    continue
                if not content or content[0] not in " +-":
                    raise ValueError("invalid hunk line")
                old -= content[0] in " -"
                new -= content[0] in " +"
                if old < 0 or new < 0:
                    raise ValueError("hunk count mismatch")
                i += 1
        elif line.startswith(("old mode ", "new mode ", "new file mode ", "deleted file mode ")):
            if line.rsplit(" ", 1)[-1] != "100644":
                raise UnsupportedPatch("file mode and symlink patches require a separate supported candidate representation")
            i += 1
        elif line.startswith(("GIT binary patch", "Binary files ")):
            raise UnsupportedPatch("binary patches require a binary-aware candidate checker")
        elif not line or line.startswith(("diff --git ", "index ", "similarity index ", "dissimilarity index ", "rename from ", "rename to ", "\\ No newline at end of file")):
            i += 1
        else:
            raise ValueError("unexpected patch metadata")
    if not pairs or not paths or len(paths) > 128:
        raise ValueError("a patch needs paired headers for 1..128 files")
    return sorted(paths)


def project_patch(args):
    from .workspace_tools import _workspace, _confined_path
    from backend.core.loop.git_execution import _environment, _executable, _invoke, GitBoundaryError
    from backend.core.sandbox_read import ReadRoot
    workspace = _workspace(args)
    patch = str(args.get("patch", ""))
    def failed(code, detail):
        return {"error": code, "detail": detail[:4000], "effect_state": "not_committed", "execution_state": "not_started"}
    if not patch.strip():
        return failed("EMPTY_PATCH", "no patch supplied")
    if len(patch.encode("utf-8")) > _MAX_PATCH:
        return failed("PATCH_UNSUPPORTED", "patch exceeds the 2 MiB projection limit")
    try:
        paths = _paths(patch)
        before = {}
        used = 0
        for relative in paths:
            target = _confined_path(workspace, relative, must_exist=False)
            if target != workspace / relative or target.is_symlink() or target.is_dir():
                raise UnsupportedPatch("patch targets must be regular workspace files without symlink parents")
            if target.exists():
                if target.stat().st_size + used > _MAX_BYTES:
                    raise UnsupportedPatch("patch inputs exceed 16 MiB")
                with ReadRoot(workspace) as acquired, acquired.open(relative) as opened:
                    raw = opened.read_bytes()
                raw.decode("utf-8-sig")
                if b"\x00" in raw:
                    raise UnsupportedPatch("binary inputs require a binary-aware candidate checker")
                before[relative] = raw
                used += len(raw)
        git = _executable(workspace)
        with tempfile.TemporaryDirectory(prefix="gitgo-patch-") as temporary:
            scratch = Path(temporary).resolve()
            if scratch.is_relative_to(workspace):
                raise UnsupportedPatch("patch scratch must be outside the formal workspace")
            environment = _environment()
            environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CEILING_DIRECTORIES=str(scratch.parent))
            command = [git, "-c", "core.autocrlf=false", "-c", "core.attributesFile=" + os.devnull,
                       "apply", "--no-index", "--whitespace=nowarn"]
            metadata = _invoke(command + ["--numstat", "-z", "-"], cwd=scratch,
                               env=environment, input_bytes=patch.encode("utf-8"), timeout=30)
            if metadata.returncode:
                return failed("PATCH_CHECK_FAILED", metadata.stderr.decode("utf-8", "replace"))
            for record in metadata.stdout.split(b"\x00"):
                if not record:
                    continue
                added, removed, name = record.split(b"\t", 2)
                if name.decode("utf-8") not in paths or added == b"-" or removed == b"-":
                    raise UnsupportedPatch("Git metadata differs from the declared text targets")
            for relative, raw in before.items():
                target = scratch / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            projected = _invoke(command + ["-"], cwd=scratch, env=environment,
                                input_bytes=patch.encode("utf-8"), timeout=30)
            if projected.returncode:
                return failed("PATCH_CHECK_FAILED", projected.stderr.decode("utf-8", "replace"))
            actual = set()
            for target in scratch.rglob("*"):
                if target.is_symlink():
                    raise UnsupportedPatch("symlink output is not a text file candidate")
                if target.is_file():
                    actual.add(target.relative_to(scratch).as_posix())
            if actual - set(paths):
                raise UnsupportedPatch("projection produced an undeclared file")
            members, used = [], 0
            for relative in paths:
                target = scratch / relative
                after = target.read_bytes() if target.is_file() else None
                old = before.get(relative)
                if old == after:
                    continue
                used += len(after or b"")
                if used > _MAX_BYTES:
                    raise UnsupportedPatch("patch outputs exceed 16 MiB")
                if after is not None and b"\x00" in after:
                    raise UnsupportedPatch("binary output is not a text file candidate")
                members.append(FileMutation("delete_file" if after is None else "write_file", workspace,
                    workspace / relative, hashlib.sha256(old).hexdigest() if old is not None else "",
                    old.decode("utf-8-sig") if old is not None else "",
                    after.decode("utf-8-sig") if after is not None else "", after_bytes=after))
            if not members:
                return failed("PATCH_CHECK_FAILED", "patch has no content changes")
            return FileMutationBatch(workspace, tuple(members))
    except (UnsupportedPatch, UnicodeError) as exc:
        return failed("PATCH_UNSUPPORTED", str(exc))
    except GitBoundaryError as exc:
        return failed(exc.code, str(exc))
    except subprocess.TimeoutExpired:
        return failed("PATCH_TIMEOUT", "private patch projection exceeded its allowance")
    except (ValueError, OSError) as exc:
        return failed("PATCH_CHECK_FAILED", str(exc))
