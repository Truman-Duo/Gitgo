import json
import sqlite3
from contextlib import closing
import pytest
from scripts.terminal_test_evidence import archive_state


def test_snapshot_includes_committed_wal_and_cas_but_excludes_credentials(tmp_path_factory):
    root = tmp_path_factory / 'run'
    project = root / 'state/projects/project'
    cas = project / 'cas/ab'
    cas.mkdir(parents=True)
    (cas / 'cd').write_text('saved answer')
    (root / 'provider_secrets.json').write_text('credential fixture')
    (root / 'state/auth.json').write_text('daemon credential fixture')
    with closing(sqlite3.connect(project / 'state.sqlite3')) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE facts(value TEXT)')
        writer.execute("INSERT INTO facts VALUES ('committed')")
        writer.commit()
        writer.execute("INSERT INTO facts VALUES ('uncommitted')")
        result = archive_state(root, tmp_path_factory / 'archive')
    assert result['ok']
    archived = tmp_path_factory / 'archive/state/projects/project'
    with closing(sqlite3.connect(archived / 'state.sqlite3')) as reader:
        assert reader.execute('SELECT value FROM facts').fetchall() == [('committed',)]
    assert (archived / 'cas/ab/cd').read_text() == 'saved answer'
    paths = json.loads((tmp_path_factory / 'archive/manifest.json').read_text())['files']
    assert len(paths) == 2
    assert not any('secret' in path or 'auth.json' in path for path in paths)


def test_oversized_archive_is_rejected_before_partial_copy(tmp_path_factory):
    root = tmp_path_factory / 'run'
    cas = root / 'state/projects/project/cas/ab'
    cas.mkdir(parents=True)
    (cas / 'cd').write_text('too much evidence')
    destination = tmp_path_factory / 'archive'
    with pytest.raises(ValueError, match='exceeds limit'):
        archive_state(root, destination, max_bytes=1)
    assert not destination.exists()
