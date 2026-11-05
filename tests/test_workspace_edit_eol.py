"""Logical read/edit round trips retain exact byte-version and uniqueness gates."""
import hashlib
from backend.core.tools.workspace_tools import read_file, project_file_mutation
from backend.core.tools.file_mutation import FileMutation


def test_read_content_can_be_used_as_crlf_edit_anchor_without_touching_file(tmp_path_factory):
    root = tmp_path_factory
    path = root / 'sample.py'
    raw = b'def value():\r\n    return 1\r\n'
    path.write_bytes(raw)
    read = read_file({'_workspace': str(root), 'path': path.name})
    candidate = project_file_mutation('edit_file', {'_workspace': str(root), 'path': path.name,
        'old_string': read['content'], 'new_string': 'def value():\n    return 2', 'expected_sha256': read['sha256']})
    assert isinstance(candidate, FileMutation)
    assert candidate.payload == b'def value():\r\n    return 2\r\n'
    assert candidate.before_sha256 == hashlib.sha256(raw).hexdigest() and path.read_bytes() == raw
    path.write_bytes(raw + b'# concurrent writer\r\n')
    stale = project_file_mutation('edit_file', {'_workspace': str(root), 'path': path.name,
        'old_string': read['content'], 'new_string': 'different', 'expected_sha256': read['sha256']})
    assert stale['error'] == 'FILE_CHANGED'


def test_eol_alignment_preserves_unique_anchor_and_refuses_mixed_style_guess(tmp_path_factory):
    root = tmp_path_factory
    path = root / 'sample.txt'
    path.write_bytes(b'one\r\ntwo\r\none\r\ntwo\r\n')
    args = {'_workspace': str(root), 'path': path.name, 'old_string': 'one\ntwo', 'new_string': 'three\nfour'}
    assert project_file_mutation('edit_file', args)['error'] == 'STRING_NOT_UNIQUE'
    all_matches = project_file_mutation('edit_file', {**args, 'replace_all': True})
    assert isinstance(all_matches, FileMutation) and all_matches.replacements == 2
    assert all_matches.payload == b'three\r\nfour\r\nthree\r\nfour\r\n'
    path.write_bytes(b'one\r\ntwo\nthree\r\n')
    assert project_file_mutation('edit_file', args)['error'] == 'STRING_NOT_FOUND'
