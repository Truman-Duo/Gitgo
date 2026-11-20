"""ACL exclusivity checks on real worktree metadata and publication parents.

These tests do not claim an approval bypass. A nonempty ACL, including a
deny-only ACL, makes this protected backend refuse the object without editing
its permissions. Ordinary source-file reads have no new ACL policy.
"""
import os
from pathlib import Path
import sys

import pytest

if sys.platform != 'darwin':
    pytest.skip('Actual Darwin worktree ACLs', allow_module_level=True)

from backend.core import macos_promotion_write as publication
from backend.core.macos_file_authority import FileAuthorityError
from backend.core.loop.worktree import WorktreeError
from tests.test_macos_existing_authority import acl
from tests.test_macos_worktree import case, create, repository, commit_fixture


@pytest.mark.parametrize('where', ['parent', 'metadata_directory', 'metadata_file', 'lock'])
@pytest.mark.parametrize('denial_only', [False, True])
def test_worktree_verification_rejects_acl_without_mutating_lease(case, where, denial_only):
    manager, _, _, _, _ = case
    lease = create(case)
    assert manager.verify(lease)['valid']
    state = manager.storage.load_worktree_state(lease.process_id)
    meta = manager.repo_root / '.git/worktrees' / lease.macos_binding['metadata']
    target = {'parent': manager.worktrees_root, 'metadata_directory': meta,
              'metadata_file': meta / 'index',
              'lock': manager.worktrees_root / '.gitgo-lifecycle.lock'}[where]
    entry = ('everyone deny delete' if denial_only else
             'everyone allow add_file,delete_child' if target.is_dir() else
             'everyone allow write,append')
    before = target.stat()
    old_index = (meta / 'index').read_bytes()
    with acl(target, entry):
        verdict = manager.verify(lease)
        assert not verdict['valid'] and 'ACL-free object' in verdict['reason']
        assert manager.storage.load_worktree_state(lease.process_id) == state
        assert (meta / 'index').read_bytes() == old_index
        assert target.stat().st_ino == before.st_ino
        # Verification must not quietly strip the ACL to pass the second time.
        assert not manager.verify(lease)['valid']
    assert manager.verify(lease)['valid']


@pytest.mark.parametrize('where', ['project', 'nested'])
@pytest.mark.parametrize('denial_only', [False, True])
def test_formal_publication_parent_acl_is_rejected_before_write(case, where, denial_only):
    manager, _, _, _, _ = case
    target = manager.repo_root if where == 'project' else manager.repo_root / 'selected'
    if where == 'nested': target.mkdir(mode=0o700)
    leaf = target / 'existing.txt'; leaf.write_bytes(b'unchanged')
    relative = str(leaf.relative_to(manager.repo_root))
    before = leaf.stat()
    with publication.destination(manager, [relative]) as (_, _, guard, _):
        guard()
    entry = 'everyone deny delete' if denial_only else 'everyone allow add_file,delete_child'
    with acl(target, entry):
        with pytest.raises(WorktreeError, match='ACL-free object') as caught:
            with publication.destination(manager, [relative]):
                pytest.fail('ACL publication parent was accepted')
        assert isinstance(caught.value.__cause__, FileAuthorityError)
        assert leaf.read_bytes() == b'unchanged'
        after = leaf.stat()
        assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)
    with publication.destination(manager, [relative]) as (_, _, guard, _):
        guard()
