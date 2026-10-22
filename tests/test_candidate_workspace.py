from types import SimpleNamespace
import os
import pytest
from backend.core.loop.candidate_workspace import allocate_candidate_workspace, candidate_workspace_root
from backend.core.sandbox import SandboxDenied, child_environment_paths


def test_private_root_does_not_merge_projects_or_move_existing_views(tmp_path_factory):
    base = tmp_path_factory
    state = base / "state"
    ws = base / "workspace"
    ws.mkdir()
    first = SimpleNamespace(state_home=state, project_root=state / "projects" / ("a" * 32), workspace=ws, project_id="a" * 32)
    second = SimpleNamespace(state_home=state, project_root=state / "projects" / ("b" * 32), workspace=ws, project_id="b" * 32)
    root, stage = allocate_candidate_workspace(first, "workspace")
    assert stage.parent == root and stage.is_dir()
    assert candidate_workspace_root(second) != root
    assert root.is_relative_to(state) and not root.is_relative_to(ws)


@pytest.mark.skipif(os.name != "nt", reason="Windows startup limit")
def test_excessive_private_environment_refuses_before_allocation(tmp_path_factory):
    base = tmp_path_factory
    state = base / ("s" * 110) / ("t" * 80)
    paths = SimpleNamespace(state_home=state, project_root=state / "projects" / ("a" * 32), workspace=base / "workspace", project_id="a" * 32)
    with pytest.raises(SandboxDenied, match="startup limit"):
        allocate_candidate_workspace(paths, "document")
    assert not state.exists()
    with pytest.raises(SandboxDenied, match="startup limit"):
        child_environment_paths(base / ("x" * 180))


def test_rejects_private_root_inside_formal_workspace(tmp_path_factory):
    paths = SimpleNamespace(state_home=tmp_path_factory / "state", project_root=tmp_path_factory / "state" / "projects" / ("a" * 32), workspace=tmp_path_factory, project_id="a" * 32)
    with pytest.raises(SandboxDenied, match="overlaps"):
        allocate_candidate_workspace(paths, "workspace")


def test_actual_child_workspace_cannot_overlap_stage_despite_shared_project_storage(tmp_path_factory):
    base = tmp_path_factory
    paths = SimpleNamespace(project_root=base / "state" / "project", workspace=base / "main")
    # Child and main share storage, but use distinct concrete formal roots.
    with pytest.raises(SandboxDenied, match="overlaps"):
        allocate_candidate_workspace(paths, "workspace", workspace=paths.project_root / "c")
    assert not paths.project_root.exists()
