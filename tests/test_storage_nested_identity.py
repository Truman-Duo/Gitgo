"""Read-only identity lookup must agree with creation without launching Git."""
from pathlib import Path
from backend.core.storage import get_storage,resolve_existing_storage_paths


def test_nested_directory_resolves_existing_git_owner_without_process_or_writes(git_repo,monkeypatch):
    from backend.core.storage import paths
    store=get_storage(git_repo);nested=git_repo/"nested/deeper";nested.mkdir(parents=True)
    def forbidden(*a,**k):raise AssertionError("read-only identity lookup spawned Git")
    monkeypatch.setattr(paths.subprocess,"run",forbidden)
    found=resolve_existing_storage_paths(nested)
    assert found is not None and found.project_id==store.paths.project_id
    assert not (nested/".gitgo").exists()


def test_nearest_nested_git_boundary_does_not_adopt_outer_identity(git_repo):
    from backend.adapters.local_git_runner import LocalGitRunner
    outer=get_storage(git_repo);inner=git_repo/"inner";inner.mkdir()
    assert LocalGitRunner(inner).run(["init"]).returncode==0
    deep=inner/"deep";deep.mkdir()
    assert resolve_existing_storage_paths(deep) is None
    own=get_storage(inner)
    assert own.paths.project_id!=outer.paths.project_id
    assert resolve_existing_storage_paths(deep).project_id==own.paths.project_id


def test_explicit_local_identity_keeps_precedence_over_git_ancestor(git_repo):
    import uuid
    outer=get_storage(git_repo);nested=git_repo/"local";nested.mkdir();meta=nested/".gitgo";meta.mkdir()
    identity=str(uuid.uuid4());(meta/"project-id").write_text(identity,encoding="ascii")
    assert resolve_existing_storage_paths(nested).project_id==identity
    assert identity!=outer.paths.project_id


def test_new_project_identity_and_scope_ignore_foreign_git_environment(git_repo, tmp_path_factory, monkeypatch):
    import subprocess
    from backend.core.storage.paths import resolve_storage_paths
    from backend.core.storage.scope import assess_repository_scope
    foreign = get_storage(git_repo)
    target = tmp_path_factory / "unrelated-project"
    target.mkdir()
    subprocess.run(["git", "init", str(target)], check=True, capture_output=True, timeout=15)
    monkeypatch.setenv("GIT_DIR", str(git_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(git_repo))
    own = resolve_storage_paths(target, state_home=tmp_path_factory / "isolated-state")
    assert own.project_id != foreign.paths.project_id
    assert (target / ".git/gitgo/project-id").read_text().strip() == own.project_id
    scope = assess_repository_scope(target)
    assert scope.allowed and scope.git_root == target.resolve()
