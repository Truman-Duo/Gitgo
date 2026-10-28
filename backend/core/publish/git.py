"""Fixed, noninteractive local Git operations; no raw subprocess fallback."""
import re

from backend.adapters.local_git_runner import ProtectedLocalGitRunner
from .state import PublishError, identity

OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class RefWriteRefused(PublishError):
    """The fixed ref transaction was not entered, or Git rejected it."""


def branch_ref(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 160
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
            or ".." in value or "//" in value
            or any(part.startswith(".") or part.endswith((".lock", ".")) or not part
                   for part in value.split("/"))):
        raise PublishError("PUBLISH_INVALID_BRANCH")
    return "refs/heads/" + value


def source_ref(value):
    if value == "HEAD" or isinstance(value, str) and OID.fullmatch(value):
        return value
    if isinstance(value, str) and value.startswith("refs/"):
        branch_ref(value[5:])
        return value
    raise PublishError("PUBLISH_INVALID_SOURCE")


class CandidateGit:
    def __init__(self, workspace):
        self.workspace = identity(workspace)
        self.runner = ProtectedLocalGitRunner(self.workspace)

    def run(self, args, *, author=None):
        return self.runner.run(args, timeout=30, **({"author_identity": author} if author is not None else {}))

    def binary(self, args, payload=None):
        result = self.runner.run(args, timeout=60, text=False, input_bytes=payload)
        if result.returncode:
            raise PublishError("PUBLISH_GIT_UNAVAILABLE")
        return result.stdout

    def require_target(self, ref, expected):
        symbolic = self.run(["symbolic-ref", "--quiet", ref])
        if symbolic.returncode != 1:
            raise PublishError("PUBLISH_TARGET_UNAVAILABLE")
        current = self.target(ref)
        if not current or current["symbolic"] or current["oid"] != expected:
            raise PublishError("PUBLISH_TARGET_CHANGED")
        worktrees = self.run(["worktree", "list", "--porcelain"])
        if worktrees.returncode:
            raise PublishError("PUBLISH_TARGET_UNAVAILABLE")
        if "branch " + ref in worktrees.stdout.splitlines():
            raise PublishError("PUBLISH_TARGET_CHECKED_OUT")

    def integrate(self, candidate, plan, result_oid):
        try:
            self.import_candidate({**candidate, "oid": result_oid, "tree_oid": plan["tree_oid"]})
            self.require_target(plan["target_ref"], plan["expected_target_oid"])
        except (PublishError, OSError, RuntimeError) as exc:
            raise RefWriteRefused(getattr(exc, "code", "PUBLISH_GIT_UNAVAILABLE")) from exc
        return self.run(["update-ref", "--no-deref", "-m", "gitgo publish formal",
                         plan["target_ref"], result_oid, plan["expected_target_oid"]])

    def oid(self, ref, kind="commit"):
        result = self.run(["rev-parse", "--verify", "--end-of-options", ref + "^{" + kind + "}"])
        value = result.stdout.strip()
        if result.returncode or OID.fullmatch(value) is None:
            raise PublishError("PUBLISH_SOURCE_UNAVAILABLE")
        return value

    def prepare(self, ref, base_oid=""):
        oid = self.oid(source_ref(ref))
        resolved_ref = ref if ref.startswith("refs/") else ""
        if ref == "HEAD" or ref.startswith("refs/"):
            symbolic = self.run(["symbolic-ref", "--quiet", ref])
            if symbolic.returncode not in (0, 1):
                raise PublishError("PUBLISH_SOURCE_UNAVAILABLE")
            if symbolic.returncode == 0:
                resolved_ref = source_ref(symbolic.stdout.strip())
                if self.oid(resolved_ref) != oid:
                    raise PublishError("PUBLISH_SOURCE_CHANGED")
        tree = self.oid(oid, "tree")
        if base_oid:
            if not isinstance(base_oid, str) or not OID.fullmatch(base_oid):
                raise PublishError("PUBLISH_INVALID_BASE")
            base = self.oid(base_oid)
        else:
            parent = self.run(["rev-list", "--parents", "-n", "1", oid])
            if parent.returncode or not parent.stdout.split():
                raise PublishError("PUBLISH_SOURCE_UNAVAILABLE")
            parents = parent.stdout.split()
            base = parents[1] if len(parents) > 1 else oid
        ancestor = self.run(["merge-base", "--is-ancestor", base, oid])
        if ancestor.returncode:
            raise PublishError("PUBLISH_BASE_NOT_ANCESTOR")
        check = self.run(["diff-tree", "--root", "--check", "-r", oid, "--"] if base == oid
                         else ["diff", "--check", base, oid, "--"])
        if check.returncode not in (0, 1, 2):
            raise PublishError("PUBLISH_CHECK_UNAVAILABLE")
        return {"repository": self.workspace, "source_ref": ref, "resolved_source_ref": resolved_ref, "oid": oid,
                "tree_oid": tree, "base_oid": base}, {
            "kind": "git_diff_check", "outcome": "passed" if check.returncode == 0 else "failed",
            "summary": (check.stdout or check.stderr)[:8000], "exit_code": check.returncode,
            "scope": "whitespace_only", "tree_oid": tree}

    def target(self, ref):
        result = self.run(["for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)", ref])
        if result.returncode:
            raise PublishError("PUBLISH_TARGET_UNAVAILABLE")
        for line in result.stdout.splitlines():
            name, oid, symbolic = line.split("\0")
            if name == ref:
                return {"oid": oid, "symbolic": symbolic}
        return None

    def require_absent(self, ref):
        symbolic = self.run(["symbolic-ref", "--quiet", ref])
        if symbolic.returncode == 0:
            raise PublishError("PUBLISH_TARGET_EXISTS")
        if symbolic.returncode != 1:
            raise PublishError("PUBLISH_TARGET_UNAVAILABLE")
        if self.target(ref) is not None:
            raise PublishError("PUBLISH_TARGET_EXISTS")
        worktrees = self.run(["worktree", "list", "--porcelain"])
        if worktrees.returncode:
            raise PublishError("PUBLISH_TARGET_UNAVAILABLE")
        if "branch " + ref in worktrees.stdout.splitlines():
            raise PublishError("PUBLISH_TARGET_CHECKED_OUT")

    def import_candidate(self, candidate):
        if identity(candidate["repository"]) != self.workspace:
            result = self.run(["fetch", "--no-tags", "--no-write-fetch-head",
                "--no-recurse-submodules", "--no-auto-maintenance",
                candidate["repository"], candidate["oid"]])
            if result.returncode:
                raise PublishError("PUBLISH_IMPORT_FAILED")
        if self.oid(candidate["oid"]) != candidate["oid"] or self.oid(candidate["oid"], "tree") != candidate["tree_oid"]:
            raise PublishError("PUBLISH_SOURCE_CHANGED")

    def adopt(self, candidate, ref):
        try:
            self.import_candidate(candidate)
            self.require_absent(ref)
        except (PublishError, OSError, RuntimeError) as exc:
            raise RefWriteRefused(getattr(exc, "code", "PUBLISH_GIT_UNAVAILABLE")) from exc
        # --no-deref prevents an existing symbolic name from changing its target.
        # Git's old-zero check atomically refuses a competing nonempty ref.
        return self.run(["update-ref", "--no-deref", "-m", "gitgo publish adopt",
                         ref, candidate["oid"], "0" * len(candidate["oid"])])
