from pathlib import Path
import pytest
import sys

if sys.platform != 'darwin':
    pytest.skip('Darwin protected Git boundary', allow_module_level=True)

from backend.core.loop.worktree import WorktreeError
from backend.core import macos_worktree_index as index
from tests.test_macos_worktree_result import ready,staged,case,create,repository,commit_fixture
from scripts.check_macos_worktree_client import ephemeral_privacy_fixture


def test_public_completion_stages_add_delete_mode_and_merges_runtime(ready):
    from tests.test_macos_worktree_runtime import evidence
    manager,lease,calls,checks,git=ready;p=Path(lease.path)
    (p/'new.txt').write_text('new');(p/'nested/run.sh').unlink();(p/'a.bin').chmod(0o755)
    evidence(p/'.gitgo');head=git('rev-parse','HEAD')
    manager.seal(lease)
    assert lease.state=='sealed' and manager.verify(lease)['valid']
    assert lease.changed_files==['a.bin','nested/run.sh','new.txt'] and git('rev-parse','HEAD')==head
    assert (manager.repo_root/'.gitgo/test_manifest.json').exists()


@pytest.mark.parametrize('failure',['ignore','exclude','secret','metadata'])
def test_preflight_refuses_without_git_effects(ready,failure):
    manager,lease,calls,checks,git=ready;p=Path(lease.path);before=len(calls)
    if failure=='ignore':(p/'.gitignore').write_text('secret\n')
    if failure=='exclude':
        info=manager.repo_root/'.git/info';info.mkdir(exist_ok=True);(info/'exclude').write_text('secret\n')
    if failure=='secret':(p/'secret').write_bytes(ephemeral_privacy_fixture())
    if failure=='metadata':
        (p/'.gitgo').mkdir();(p/'.gitgo/unknown').write_text('preserve')
    with pytest.raises(WorktreeError):manager.seal(lease)
    assert len(calls)==before and lease.state=='leased' and not lease.macos_binding.get('result')


def test_drift_after_staging_cannot_publish(ready,monkeypatch):
    manager,lease,calls,checks,git=ready;p=Path(lease.path);(p/'new').write_text('before');stage=index.stage
    def drift(*a,**kw):
        value=stage(*a,**kw);(p/'new').write_text('after');return value
    monkeypatch.setattr(index,'stage',drift)
    with pytest.raises(WorktreeError,match='input changed'):manager.seal(lease)
    assert lease.state=='leased' and not lease.macos_binding.get('result')


def test_many_changes_use_bounded_batches(ready,monkeypatch):
    manager,lease,*_=ready;p=Path(lease.path);stage=index.stage;batches=[]
    for i in range(19):(p/f'new{i:02}').write_text(str(i))
    def record(*a,**kw):
        batches.append(list(a[2]));return stage(*a,**kw)
    monkeypatch.setattr(index,'stage',record);manager.seal(lease)
    assert [len(b) for b in batches]==[16,3] and lease.state=='sealed'
    assert len(lease.changed_files)==20 and manager.verify(lease)['valid']


def test_empty_tree_restage_after_deleting_every_business_file(ready):
    manager,lease,*_=ready;p=Path(lease.path)
    for name in ('a.bin','.gitattributes','nested/run.sh'):(p/name).unlink()
    manager.stage_index(lease,['a.bin','.gitattributes','nested/run.sh'])
    # Exercise an empty baseline awaiting its explicit verified tree.
    lease.macos_binding.pop('staged_tree');manager._persist(lease)
    manager.seal(lease)
    assert lease.state=='sealed' and manager.verify(lease)['valid']
    assert lease.changed_files==['.gitattributes','a.bin','nested/run.sh']


def test_later_batch_failure_does_not_publish_or_automatically_resume(ready,monkeypatch):
    manager,lease,calls,*_=ready;p=Path(lease.path);stage=index.stage;seen=[]
    for i in range(17):(p/f'new{i:02}').write_text('x')
    def interrupt(*a,**kw):
        seen.append(a[2])
        if len(seen)==2:raise OSError('Host interruption between batches')
        return stage(*a,**kw)
    monkeypatch.setattr(index,'stage',interrupt)
    with pytest.raises(OSError):manager.seal(lease)
    count=len(calls);manager.recover_owned(lease)
    assert len(calls)==count and lease.state=='leased' and not lease.macos_binding.get('result')
    monkeypatch.setattr(index,'stage',stage);manager.seal(lease)
    assert lease.state=='sealed' and manager.verify(lease)['valid']


@pytest.mark.parametrize('raw',[b'',b'# comment\n\n',b'/.gitgo/\n',b'.gitgo/\r\n'])
def test_metadata_only_ignore_preserves_business_collection(ready,raw):
    manager,lease,*_=ready;p=Path(lease.path)
    (p/'.gitignore').write_bytes(raw);(p/'ordinary.txt').write_text('keep')
    manager.seal(lease)
    assert lease.state=='sealed' and '.gitignore' in lease.changed_files and 'ordinary.txt' in lease.changed_files
    assert manager.verify(lease)['valid']


@pytest.mark.parametrize('raw',[b'*.log',b'!keep',b'.gitgo*',b'/.gitgo/**/../*',b'\xff',b' #not-a-comment',b'\\#literal'])
def test_business_or_ambiguous_ignore_rules_are_not_interpreted(raw):
    from backend.core.macos_worktree_finish import metadata_only_ignore
    assert not metadata_only_ignore(raw)


def test_nested_runtime_name_rule_remains_unsupported():
    from backend.core.macos_worktree_finish import metadata_only_ignore
    assert not metadata_only_ignore(b'.gitgo/',root=False)
