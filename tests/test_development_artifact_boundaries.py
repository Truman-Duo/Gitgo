"""Developer output boundaries and an isolated product storage example."""
from pathlib import Path
import ast
import importlib.util
import os
import pytest
from scripts.development_paths import development_root, test_state_home as product_state_home


def test_default_output_and_override_never_create_product_state(tmp_path_factory, monkeypatch):
    repo=tmp_path_factory/'repo';repo.mkdir()
    monkeypatch.delenv('GITGO_DEV_ARTIFACTS',raising=False)
    assert development_root(repo)==repo/'dev-artifacts'
    assert not (repo/'.gitgo').exists()
    alternate=tmp_path_factory/'external-development'
    monkeypatch.setenv('GITGO_DEV_ARTIFACTS',str(alternate))
    assert development_root(repo)==alternate
    assert not alternate.exists()


@pytest.mark.parametrize('suffix',['.gitgo','.gitgo/experiments','.git','.git/objects'])
def test_product_owned_directory_is_rejected(tmp_path_factory,monkeypatch,suffix):
    monkeypatch.setenv('GITGO_DEV_ARTIFACTS',str(tmp_path_factory/suffix))
    with pytest.raises(ValueError):development_root(tmp_path_factory/'repo')


def test_only_product_writer_creates_state_under_its_test_project(tmp_path_factory):
    from backend.core.storage import StorageRuntime
    project=tmp_path_factory/'test-project';project.mkdir()
    selected=product_state_home(project)
    assert not selected.exists()
    with StorageRuntime(project,state_home=selected) as store:
        assert store.paths.project_root.is_relative_to(project/'.gitgo')
        store.put_state_ref('boundary-test','observation','fixture-value')
        assert store.get_state_ref('boundary-test','observation')=='fixture-value'
    assert not (tmp_path_factory/'.gitgo').exists()


def test_developer_outputs_do_not_pollute_dependency_inputs(tmp_path_factory):
    from backend.core.dependency_graph import _source_files
    repo=tmp_path_factory
    for relative in ['app.py','dev-artifacts/case/project.py','dist-terminal/old/build.py']:
        path=repo/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('VALUE=1',encoding='utf-8')
    assert [p.relative_to(repo).as_posix() for p in _source_files(repo)]==['app.py']


def test_maintained_drivers_do_not_target_repository_dot_gitgo():
    root=Path(__file__).resolve().parents[1]
    files=list((root/'scripts').glob('*.py'))+list((root/'docs/technical-reports/scripts').glob('*.py'))
    for path in files:
        tree=ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Div) and isinstance(node.left,ast.Name) and node.left.id in {'ROOT','PROJECT_ROOT'} and isinstance(node.right,ast.Constant) and isinstance(node.right.value,str):
                assert not (node.right.value=='.gitgo' or node.right.value.startswith('.gitgo/')),str(path)


def test_stale_manifest_cannot_recreate_developer_files_in_product_storage(tmp_path_factory,monkeypatch):
    from scripts.development_paths import owned_development_path
    monkeypatch.delenv('GITGO_DEV_ARTIFACTS',raising=False)
    with pytest.raises(ValueError):
        owned_development_path(tmp_path_factory,tmp_path_factory/'.gitgo/old-experiment')
    expected=tmp_path_factory/'dev-artifacts/new-experiment'
    assert owned_development_path(tmp_path_factory,expected)==expected
    assert not expected.exists()


def test_relative_developer_override_is_repository_relative(tmp_path_factory,monkeypatch):
    monkeypatch.setenv('GITGO_DEV_ARTIFACTS','local-test-output')
    repo=tmp_path_factory/'repo'
    assert development_root(repo)==repo/'local-test-output'
