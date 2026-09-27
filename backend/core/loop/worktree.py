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
    result_commit: str = ""
    own_commit: str = ""
    upstream_commits: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    runtime_offsets: dict[str, int] = field(default_factory=dict)
    error: str = ""
    promoted: bool = False
    promoted_at: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    @property
    def isolated(self) -> bool:
        return self.state not in {"shared", "disposed"} and bool(self.path)

    def to_dict(self) -> dict:
        value = asdict(self)
        value["isolated"] = self.isolated
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "WorktreeLease":
        names = set(cls.__dataclass_fields__)
        return cls(**{key: item for key, item in value.items() if key in names})


class AgentWorktreeManager:
    """Create, seal, promote and dispose task-scoped linked worktrees."""

    def __init__(self, workspace: str | Path, storage) -> None:
        self.workspace = Path(workspace).resolve()
        self.storage = storage
        root = self._git_text(self.workspace, "rev-parse", "--show-toplevel")
        self.repo_root = Path(root).resolve()
        if self.repo_root != self.workspace:
            # Tool paths and repository identity are rooted at the submitted
            # workspace.  Silently widening to an ancestor would broaden the
            # Agent's authority and file scan scope.
            raise WorktreeError(
                f"workspace must be the Git repository root: {self.workspace}"
            )
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
        command = list(args)
        if command and command[0] == "git":
            # A daemon child must never inherit the JSON control pipe or wait
            # for Git Credential Manager/editor interaction.  Apart from
            # potentially consuming protocol input, inherited handles can
            # keep a nested Windows process alive after its owning task was
            # cancelled.  Resolve Git before launch so executable discovery
            # is not delegated to a grandchild process.
            command[0] = shutil.which("git") or "git"
            command[1:1] = ["-c", "core.longpaths=true"]
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_EDITOR": "true",
            "GIT_PAGER": "cat",
        }
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        input_options = (
            {"input": input_bytes}
            if input_bytes is not None
            else {"stdin": subprocess.DEVNULL}
        )
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                close_fds=True,
                creationflags=creationflags,
                env=environment,
                **input_options,
            )
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

    def _copy_untracked(self, target: Path) -> None:
        raw = self._git_bytes(
            self.repo_root, "ls-files", "--others", "--exclude-standard", "-z",
        )
        for encoded in raw.split(b"\0"):
            if not encoded:
                continue
            relative = encoded.decode("utf-8", errors="surrogateescape")
            source = (self.repo_root / relative).resolve()
            destination = (target / relative).resolve()
            try:
                source.relative_to(self.repo_root)
                destination.relative_to(target)
            except ValueError as exc:
                raise WorktreeError("untracked path escapes repository") from exc
            if source.is_symlink() or not source.is_file():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def _commit(self, path: Path, message: str) -> str:
        self._git_text(path, "add", "-A")
        staged = self._git_bytes(
            path, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
        )
        if not staged:
            return self._git_text(path, "rev-parse", "HEAD")
        from backend.core.operations.security import scan_diff_for_secrets

        warnings = scan_diff_for_secrets(staged.decode("utf-8", errors="replace"))
        if warnings:
            self._git_text(path, "reset", "--mixed", "HEAD")
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
        self._git_text(
            path,
            "-c", "user.name=Gitgo Agent",
            "-c", "user.email=agent@local.gitgo.invalid",
            "commit", "--no-verify", "-m", message,
        )
        return self._git_text(path, "rev-parse", "HEAD")

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
        upstream_commits: Iterable[str] = (),
        mode: str = "write",
        review_commit: str = "",
        context_refs: dict | None = None,
    ) -> WorktreeLease:
        """Create one isolated checkout and materialize declared predecessors."""
        if mode not in {"write", "review"}:
            raise WorktreeError("worktree mode must be write or review")
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
        )
        # Establish a durable lifecycle row before invoking any child process.
        # If even `git rev-parse` cannot start, recovery/diagnostics still know
        # which managed lease failed and why.
        self._persist(lease)
        try:
            base_head = self._git_text(
                self.repo_root, "rev-parse", "HEAD", timeout_seconds=15,
            )
            lease.base_commit = base_head
            self._persist(lease)
            if review_commit:
                self._add_detached(target, review_commit)
                lease.snapshot_commit = review_commit
                lease.result_commit = review_commit
                lease.state = "review_ready"
            else:
                start = snapshot_commit or base_head
                self._add_detached(target, start)
                if not snapshot_commit:
                    patch = self._git_bytes(
                        self.repo_root, "diff", "--binary", "--full-index", "HEAD", "--",
                    )
                    if patch:
                        self._git_input(
                            target, patch, "apply", "--binary", "--whitespace=nowarn", "-",
                        )
                    self._copy_untracked(target)
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
                    self._git_text(target, "cherry-pick", "--no-commit", commit)
                if commits:
                    self._commit(target, f"gitgo materialize upstream for {task_id}")
                lease.upstream_commits = commits
                lease.state = "leased"
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

    def seal(self, lease: WorktreeLease) -> WorktreeLease:
        """Freeze a successful worker result behind an internal immutable ref."""
        if lease.mode != "write" or lease.state not in {"leased", "privacy_blocked"}:
            raise WorktreeError(f"worktree cannot be sealed from state {lease.state}")
        path = Path(lease.path).resolve()
        parent = self._git_text(path, "rev-parse", "HEAD")
        try:
            result = self._commit(path, f"gitgo agent result {lease.process_id}")
        except WorktreeError as exc:
            lease.state = "privacy_blocked"
            lease.error = str(exc)
            self._persist(lease)
            raise
        lease.own_commit = result if result != parent else ""
        lease.result_commit = result
        changed_range = f"{parent}..{result}" if result != parent else ""
        lease.changed_files = (
            self._git_text(path, "diff", "--name-only", changed_range, "--").splitlines()
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
        try:
            size = int(self._git_text(
                self.repo_root, "cat-file", "-s", object_ref,
            ))
        except (ValueError, WorktreeError) as exc:
            raise WorktreeError("sealed artifact does not exist") from exc
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
        return {
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
    ) -> dict:
        """Apply approved DAG deltas to the user workspace without committing."""
        commits = list(dict.fromkeys(str(item) for item in ordered_commits if item))
        if not commits:
            return {"promoted": True, "changed_files": [], "commits": []}
        # Task ids are user/provider facing and may legitimately contain
        # separators that are invalid in a directory component.  Worktree
        # identities are Host-owned opaque values, so hash rather than reject
        # an otherwise valid task at the final promotion boundary.
        integration_id = f"promote-{self._ref_id(promotion_id)}"
        integration = self._target_path(integration_id)
        self._add_detached(integration, snapshot_commit)
        promotion_result: dict | None = None
        try:
            for commit in commits:
                self._git_text(integration, "cherry-pick", "--no-commit", commit)
                self._commit(integration, f"gitgo integrate {promotion_id}")
            tip = self._git_text(integration, "rev-parse", "HEAD")
            changed = self._git_text(
                integration, "diff", "--name-only", f"{snapshot_commit}..{tip}", "--",
            ).splitlines()
            if changed:
                overlap = self._git_text(
                    self.repo_root, "diff", "--name-only", snapshot_commit, "--", *changed,
                ).splitlines()
                if overlap:
                    raise WorktreeError(
                        "workspace changed after DAG snapshot on: " + ", ".join(overlap)
                    )
            patch = self._git_bytes(
                integration, "diff", "--binary", "--full-index",
                f"{snapshot_commit}..{tip}", "--",
            )
            if patch:
                self._git_input(
                    self.repo_root, patch,
                    "apply", "--binary", "--whitespace=nowarn", "-",
                )
            promotion_result = {
                "promoted": True,
                "changed_files": changed,
                "commits": commits,
                "snapshot_commit": snapshot_commit,
                "result_commit": tip,
            }
        finally:
            try:
                self._remove_path(integration)
            except WorktreeError as cleanup_exc:
                # Applying the patch is the promotion commit point.  A leaked
                # temporary checkout is a visible cleanup fault, not grounds
                # to falsely report that the already-applied patch failed.
                if promotion_result is not None:
                    promotion_result["cleanup_error"] = str(cleanup_exc)
        return promotion_result or {
            "promoted": False,
            "changed_files": [],
            "commits": commits,
        }

    def verify(self, lease: WorktreeLease) -> dict:
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
        self._remove_path(Path(lease.path).resolve())
        if not keep_ref:
            ref = self._ref("agents", self._safe_id(lease.process_id))
            self._git_text(self.repo_root, "update-ref", "-d", ref, check=False)
        lease.state = "disposed"
        lease.error = ""
        self._persist(lease)
        return lease

    def mark_promoted(self, lease: WorktreeLease) -> WorktreeLease:
        if lease.state != "sealed":
            raise WorktreeError(f"worktree cannot be promoted from state {lease.state}")
        lease.promoted = True
        lease.promoted_at = _utc_now()
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
