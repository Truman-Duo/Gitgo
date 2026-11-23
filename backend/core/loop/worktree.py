"""Host-owned Git worktree isolation for Agent task nodes.

Agent worktrees are ephemeral execution sandboxes.  They do not replace
Gitgo's user-facing workspace/release/trial repositories and they are never
created inside the user's checkout.  Every durable result is held by an
internal ``refs/gitgo/...`` ref until it is promoted or explicitly discarded.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class WorktreeError(RuntimeError):
    """A worktree lifecycle transition could not be completed safely."""


class WorktreeScopeError(WorktreeError):
    code = "ASSIGNMENT_HANDOFF_DELIVERY_OUTSIDE_SCOPE"


class WorktreeArtifactUnavailable(WorktreeError):
    """The selected path cannot be read from the pinned result."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorktreeLease:
    worktree_id: str
    process_id: str
    task_id: str
    path: str
    root_workspace: str
    mode: str = "write"
    state: str = "creating"
    base_commit: str = ""
    snapshot_commit: str = ""
    accepted_commit: str = ""
    result_commit: str = ""
    own_commit: str = ""
    retained_result: bool = False
    resumed_from_process_id: str = ""
    continuation_commit: str = ""
    upstream_commits: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    runtime_offsets: dict[str, int] = field(default_factory=dict)
    input_dependencies: list[dict] = field(default_factory=list)
    macos_binding: dict = field(default_factory=dict)
    error: str = ""
    promoted: bool = False
    promoted_at: str = ""
    promoted_commit: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    @property
    def isolated(self) -> bool:
        return self.state not in {"shared", "disposed"} and bool(self.path)

    @property
    def has_promotable_result(self) -> bool:
        # Approval disposes a no-change checkout but keeps its immutable
        # result ref. That lifecycle cleanup must not invalidate a DAG proof.
        return bool(self.result_commit) and (
            self.state == "sealed" or (
                self.state == "disposed" and self.mode == "write"
                and (self.retained_result or not self.own_commit)
            )
        )

    @property
    def input_snapshot(self) -> str:
        return self.accepted_commit or self.snapshot_commit

    def to_dict(self) -> dict:
        value = asdict(self)
        value["isolated"] = self.isolated
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "WorktreeLease":
        names = set(cls.__dataclass_fields__)
        fields = {key: item for key, item in value.items() if key in names}
        fields["input_dependencies"] = list(fields.get("input_dependencies") or [])
        return cls(**fields)


