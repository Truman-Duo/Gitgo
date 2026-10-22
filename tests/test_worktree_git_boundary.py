"""Real Git extension probes; no model, network, credentials or live project."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from backend.core.loop.git_execution import protected_git, GitBoundaryError
from backend.core.loop.worktree import AgentWorktreeManager, WorktreeError
from backend.core.storage import StorageRuntime

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows protected-config backend")


def git(repo, *args, env=None):
    result = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True,
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


@pytest.fixture
def case(tmp_path_factory):
    tmp_path = tmp_path_factory
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@gitgo.invalid")
    # Base creation itself must not consume operator-global executable drivers.
    (repo / "app.txt").write_bytes(b"base\n")
    git(repo, "add", "app.txt")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "base")
    storage = StorageRuntime(repo, state_home=tmp_path / "state")
    manager = AgentWorktreeManager(repo, storage)
    try:
        yield repo, manager, tmp_path
    finally:
        storage.close()


def program(root, name):
    marker = root / (name + ".marker")
    script = root / (name + ".py")
    script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n", encoding="utf-8")
    # Git invokes configured programs via its shell; paths may contain spaces.
    return shlex.quote(sys.executable.replace("\\", "/")) + " " + shlex.quote(str(script).replace("\\", "/")), marker


@pytest.mark.parametrize("kind", ["clean", "smudge", "process"])
def test_configured_filters_never_run_or_silently_change_business_bytes(case, kind):
    repo, manager, root = case
    command, marker = program(root, kind)
    git(repo, "config", f"filter.special.{kind}", command)
    (repo / ".gitattributes").write_text("*.txt filter=special\n", encoding="utf-8")
    if kind == "smudge":
        # Materializing a checkout must reject unsupported transformation.
        with pytest.raises(WorktreeError, match="filter.*failed"):
            manager.create(process_id="filtered", task_id="filtered")
    else:
        with pytest.raises(WorktreeError, match="filter.*failed"):
            manager._git_text(repo, "hash-object", "--path=app.txt", "--", "app.txt")
    assert not marker.exists()
    assert (repo / "app.txt").read_bytes() == b"base\n"
    assert manager.storage.load_worktree_state("filtered") is None or manager.storage.load_worktree_state("filtered")["state"] == "failed"


def test_unused_filter_does_not_block_ordinary_dag(case):
    repo, manager, root = case
    command, marker = program(root, "unused-filter")
    git(repo, "config", "filter.unused.clean", command)
    lease = manager.create(process_id="plain", task_id="plain")
    (Path(lease.path) / "app.txt").write_text("changed\n", encoding="utf-8")
    manager.seal(lease)
    result = manager.promote(snapshot_commit=lease.snapshot_commit,
        ordered_commits=[lease.own_commit], promotion_id="plain")
    assert result["promoted"] and (repo / "app.txt").read_text() == "changed\n"
    assert not marker.exists()
    manager.dispose(lease)


def test_hooks_signing_fsmonitor_and_external_diff_do_not_run(case):
    repo, manager, root = case
    hook_command, hook_marker = program(root, "hook")
    hooks = root / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "prepare-commit-msg", "post-commit", "reference-transaction"):
        target = hooks / name
        target.write_text("#!/bin/sh\n" + hook_command + "\n", encoding="utf-8")
        target.chmod(0o755)
    fs_command, fs_marker = program(root, "fsmonitor")
    sign_command, sign_marker = program(root, "sign")
    diff_command, diff_marker = program(root, "diff")
    git(repo, "config", "core.hooksPath", str(hooks))
    git(repo, "config", "core.fsmonitor", fs_command)
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", sign_command)
    git(repo, "config", "diff.external", diff_command)
    git(repo, "config", "diff.special.textconv", diff_command)
    (repo / ".gitattributes").write_text("*.txt diff=special\n", encoding="utf-8")
    lease = manager.create(process_id="guarded", task_id="guarded")
    (Path(lease.path) / "app.txt").write_text("changed\n", encoding="utf-8")
    manager.seal(lease)
    manager.promote(snapshot_commit=lease.snapshot_commit,
        ordered_commits=[lease.own_commit], promotion_id="guarded")
    manager.dispose(lease)
    assert not any(p.exists() for p in (hook_marker, fs_marker, sign_marker, diff_marker))
    assert (repo / "app.txt").read_text() == "changed\n"


def test_configuration_cannot_be_rewritten_or_replaced_during_command(case):
    repo, manager, root = case
    config = repo / ".git" / "config"
    before = config.read_bytes()
    replacement = root / "replacement"
    replacement.write_bytes(before + b"\n[filter \"new\"]\nclean = echo injected\n")
    with protected_git(repo, ["git", "rev-parse", "HEAD"]):
        with pytest.raises(PermissionError):
            config.write_bytes(b"corrupted")
        with pytest.raises(PermissionError):
            os.replace(replacement, config)
    assert config.read_bytes() == before
    # The lock is scoped, so normal operator Git is not permanently disabled.
    git(repo, "config", "gitgo.afterProbe", "yes")


def test_repository_includes_are_protected_and_missing_include_is_rejected(case):
    repo, manager, root = case
    include = root / "included.conf"
    command, marker = program(root, "include")
    git(repo, "config", "--file", str(include), "core.fsmonitor", command)
    git(repo, "config", "include.path", str(include))
    with protected_git(repo, ["git", "rev-parse", "HEAD"]):
        with pytest.raises(PermissionError):
            include.write_text("changed")
    lease = manager.create(process_id="include", task_id="include")
    manager.dispose(lease)
    assert not marker.exists()
    git(repo, "config", "--add", "include.path", str(root / "missing.conf"))
    with pytest.raises(WorktreeError, match="GIT_CONFIG_UNSUPPORTED.*missing.*include"):
        manager.create(process_id="missing", task_id="missing")
    assert manager.storage.load_worktree_state("missing")["state"] == "failed"


def test_inherited_git_controls_cannot_redirect_index_or_run_programs(case, monkeypatch):
    repo, manager, root = case
    alternate = root / "alternate-index"
    alternate.write_bytes(b"must remain untouched")
    command, marker = program(root, "inherited")
    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    monkeypatch.setenv("GIT_TRACE", str(root / "trace-secret"))
    lease = manager.create(process_id="env", task_id="env")
    manager.dispose(lease)
    assert alternate.read_bytes() == b"must remain untouched"
    assert not marker.exists() and not (root / "trace-secret").exists()


def test_eol_attributes_binary_delete_and_reconciliation_remain_exact(case):
    repo, manager, root = case
    git(repo, "config", "core.autocrlf", "true")
    (repo / ".gitattributes").write_bytes(b"*.txt text eol=crlf\n*.bin binary\n")
    (repo / "obsolete.bin").write_bytes(b"\x00old")
    (repo / "oldname.txt").write_bytes(b"rename\r\n")
    git(repo, "add", "obsolete.bin", "oldname.txt")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "delete and rename baseline")
    lease = manager.create(process_id="bytes", task_id="bytes")
    checkout = Path(lease.path)
    (checkout / "app.txt").write_bytes(b"changed\r\n")
    (checkout / "payload.bin").write_bytes(b"\x00\xffnew\r\n")
    (checkout / "obsolete.bin").unlink()
    (checkout / "oldname.txt").rename(checkout / "新名字.txt")
    manager.seal(lease)
    result = manager.promote(snapshot_commit=lease.snapshot_commit,
        ordered_commits=[lease.own_commit], promotion_id="bytes")
    again = manager.promote(snapshot_commit=lease.snapshot_commit,
        ordered_commits=[lease.own_commit], promotion_id="bytes")
    assert result["promoted"] and not again["applied_commits"]
    assert (repo / "app.txt").read_bytes() == b"changed\r\n"
    assert (repo / "payload.bin").read_bytes() == b"\x00\xffnew\r\n"
    assert not (repo / "obsolete.bin").exists() and not (repo / "oldname.txt").exists()
    assert (repo / "新名字.txt").read_bytes() == b"rename\r\n"
    manager.dispose(lease)


@pytest.mark.parametrize("args", [["git", "fetch"], ["git", "-c", "core.fsmonitor=evil", "status"], ["python", "script.py"]])
def test_non_plumbing_invocations_cannot_use_trusted_runner(case, args):
    repo, manager, root = case
    with pytest.raises(WorktreeError, match="GIT_COMMAND_UNSUPPORTED"):
        manager._run(repo, args)


def test_unprotected_backend_never_falls_back_to_shared_execution(case, monkeypatch):
    import backend.core.loop.git_execution as boundary
    repo, manager, root = case
    def unavailable(_owners):
        raise GitBoundaryError("GIT_BOUNDARY_UNAVAILABLE", "injected missing backend")
    monkeypatch.setattr(boundary, "_WindowsPins", unavailable)
    with pytest.raises(WorktreeError, match="GIT_BOUNDARY_UNAVAILABLE") as caught:
        manager.create(process_id="no-backend", task_id="no-backend")
    info = caught.value.diagnostic["error_info"]
    assert info["catalog_id"] == "GITGO-E3812" and not info["retryable"]
    assert info["details"]["command_started"] is False
    from backend.core.loop.recovery_guidance import lookup_guidance
    assert lookup_guidance(info["catalog_id"])["found"]
    assert manager.storage.load_worktree_state("no-backend")["state"] == "failed"
    assert (repo / "app.txt").read_bytes() == b"base\n"


def test_original_clean_filter_probe_is_a_positive_control(case):
    repo, manager, root = case
    command, marker = program(root, "control")
    git(repo, "config", "filter.control.clean", command)
    (repo / ".gitattributes").write_text("*.txt filter=control\n", encoding="utf-8")
    # Only this isolated synthetic control intentionally runs unguarded Git.
    git(repo, "hash-object", "--path=app.txt", "--", "app.txt")
    assert marker.read_text() == "executed"
    marker.unlink()
    with pytest.raises(WorktreeError, match="filter.*failed"):
        manager._git_text(repo, "hash-object", "--path=app.txt", "--", "app.txt")
    assert not marker.exists()


def test_missing_objects_never_trigger_a_promisor_transport(case):
    repo, manager, root = case
    command, marker = program(root, "promisor")
    git(repo, "config", "extensions.partialClone", "test-promisor")
    git(repo, "config", "remote.test-promisor.promisor", "true")
    git(repo, "config", "remote.test-promisor.url", "ext::" + command)
    git(repo, "config", "protocol.ext.allow", "always")
    result = manager._run(repo, ["git", "cat-file", "-e", "1" * 40], check=False, timeout_seconds=5)
    assert result.returncode != 0 and not marker.exists()


def test_multiply_linked_config_cannot_enter_trusted_git(case):
    repo, manager, root = case
    os.link(repo / ".git" / "config", root / "linked-config")
    with pytest.raises(WorktreeError, match="linked"):
        manager.create(process_id="linked", task_id="linked")
    assert manager.storage.load_worktree_state("linked")["state"] == "failed"


def test_round_snapshot_and_recovery_use_the_same_protected_git(case, monkeypatch):
    from types import SimpleNamespace
    from backend.core.daemon import policy_helpers
    from backend.core.config import ProjectConfig
    from backend.core.history import HistoryManager
    from backend.core.loop.recovery import inspect_worktree_recovery
    repo, manager, root = case
    command, marker = program(root, "round-hook")
    hooks = root / "round-hooks"
    hooks.mkdir()
    for name in ("prepare-commit-msg", "post-commit", "reference-transaction"):
        target = hooks / name
        target.write_text("#!/bin/sh\n" + command + "\n", encoding="utf-8")
        target.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    git(repo, "config", "core.fsmonitor", command)
    events, history = [], []
    monkeypatch.setattr(policy_helpers, "_emit", events.append)
    monkeypatch.setattr(HistoryManager, "add_operation", lambda *a, **kw: history.append(a))
    (repo / "app.txt").write_text("round change\n")
    session = SimpleNamespace(workspace_path=str(repo), _correlation_id="synthetic",
        entries=[SimpleNamespace(rel_path="app.txt", status="modified")])
    assert policy_helpers._snapshot_workspace(session, ProjectConfig(name="synthetic")) == ["app.txt"]
    assert history and events[-1]["event"] == "workspace_snapshot"
    assert policy_helpers._snapshot_workspace(session, ProjectConfig(name="synthetic")) == []
    assert not marker.exists()
    lease = manager.create(process_id="recovery", task_id="recovery")
    manager.seal(lease)
    assert inspect_worktree_recovery(lease.to_dict()) == []
    assert not marker.exists()
    manager.dispose(lease)


def test_round_snapshot_filter_failure_is_not_reported_as_no_changes(case, monkeypatch):
    from types import SimpleNamespace
    from backend.core.daemon import policy_helpers
    from backend.core.config import ProjectConfig
    repo, manager, root = case
    command, marker = program(root, "round-filter")
    git(repo, "config", "filter.round.clean", command)
    (repo / ".gitattributes").write_text("*.txt filter=round\n")
    (repo / "app.txt").write_text("round change\n")
    events = []
    monkeypatch.setattr(policy_helpers, "_emit", events.append)
    session = SimpleNamespace(workspace_path=str(repo), _correlation_id="synthetic",
        entries=[SimpleNamespace(rel_path="app.txt", status="modified")])
    assert policy_helpers._snapshot_workspace(session, ProjectConfig(name="synthetic")) is None
    assert events[-1]["event"] == "snapshot_error" and not marker.exists()


def test_conditional_driver_inactive_in_parent_cannot_run_in_new_worktree(case):
    repo, manager, root = case
    include = root / "child-only.conf"
    command, marker = program(root, "conditional-smudge")
    git(repo, "config", "--file", str(include), "filter.child.smudge", command)
    git(repo, "config", "includeIf.gitdir:**/worktrees/**.path", str(include))
    (repo / ".gitattributes").write_text("*.txt filter=child\n")
    git(repo, "add", ".gitattributes")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "conditional checkout attributes")
    control = root / "control-checkout"
    git(repo, "worktree", "add", "--detach", str(control), "HEAD")
    assert marker.exists(), "unguarded nested checkout must activate this includeIf driver"
    git(repo, "worktree", "remove", "--force", str(control))
    marker.unlink()
    # The root has no active driver. It becomes active only in the nested
    # worktree checkout subprocess; all include branches must be constrained.
    with pytest.raises(WorktreeError, match="filter.*failed"):
        manager.create(process_id="conditional", task_id="conditional")
    assert not marker.exists()


def test_sealed_business_delta_excludes_product_metadata_without_gitignore(case):
    from backend.core.storage.metadata_files import write_metadata_bytes
    from backend.core.contract import ContractManager, ProjectContract
    repo, manager, root = case
    # Metadata is created through product services, rather than developer files
    # pretending to be product-owned facts. No .gitignore is required.
    write_metadata_bytes(repo, "future-internal-record.json", b'{"fixture": true}')
    lease = manager.create(process_id="metadata-seal", task_id="metadata-seal")
    child = Path(lease.path)
    ContractManager.save(child, ProjectContract(project="synthetic"))
    write_metadata_bytes(child, "future-internal-record.json", b'{"fixture": false}')
    nested = child / "nested-project"; nested.mkdir()
    write_metadata_bytes(nested, "internal.json", b'{"nested": true}')
    (child / "app.txt").write_text("actual business delta\n", encoding="utf8")
    # Even an already staged internal file is removed from the business index.
    manager._git_text(child, "add", "-A")
    manager.seal(lease)
    assert lease.changed_files == ["app.txt"]
    patch = manager._git_bytes(child, "diff", lease.result_commit + "^", lease.result_commit)
    assert b".gitgo" not in patch and b"actual business delta" in patch
    assert (child / ".gitgo/contract.yaml").exists()
    manager.dispose(lease)


def test_ordinary_local_git_content_reads_share_protected_configuration(case):
    from backend.adapters.local_git_runner import LocalGitRunner
    repo, manager, root = case
    diff_command, diff_marker = program(root, "ordinary-diff")
    fs_command, fs_marker = program(root, "ordinary-fsmonitor")
    gpg_command, gpg_marker = program(root, "ordinary-signature")
    git(repo, "config", "diff.external", diff_command)
    git(repo, "config", "diff.custom.textconv", diff_command)
    git(repo, "config", "core.fsmonitor", fs_command)
    git(repo, "config", "gpg.program", gpg_command)
    git(repo, "config", "log.showSignature", "true")
    (repo / ".gitattributes").write_text("*.txt diff=custom\n", encoding="utf8")
    (repo / "app.txt").write_text("actual changed bytes\n", encoding="utf8")
    runner = LocalGitRunner(repo)
    assert "actual changed bytes" in runner.diff()
    assert runner.run(["status", "--porcelain"]).returncode == 0
    assert runner.log() and runner.rev_parse()
    assert not any(path.exists() for path in (diff_marker, fs_marker, gpg_marker))


def test_unavailable_protected_read_does_not_retry_raw_git(case, monkeypatch):
    from backend.adapters.local_git_runner import LocalGitRunner
    import backend.core.loop.git_execution as module
    from contextlib import contextmanager
    repo, manager, root = case
    @contextmanager
    def denied(*args, **kwargs):
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "synthetic boundary rejection")
        yield
    monkeypatch.setattr(module, "protected_git", denied)
    with pytest.raises(GitBoundaryError):
        LocalGitRunner(repo).diff()


def test_ordinary_local_git_writes_never_execute_repository_programs(case):
    from backend.adapters.local_git_runner import LocalGitRunner
    repo, manager, root = case
    command, marker = program(root, "ordinary-write-hook")
    hooks = root / "ordinary-write-hooks"; hooks.mkdir()
    for name in ("pre-commit", "prepare-commit-msg", "post-commit", "reference-transaction"):
        hook = hooks / name
        hook.write_text("#!/bin/sh\n" + command + "\n", encoding="utf8")
        hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    git(repo, "config", "core.fsmonitor", command)
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", command)
    runner = LocalGitRunner(repo)
    (repo / "app.txt").write_bytes(b"actual local operation\n")
    assert runner.add_all()
    ok, error = runner.commit("local service operation")
    assert ok, error
    assert not marker.exists()
    assert "actual local operation" in runner.run(["show", "HEAD:app.txt"]).stdout
    # Active custom filtering must refuse, never stage unfiltered content.
    git(repo, "config", "filter.local.clean", command)
    (repo / ".gitattributes").write_text("*.txt filter=local\n", encoding="utf8")
    (repo / "app.txt").write_bytes(b"unsupported filter change\n")
    assert not runner.add_all()
    assert not marker.exists()


def test_diff_tree_summary_and_patch_never_execute_repository_diff_programs(case):
    from backend.adapters.local_git_runner import LocalGitRunner
    from backend.core.operations.diff import get_diff_summary
    repo, manager, root = case
    (repo / "app.txt").write_bytes(b"new tree bytes\n")
    git(repo, "add", "app.txt")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "tree delta")
    command, marker = program(root, "tree-diff")
    git(repo, "config", "diff.external", command)
    git(repo, "config", "diff.custom.textconv", command)
    (repo / ".gitattributes").write_text("*.txt diff=custom\n", encoding="utf8")
    runner = LocalGitRunner(repo)
    summary = get_diff_summary(runner.rev_parse(), runner)
    assert any(item["path"] == "app.txt" and item["added"] > 0 for item in summary)
    patch = runner.run(["diff-tree", "-p", "HEAD"])
    assert patch.returncode == 0 and "new tree bytes" in patch.stdout
    assert not marker.exists()
    for option in ("--ext-diff", "--textconv"):
        with pytest.raises(GitBoundaryError, match="GIT_COMMAND_UNSUPPORTED"):
            runner.run(["diff-tree", "-p", option, "HEAD"])
    assert not marker.exists()