class AgentWorktreeManager:
    """Create, seal, promote and dispose task-scoped linked worktrees."""

    def __init__(self, workspace: str | Path, storage) -> None:
        self.workspace = Path(workspace).resolve()
        self.storage = storage
        # Establish the submitted root without executing repository-configured
        # programs during daemon assembly. Protected Git validates the actual
        # metadata on every operation. An unsupported backend/config must fail
        # the requested isolated task, not make startup fall back to shared mode.
        marker = self.workspace / ".git"
        if not (marker.is_dir() or marker.is_file()):
            raise WorktreeError(
                f"workspace must be the Git repository root: {self.workspace}"
            )
        self.repo_root = self.workspace
        # Keep linked checkouts outside both the user repository and the long
        # per-project SQLite/CAS path.  Git for Windows still encounters tools
        # and hooks that assume MAX_PATH, so opaque directory names are a
        # deliberate compatibility boundary rather than presentation data.
        project_slug = hashlib.sha256(
            str(storage.paths.project_id).encode("utf-8")
        ).hexdigest()[:16]
        self.worktrees_root = (
            storage.paths.state_home.parent / "wt" / project_slug
        ).resolve()
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    # Product-managed metadata is never a business delta. Its selected
    # observations travel through the canonical metadata channels below;
    # new internal file kinds must not accidentally enter a sealed result.
    _RUNTIME_PATHS = (".gitgo",)
    _RUNTIME_INPUT_NAMES = ("test_manifest.json", "dependency_graph.v2.json",
                            "dependency_feedback.json", "dependency_observations.jsonl")

    @classmethod
    def _business_paths(cls) -> list[str]:
        return [".", ":(top,exclude,icase,literal).gitgo",
                ":(exclude,icase,glob)**/.gitgo/**",
                ":(exclude,icase,glob)**/.git/**"]

    @classmethod
    def _is_runtime_path(cls, relative: str) -> bool:
        from backend.core.tools.file_operations import protected_metadata_part
        return any(protected_metadata_part(part) for part in relative.replace(chr(92), "/").split("/"))

    def _apply_business_commit(self, target: Path, commit: str) -> None:
        # Project the business delta even for an older sealed commit that
        # contained runtime artifacts. Preserve immutable refs and normal
        # three-way conflict detection; do not mutate the user's index.
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(commit)):
            raise WorktreeError("Agent result commit is invalid")
        patch = self._git_bytes(
            target, "diff", "--binary", "--full-index", "--no-ext-diff",
            f"{commit}^", commit, "--", *self._business_paths(),
        )
        if patch:
            self._git_input(target, patch, "apply", "--3way", "--index", "-")

    @staticmethod
    def _safe_id(value: str) -> str:
        safe = "".join(char for char in value if char.isalnum() or char in "-_")
        if not safe or safe != value:
            raise WorktreeError("worktree identity contains unsafe characters")
        return safe

    @staticmethod
    def _ref_id(value: str) -> str:
        """Git refs reject task separators such as ':'; keep a stable opaque id."""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _run(
        cwd: Path,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
        timeout_seconds: float = 120,
    ) -> subprocess.CompletedProcess:
        from .git_execution import GitBoundaryError, protected_git
        try:
            import sys
            if sys.platform=='darwin':
                from .git_macos import run_object_query
                if not args or args[0]!='git':
                    raise GitBoundaryError('GIT_COMMAND_UNSUPPORTED','Only Git object queries are supported')
                result=run_object_query(cwd,args[1:],timeout=timeout_seconds,text=False,input_bytes=input_bytes)
            else:
                with protected_git(cwd, args) as (command, environment):
                    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    result = subprocess.run(command, cwd=cwd, capture_output=True,
                        timeout=timeout_seconds, check=False, close_fds=True,
                        creationflags=creationflags, env=environment,
                        **({"pass_fds": command.pass_fds} if os.name == 'posix' and hasattr(command, 'pass_fds') else {}),
                        **({"input": input_bytes} if input_bytes is not None
                           else {"stdin": subprocess.DEVNULL}))
        except GitBoundaryError as exc:
            from backend.core.errors import error_payload
            failure = WorktreeError(str(exc))
            failure.diagnostic = error_payload(exc.code, message=str(exc), details={
                "scope": "this_git_command", "command_started": getattr(exc,'command_started',False),
                "task_effects": "not_inferred", "unrestricted_fallback": False,
            })
            raise failure from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            command_label = " ".join(str(item) for item in args[:5])
            raise WorktreeError(
                f"Git worktree command failed ({command_label}): {exc}"
            ) from exc
        if check and result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
            raise WorktreeError(diagnostic or f"command exited {result.returncode}")
        return result

    @classmethod
    def _git_bytes(
        cls, cwd: Path, *args: str, check: bool = True,
        timeout_seconds: float = 120,
    ) -> bytes:
        return cls._run(
            cwd, ["git", *args], check=check, timeout_seconds=timeout_seconds,
        ).stdout

    @classmethod
    def _git_text(
        cls, cwd: Path, *args: str, check: bool = True,
        timeout_seconds: float = 120,
    ) -> str:
        return cls._git_bytes(
            cwd, *args, check=check, timeout_seconds=timeout_seconds,
        ).decode(
            "utf-8", errors="replace"
        ).strip()

    @classmethod
    def _git_input(cls, cwd: Path, data: bytes, *args: str) -> None:
        cls._run(cwd, ["git", *args], input_bytes=data)

    @staticmethod
    def _ref(kind: str, identity: str) -> str:
        return f"refs/gitgo/{kind}/{identity}"

    def _persist(self, lease: WorktreeLease) -> None:
        lease.updated_at = _utc_now()
        self.storage.save_worktree_state(lease.to_dict())

    def _target_path(self, process_id: str) -> Path:
        safe_id = self._safe_id(process_id)
        directory = hashlib.sha256(safe_id.encode("utf-8")).hexdigest()[:20]
        target = (self.worktrees_root / directory).resolve()
        try:
            target.relative_to(self.worktrees_root)
        except ValueError as exc:
            raise WorktreeError("worktree path escapes managed state root") from exc
        return target

    def _add_detached(self, target: Path, commit: str) -> None:
        if target.exists():
            raise WorktreeError(f"managed worktree path already exists: {target}")
        self._git_text(
            self.repo_root, "worktree", "add", "--detach", str(target), commit,
        )

    def _copy_untracked(self, target: Path, source_root: Path) -> None:
        raw = self._git_bytes(
            source_root, "ls-files", "--others", "--exclude-standard", "-z",
        )
        for encoded in raw.split(b"\0"):
            if not encoded:
                continue
            relative = encoded.decode("utf-8", errors="surrogateescape")
            if self._is_runtime_path(relative):
                continue
            source = (source_root / relative).resolve()
            destination = (target / relative).resolve()
            try:
                source.relative_to(source_root)
                destination.relative_to(target)
            except ValueError as exc:
                raise WorktreeError("untracked path escapes repository") from exc
            if source.is_symlink() or not source.is_file():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def _commit(self, path: Path, message: str, *, target_files: list[str] | None = None) -> str:
        # Enumerate Git-visible paths rather than passing excluded children
        # of an ignored .gitgo directory to `git add` (Git rejects those).
        visible = self._git_bytes(path, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        business = dict.fromkeys(
            name for name in visible.split(b"\0") if name
            and not self._is_runtime_path(name.decode("utf-8", errors="surrogateescape"))
        )
        if business:
            pathspec = b"\0".join(b":(literal)" + name for name in business) + b"\0"
            self._git_input(path, pathspec, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul")
        # A worker may have staged files itself, or an older result may have
        # carried runtime paths. Unstage those paths without deleting evidence.
        staged_names = self._git_bytes(path, "diff", "--cached", "--no-renames", "--name-only", "-z")
        names = [name.decode("utf8", errors="surrogateescape") for name in staged_names.split(b"\0") if name]
        metadata = [name for name in names if self._is_runtime_path(name)]
        if metadata:
            self._git_text(path, "reset", "--quiet", "HEAD", "--", *metadata)
        if target_files is not None:
            outside = sorted(set(names) - set(metadata) - set(target_files))
            if outside:
                raise WorktreeScopeError("Reassigned delivery changed files outside its scope: " + ", ".join(outside[:16]))
        staged = self._git_bytes(
            path, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
        )
        if not staged:
            return self._git_text(path, "rev-parse", "HEAD")
        try:
            self._scan_business_patch(staged)
        except WorktreeError:
            self._git_text(path, "reset", "--mixed", "HEAD")
            raise
        self._git_text(
            path,
            "-c", "user.name=Gitgo Agent",
            "-c", "user.email=agent@local.gitgo.invalid",
            "commit", "--no-verify", "-m", message,
        )
        return self._git_text(path, "rev-parse", "HEAD")

    @staticmethod
    def _scan_business_patch(patch: bytes) -> None:
        from backend.core.operations.security import scan_diff_for_secrets

        warnings = scan_diff_for_secrets(patch.decode("utf-8", errors="replace"))
        if warnings:
            rules = sorted({str(item.get("rule_id", "secret")) for item in warnings})
            # Return actionable, non-secret evidence.  Rule-only failures made
            # an A or user guess which of hundreds of snapshot files triggered
            # the gate, while exposing the matched text would defeat the gate.
            findings = [{
                "rule_id": str(item.get("rule_id") or "secret"),
                "file": str(item.get("file") or ""),
                "line": int(item.get("line", 0) or 0),
                "fingerprint": str(item.get("match_fingerprint") or ""),
            } for item in warnings[:20]]
            raise WorktreeError(
                "privacy scan blocked Agent snapshot: " + ", ".join(rules)
                + "; non_secret_findings="
                + json.dumps(findings, ensure_ascii=False, separators=(",", ":"))
            )

    def _copy_runtime_inputs(
        self, target: Path, lease: WorktreeLease, context_refs: dict | None = None,
    ) -> None:
        """Materialize task-pinned host metadata that Git intentionally ignores."""
        source_root = self.repo_root / ".gitgo"
        target_root = target / ".gitgo"
        for name in (
            "test_manifest.json", "dependency_graph.v2.json",
            "dependency_feedback.json", "dependency_observations.jsonl",
        ):
            source = source_root / name
            if not source.is_file():
                continue
            target_file = target_root / name
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_file)
            lease.runtime_offsets[name] = source.stat().st_size
        context_objects = source_root / "context_objects"
        for raw in (context_refs or {}).values():
            ref = str((raw or {}).get("pinned") or (raw or {}).get("latest") or "")
            if not ref.startswith("context:") or "@" not in ref:
                continue
            logical, selector = ref.removeprefix("context:").rsplit("@", 1)
            digest = selector.removeprefix("sha256:")
            if selector == "latest":
                ref_file = context_objects / "refs" / f"{logical}.json"
                try:
                    import json
                    digest = str(json.loads(ref_file.read_text(encoding="utf-8"))["digest"])
                except (OSError, KeyError, ValueError):
                    continue
            if len(digest) != 64:
                continue
            source_blob = context_objects / "blobs" / f"{digest}.json"
            source_ref = context_objects / "refs" / f"{logical}.json"
            for source, destination in (
                (source_blob, target_root / "context_objects" / "blobs" / source_blob.name),
                (source_ref, target_root / "context_objects" / "refs" / f"{logical}.json"),
            ):
                if source.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)

    def _merge_runtime_outputs(self, path: Path, lease: WorktreeLease) -> None:
        """Return test/dependency evidence through canonical host-owned channels."""
        from backend.core.loop.test_manifest import TestManifest

        child_manifest = TestManifest.load(path)
        if child_manifest.records:
            root_manifest = TestManifest.load(self.repo_root)
            root_manifest.register_many(list(child_manifest.records.values()))

        child_observations = path / ".gitgo" / "dependency_observations.jsonl"
        if child_observations.is_file():
            offset = max(0, int(lease.runtime_offsets.get(
                "dependency_observations.jsonl", 0
            )))
            with child_observations.open("rb") as source:
                source.seek(offset)
                additions = source.read()
            if additions:
                destination = self.repo_root / ".gitgo" / "dependency_observations.jsonl"
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("ab") as handle:
                    handle.write(additions)

        child_feedback = path / ".gitgo" / "dependency_feedback.json"
        if child_feedback.is_file():
            import json

            root_feedback = self.repo_root / ".gitgo" / "dependency_feedback.json"
            try:
                child_entries = json.loads(
                    child_feedback.read_text(encoding="utf-8")
                ).get("entries", [])
            except (OSError, json.JSONDecodeError):
                child_entries = []
            try:
                root_entries = json.loads(
                    root_feedback.read_text(encoding="utf-8")
                ).get("entries", [])
            except (OSError, json.JSONDecodeError):
                root_entries = []
            merged = {
                (str(item.get("dependent", "")), str(item.get("dependency", ""))): item
                for item in root_entries
            }
            merged.update({
                (str(item.get("dependent", "")), str(item.get("dependency", ""))): item
                for item in child_entries
            })
            if merged:
                root_feedback.parent.mkdir(parents=True, exist_ok=True)
                temporary = root_feedback.with_suffix(".json.tmp")
                temporary.write_text(json.dumps({
                    "version": 1,
                    "entries": list(merged.values()),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, root_feedback)

    def create(
        self,
        *,
        process_id: str,
        task_id: str,
        snapshot_commit: str = "",
        snapshot_source: Path | None = None,
        upstream_commits: Iterable[str] = (),
        accepted_commit: str = "",
        mode: str = "write",
        review_commit: str = "",
        context_refs: dict | None = None,
        resume_from: WorktreeLease | None = None,
        assignment_draft: dict | None = None,
        continuation_target_files: list[str] | None = None,
    ) -> WorktreeLease:
        """Create one isolated checkout and materialize declared predecessors."""
        if accepted_commit and (not snapshot_commit or snapshot_source is not None or upstream_commits
                or mode != "write" or review_commit):
            raise WorktreeError("An accepted workspace version requires its own fixed task snapshot.")
        import sys
        if sys.platform == 'darwin':
            if snapshot_source is not None:
                raise WorktreeError("Protected macOS nested snapshots are unavailable.")
            if assignment_draft is not None or continuation_target_files is not None:
                raise WorktreeScopeError("Protected macOS assignment handoff is unavailable.")
            from backend.core.macos_worktree import create
            return create(self,process_id=process_id,task_id=task_id,snapshot_commit=snapshot_commit,
                upstream_commits=upstream_commits,accepted_commit=accepted_commit,mode=mode,review_commit=review_commit,
                context_refs=context_refs,resume_from=resume_from)
        if mode not in {"write", "review"}:
            raise WorktreeError("worktree mode must be write or review")
        source_root = self.repo_root
        if snapshot_source is not None:
            source_root = snapshot_source.resolve(strict=True)
            if (mode != "write" or snapshot_commit or review_commit
                    or source_root.parent != self.worktrees_root):
                raise WorktreeError("Nested snapshots require a managed source checkout.")
        process_id = self._safe_id(process_id)
        target = self._target_path(process_id)
        lease = WorktreeLease(
            worktree_id=process_id,
            process_id=process_id,
            task_id=task_id,
            path=str(target),
            root_workspace=str(self.repo_root),
            mode=mode,
            base_commit="",
            accepted_commit=accepted_commit,
            resumed_from_process_id=resume_from.process_id if resume_from else "",
        )
        # Establish a durable lifecycle row before invoking any child process.
        # If even `git rev-parse` cannot start, recovery/diagnostics still know
        # which managed lease failed and why.
        self._persist(lease)
        try:
            base_head = self._git_text(
                source_root, "rev-parse", "HEAD", timeout_seconds=15,
            )
            lease.base_commit = base_head
            self._persist(lease)
            if review_commit:
                self._add_detached(target, review_commit)
                lease.snapshot_commit = review_commit
                lease.result_commit = review_commit
                lease.state = "review_ready"
            else:
                start = accepted_commit or snapshot_commit or base_head
                self._add_detached(target, start)
                if accepted_commit and self._git_text(target, "rev-parse", "HEAD") != accepted_commit:
                    raise WorktreeError("The follow-up checkout does not contain the pinned accepted version.")
                if not snapshot_commit:
                    patch = self._git_bytes(
                        source_root, "diff", "--binary", "--full-index", "HEAD", "--", *self._business_paths(),
                    )
                    if patch:
                        self._git_input(
                            target, patch, "apply", "--binary", "--whitespace=nowarn", "-",
                        )
                    self._copy_untracked(target, source_root)
                    snapshot_commit = self._commit(
                        target, f"gitgo task snapshot {task_id}",
                    )
                    self._git_text(
                        self.repo_root, "update-ref",
                        self._ref("tasks", self._ref_id(task_id)), snapshot_commit,
                    )
                lease.snapshot_commit = snapshot_commit
                commits = list(dict.fromkeys(str(item) for item in upstream_commits if item))
                for commit in commits:
                    self._apply_business_commit(target, commit)
                if commits:
                    self._commit(target, f"gitgo materialize upstream for {task_id}")
                lease.upstream_commits = commits
                lease.state = "leased"
                if resume_from is not None:
                    self._restore_partial_work(lease, resume_from, target_files=continuation_target_files)
                if assignment_draft is not None:
                    self.restore_assignment_draft(lease, assignment_draft)
            self._copy_runtime_inputs(target, lease, context_refs=context_refs)
            self._persist(lease)
            return lease
        except BaseException as exc:
            lease.state = "failed"
            lease.error = str(exc)
            self._persist(lease)
            try:
                self._remove_path(target)
            except WorktreeError as cleanup_exc:
                lease.error = f"{lease.error}; cleanup failed: {cleanup_exc}"
                self._persist(lease)
            raise

    def _restore_partial_work(self, lease: WorktreeLease, previous: WorktreeLease, *, target_files=None) -> None:
        """Copy a failed execution's business delta, never its execution authority.

        The previous checkout is frozen behind a private ref after the normal
        privacy scan. The new checkout keeps its original upstream HEAD; the
        restored edits remain uncommitted so sealing includes the entire delta.
        """
        allowed_states = {"leased", "scope_blocked"} if target_files is not None else {"leased"}
        if (previous.mode != "write" or previous.state not in allowed_states or previous.promoted
                or previous.root_workspace != lease.root_workspace
                or previous.snapshot_commit != lease.snapshot_commit
                or previous.accepted_commit != lease.accepted_commit
                or previous.upstream_commits != lease.upstream_commits):
            raise WorktreeError("CONTINUATION_BASE_MISMATCH: unpublished work requires the same snapshot and dependencies")
        target = Path(lease.path)
        base = self._git_text(target, "rev-parse", "HEAD")
        checkpoint, patch = self._retain_partial_work(previous, base, target_files=target_files)
        if patch:
            # Scoped checkpoints store the exact prior file bytes. Leave the
            # new index on its original tree so Git applies its ordinary
            # attributes and EOL rules when the successor seals the result.
            index = ["--index"] if target_files is None else []
            self._git_input(target, patch, "apply", "--binary", *index, "--whitespace=nowarn", "-")
        lease.resumed_from_process_id = previous.process_id
        lease.continuation_commit = checkpoint

    def _retain_partial_work(self, previous: WorktreeLease, base: str, *, target_files=None) -> tuple[str, bytes]:
        source = Path(previous.path).resolve()
        if source != self._target_path(self._safe_id(previous.process_id)).resolve():
            raise WorktreeError("CONTINUATION_WORKTREE_INVALID: source is not its managed checkout")
        if not self.verify(previous).get("valid"):
            raise WorktreeError("CONTINUATION_WORKTREE_INVALID: retained checkout is unavailable")
        source_common = self._git_text(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        root_common = self._git_text(self.repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if Path(source_common).resolve() != Path(root_common).resolve():
            raise WorktreeError("CONTINUATION_WORKTREE_INVALID: checkout belongs to another repository")
        if target_files is None:
            checkpoint = self._commit(source, f"gitgo retain failed execution {previous.process_id}")
        else:
            import uuid
            retained, _ = self.freeze_assignment_draft(source, base, target_files, str(uuid.uuid4()))
            checkpoint = retained["commit"]
        patch = self._git_bytes(source, "diff", "--binary", "--full-index", "--no-ext-diff",
                                base, checkpoint, "--", *self._business_paths())
        self._scan_business_patch(patch)
        self._git_text(self.repo_root, "update-ref",
                       self._ref("continuations", self._safe_id(previous.process_id)), checkpoint)
        previous.continuation_commit = checkpoint
        self._persist(previous)
        return checkpoint, patch

    def retain_returned_work(self, lease: WorktreeLease) -> dict:
        """Freeze a stopped independent checkout for owner review, without sealing it."""
        if (lease.mode != "write" or lease.state != "leased" or lease.promoted
                or lease.upstream_commits or not lease.snapshot_commit):
            raise WorktreeError("returned work requires an independent leased checkout")
        # Inspect the whole business delta. Excluded runtime paths are never
        # offered as reviewable results.
        checkpoint, _ = self._retain_partial_work(lease, lease.input_snapshot)
        names = self._git_bytes(self.repo_root, "diff", "--no-renames", "--name-only", "--diff-filter=AMT", "-z",
                               lease.input_snapshot, checkpoint, "--", *self._business_paths())
        return {"commit": checkpoint, "files": [name.decode("utf-8", errors="surrogateescape")
                                                 for name in names.split(b"\0") if name]}

    def freeze_assignment_draft(self, source, base_commit: str, targets: list[str], draft_id: str) -> tuple[dict, dict]:
        """Freeze admitted paths at a worker boundary, leaving its HEAD and index alone."""
        import stat
        from backend.core.publish import formal
        from backend.core.publish.git import CandidateGit, OID
        from backend.core.sandbox_read import ReadRoot, relative_parts
        from backend.core.tools.workspace_projection import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES

        if not OID.fullmatch(base_commit) or not targets:
            raise WorktreeError("A draft requires a fixed base commit and concrete file targets")
        git = CandidateGit(self.repo_root)
        source = Path(source).resolve(strict=True)
        base = {row[0]: row for row in formal.tree_files(git, base_commit, allow_empty=True)}
        files = dict(base)
        with ReadRoot(source) as reader:
            for relative in targets:
                if ("/".join(relative_parts(relative)) != relative or self._is_runtime_path(relative)
                        or "\\" in relative):
                    raise WorktreeError("A draft target must be a canonical business path")
                path = source / relative
                try:
                    mode = path.lstat().st_mode
                except FileNotFoundError:
                    files.pop(relative, None)
                    continue
                if not stat.S_ISREG(mode):
                    raise WorktreeError("A draft can only contain regular business files")
                with reader.open(relative) as binding:
                    raw = binding.read_bytes(max_bytes=MAX_FILE_BYTES)
                git_mode = (base[relative][1] if os.name == "nt" and relative in base
                            else "100755" if os.name != "nt" and mode & stat.S_IXUSR else "100644")
                if relative in base and base[relative][1] == git_mode and base[relative][3] == raw:
                    continue
                blob = git.binary(["hash-object", "-w", "--stdin"], raw).decode().strip()
                if not OID.fullmatch(blob):
                    raise WorktreeError("Draft file identity could not be recorded")
                files[relative] = (relative, git_mode, blob, raw)
        if len(files) > MAX_FILES or sum(len(row[3]) for row in files.values()) > MAX_TOTAL_BYTES:
            raise WorktreeError("Draft exceeds the existing workspace snapshot limit")
        aliases = {}
        for relative in files:
            parts = relative.split("/")
            for stop in range(1, len(parts) + 1):
                spelling = "/".join(parts[:stop])
                if aliases.setdefault(spelling.casefold(), spelling) != spelling:
                    raise WorktreeError("Draft contains ambiguous path spellings")
        changed = sorted(relative for relative in targets if files.get(relative) != base.get(relative))
        tree = formal.write_tree(git, files.values())
        commit = self._git_text(self.repo_root, "-c", "user.name=Gitgo Agent",
                                "-c", "user.email=agent@local.gitgo.invalid",
                                "commit-tree", tree, "-p", base_commit, "-m", f"Gitgo assignment draft {draft_id}")
        if not OID.fullmatch(commit):
            raise WorktreeError("Draft commit identity could not be recorded")
        patch = self._git_bytes(self.repo_root, "diff", "--binary", "--full-index", "--no-ext-diff",
                                base_commit, commit, "--", *self._business_paths())
        self._scan_business_patch(patch)
        self._git_text(self.repo_root, "update-ref", self._ref("assignment-drafts", self._safe_id(draft_id)),
                       commit, "0" * len(commit))
        return {"commit": commit, "base_commit": base_commit, "tree": tree,
                "files": [name for name in changed if name in files],
                "deleted_files": [name for name in changed if name not in files]}, files

    def restore_assignment_draft(self, lease: WorktreeLease, draft: dict) -> None:
        """Use the selected immutable input, even if the original checkout advanced."""
        if lease.input_snapshot != draft["base_commit"] or lease.upstream_commits:
            raise WorktreeError("Draft base or dependency inputs changed before reassignment")
        targets = draft.get("retained_target_files")
        paths = self._business_paths() if targets is None else [f":(top,literal){name}" for name in targets]
        split_renames = () if targets is None else ("--no-renames",)
        patch = self._git_bytes(self.repo_root, "diff", "--binary", "--full-index", "--no-ext-diff", *split_renames,
                                draft["base_commit"], draft["commit"], "--", *paths)
        if patch:
            self._git_input(Path(lease.path), patch, "apply", "--binary", "--index", "--whitespace=nowarn", "-")

    def _delivery_files(self, integration: Path, changed: list[str],
                        artifact_paths: Iterable[str] = ()) -> tuple[list[dict], list[str]]:
        """Bounded digests from the candidate tree, before the promotion effect.

        Unsupported files stay ordinary promoted files; they cannot silently
        become text evidence. This metadata is consumed by the existing receipt
        pipeline and delivery collector, not a second completion authority.
        """
        from backend.core.tools.workspace_tools import _confined_path
        from .delivery_artifacts import MAX_ARTIFACT_BYTES
        files, notices = [], []
        candidates = list(dict.fromkeys([*changed, *(str(p) for p in artifact_paths if str(p) != "*")]))
        for relative in candidates[:128]:
            try:
                path = _confined_path(integration, relative, must_exist=True)
                if not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                    raise ValueError("not a bounded text artifact")
                with path.open("rb") as stream:
                    raw = stream.read(MAX_ARTIFACT_BYTES + 1)
                if len(raw) > MAX_ARTIFACT_BYTES or b"\0" in raw:
                    raise ValueError("not a bounded text artifact")
                raw.decode("utf-8-sig")
                files.append({"path": relative, "after_sha256": hashlib.sha256(raw).hexdigest(),
                              "evidence_kind": "committed_write" if relative in changed else "snapshot_verified"})
            except (ValueError, OSError, UnicodeError) as exc:
                notices.append(f"{relative}: {exc}")
        if len(candidates) > 128:
            notices.append("Promotion artifact metadata limited to 128 files.")
        return files, notices

    def stage_index(self, lease: WorktreeLease, paths: list[str], *, cancellation=None) -> dict:
        """Stage Host-selected paths using the registered macOS worktree grant."""
        if not lease.macos_binding:
            raise WorktreeError('Protected macOS index staging requires a registered lease')
        from backend.core.macos_worktree_index import stage
        return stage(self,lease,paths,cancellation=cancellation)

    def publish_index(self, lease: WorktreeLease, *, cancellation=None) -> dict:
        """Publish only the explicit staged result; does not seal/promote a task."""
        if not lease.macos_binding:
            raise WorktreeError('Protected macOS result publication requires a registered lease')
        from backend.core.macos_worktree_result import publish
        return publish(self,lease,cancellation=cancellation)

    def merge_runtime_outputs(self, lease: WorktreeLease, *, cancellation=None) -> WorktreeLease:
        """Explicitly merge captured runtime evidence; never promote business files."""
        if not lease.macos_binding:
            raise WorktreeError('Protected runtime merge requires a registered macOS lease')
        from backend.core.macos_worktree_runtime import merge
        return merge(self,lease,cancellation=cancellation)

    def seal(self, lease: WorktreeLease, *, target_files: list[str] | None = None) -> WorktreeLease:
        """Freeze a successful worker result behind an internal immutable ref."""
        if lease.mode != "write" or lease.state not in {"leased", "privacy_blocked"}:
            raise WorktreeError(f"worktree cannot be sealed from state {lease.state}")
        if lease.macos_binding:
            if target_files is not None:
                raise WorktreeScopeError("Protected macOS scoped sealing is unavailable.")
            from backend.core.macos_worktree_seal import seal
            from .native_invocation_scope import current_native_cancellation
            cancellation=current_native_cancellation(self.repo_root)
            if not lease.macos_binding.get('result'):
                from backend.core.macos_worktree_finish import prepare
                prepare(self,lease,cancellation=cancellation)
            return seal(self, lease, cancellation=cancellation)
        path = Path(lease.path).resolve()
        parent = self._git_text(path, "rev-parse", "HEAD")
        try:
            if target_files is not None and parent != lease.input_snapshot:
                raise WorktreeScopeError("The reassigned worktree left its original snapshot.")
            message = f"gitgo agent result {lease.process_id}"
            result = self._commit(path, message, target_files=target_files) if target_files is not None else self._commit(path, message)
        except WorktreeScopeError as exc:
            lease.state = "scope_blocked"
            lease.error = str(exc)
            self._persist(lease)
            raise
        except WorktreeError as exc:
            lease.state = "privacy_blocked"
            lease.error = str(exc)
            self._persist(lease)
            raise
        lease.own_commit = result if result != parent else ""
        lease.result_commit = result
        changed_range = f"{parent}..{result}" if result != parent else ""
        lease.changed_files = (
            [name.decode("utf-8", errors="surrogateescape") for name in self._git_bytes(
                path, "diff", "--no-renames", "--name-only", "-z", changed_range, "--").split(b"\0") if name]
            if changed_range else []
        )
        self._merge_runtime_outputs(path, lease)
        self._git_text(
            self.repo_root, "update-ref",
            self._ref("agents", self._safe_id(lease.process_id)), result,
        )
        lease.state = "sealed"
        lease.error = ""
        self._persist(lease)
        return lease

    def read_artifact(
        self, commit: str, path: str, *, sha256: str = "", offset: int = 0,
        max_chars: int = 24000,
    ) -> dict:
        """Read one text artifact from an immutable Agent result commit."""
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(commit)):
            raise WorktreeError("sealed artifact commit is invalid")
        relative = str(path or "").replace("\\", "/").strip("/")
        parts = [item for item in relative.split("/") if item]
        if not parts or any(item in {".", ".."} for item in parts):
            raise WorktreeError("sealed artifact path must be workspace-relative")
        relative = "/".join(parts)
        object_ref = f"{commit}:{relative}"
        import sys
        if sys.platform == 'darwin':
            from backend.core.macos_worktree_result import read_blob
            # Preserve literal-path semantics: do not normalize an absolute or
            # backslash path into a different authorized artifact name.
            if str(path or '') != relative:
                raise WorktreeError('sealed artifact requires an exact relative path')
            raw = read_blob(self, commit, relative)
        else:
            try:
                size = int(self._git_text(
                    self.repo_root, "cat-file", "-s", object_ref,
                ))
            except (ValueError, WorktreeError) as exc:
                raise WorktreeArtifactUnavailable("sealed artifact does not exist or cannot be read") from exc
            if size > 10 * 1024 * 1024:
                raise WorktreeError("sealed artifact exceeds the 10 MiB review limit")
            raw = self._git_bytes(self.repo_root, "show", object_ref)
        if b"\0" in raw[:8192]:
            raise WorktreeError("sealed artifact is binary")
        content = raw.decode("utf-8-sig", errors="replace")
        digest = hashlib.sha256(raw).hexdigest()
        if sha256 and sha256 != digest:
            raise WorktreeError("sealed artifact digest mismatch")
        start = max(0, int(offset or 0))
        if start > len(content):
            raise WorktreeError("sealed artifact offset is beyond the file")
        span = max(1000, min(int(max_chars or 24000), 100000))
        end = min(len(content), start + span)
        return {"representation": "git_blob", "sha256_basis": "git_blob_bytes",
                "working_tree_bytes_verified": False,
                "materialization_note": "Git checkout attributes/settings may change working-file bytes; promotion file_results bind the actual checked candidate bytes.",
            "path": relative,
            "sha256": digest,
            "content": content[start:end],
            "offset": start,
            "next_offset": end if end < len(content) else None,
            "total_chars": len(content),
            "sealed_commit": commit,
        }

    def promote(
        self,
        *,
        snapshot_commit: str,
        ordered_commits: Iterable[str],
        promotion_id: str,
        artifact_paths: Iterable[str] = (),
        input_dependencies: Iterable[dict] = (),
        file_service_args: dict | None = None,
    ) -> dict:
        from .promotion import promote
        return promote(self, snapshot_commit=snapshot_commit, ordered_commits=ordered_commits,
                       promotion_id=promotion_id, artifact_paths=artifact_paths, input_dependencies=input_dependencies,
                       file_service_args=file_service_args)

    def verify(self, lease: WorktreeLease) -> dict:
        if lease.macos_binding:
            from backend.core.macos_worktree import verify
            try:return verify(self,lease)
            except (OSError,ValueError,WorktreeError) as exc:return {"valid":False,"reason":str(exc)}
        path = Path(lease.path).resolve()
        if not path.is_dir():
            return {"valid": False, "reason": "worktree path missing"}
        common = self._git_text(path, "rev-parse", "--git-common-dir", check=False)
        if not common:
            return {"valid": False, "reason": "path is not a linked Git worktree"}
        head = self._git_text(path, "rev-parse", "HEAD", check=False)
        expected = lease.result_commit if lease.state == "sealed" else ""
        if expected and head != expected:
            return {"valid": False, "reason": "worktree HEAD differs from sealed result"}
        return {"valid": True, "head": head}

    def dispose(self, lease: WorktreeLease, *, keep_ref: bool = True) -> WorktreeLease:
        if lease.macos_binding:
            if not keep_ref:
                raise WorktreeError('Protected macOS ref deletion is not yet supported')
            from backend.core.macos_worktree import retire
            return retire(self,lease)
        self._remove_path(Path(lease.path).resolve())
        if not keep_ref:
            ref = self._ref("agents", self._safe_id(lease.process_id))
            self._git_text(self.repo_root, "update-ref", "-d", ref, check=False)
        lease.retained_result = keep_ref and lease.has_promotable_result
        lease.state = "disposed"
        lease.error = ""
        self._persist(lease)
        return lease

    def recover_owned(self, lease: WorktreeLease) -> WorktreeLease:
        """Reconcile an existing macOS lease; never recreate its Git command."""
        if not lease.macos_binding:
            raise WorktreeError('No recorded macOS directory authority')
        from backend.core.macos_worktree import recover
        return recover(self,lease)

    def mark_promoted(self, lease: WorktreeLease, *, result_commit: str = "") -> WorktreeLease:
        if not lease.has_promotable_result:
            raise WorktreeError(f"worktree cannot be promoted from state {lease.state}")
        lease.promoted = True
        lease.promoted_at = _utc_now()
        lease.promoted_commit = result_commit
        self._persist(lease)
        return lease

    def _remove_path(self, target: Path) -> None:
        target = target.resolve()
        try:
            target.relative_to(self.worktrees_root)
        except ValueError as exc:
            raise WorktreeError("refusing to remove unmanaged worktree") from exc
        if target.exists():
            result = self._run(
                self.repo_root,
                ["git", "worktree", "remove", "--force", str(target)],
                check=False,
            )
            if result.returncode != 0 or target.exists():
                diagnostic = result.stderr.decode(
                    "utf-8", errors="replace"
                ).strip()
                raise WorktreeError(
                    diagnostic or f"managed worktree could not be removed: {target}"
                )
        self._git_text(self.repo_root, "worktree", "prune", check=False)


def patch_digest(data: bytes) -> str:
    """Stable helper used in receipts/tests without persisting patch bodies."""
    return hashlib.sha256(data).hexdigest()
