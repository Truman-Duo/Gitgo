"""Future CAS formats must not be silently opened, salvaged or reset as v1."""
from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from backend.core.storage import StorageRuntime
from backend.core.storage.maintenance import StorageLease, StorageMaintenanceActive
from backend.core.storage.migrations import STATE_MIGRATIONS
from backend.core.storage.protocol import (
    StorageProtocolUnsupported, require_legacy_storage_format,
)
from scripts.recover_state_sqlite import recover
from scripts.reset_disposable_storage import reset_disposable, relocate_disposable
from scripts.relocate_state_storage import relocate_preserving


def _guard(connection):
    # Synthetic future format only. Production has no activation API.
    connection.execute('ALTER TABLE schema_migrations RENAME TO schema_migration_records')
    connection.execute('''CREATE VIEW schema_migrations AS
        SELECT version,name,checksum,applied_at FROM schema_migration_records
        WHERE gitgo_storage_protocol(2)=1''')
    connection.execute('''CREATE TRIGGER schema_migrations_append
        INSTEAD OF INSERT ON schema_migrations BEGIN
        INSERT INTO schema_migration_records VALUES(
            NEW.version,NEW.name,NEW.checksum,NEW.applied_at); END''')


def _store(root):
    workspace = root / 'workspace'
    workspace.mkdir()
    state_home = root / 'state'
    with StorageRuntime(workspace, state_home=state_home) as runtime:
        runtime.put_state_ref('fixture', 'fact', 'opaque-fixture')
        paths = runtime.paths
    return workspace, state_home, paths


@pytest.mark.parametrize('shape', ['guard', 'mixed_guard', 'view_only', 'records_only', 'marker_only',
                                 'future', 'missing', 'empty', 'gap'])
def test_future_format_rejected_before_writer_setup(tmp_path_factory, monkeypatch, shape):
    workspace, state_home, paths = _store(tmp_path_factory)
    with closing(sqlite3.connect(paths.state_db)) as connection:
        if shape == 'guard':
            _guard(connection)
        elif shape == 'mixed_guard':
            connection.execute('ALTER TABLE schema_migrations RENAME TO SCHEMA_MIGRATION_RECORDS')
            connection.execute('''CREATE VIEW SCHEMA_MIGRATIONS AS
                SELECT * FROM SCHEMA_MIGRATION_RECORDS WHERE gitgo_storage_protocol(2)=1''')
        elif shape == 'view_only':
            connection.execute('DROP TABLE schema_migrations')
            connection.execute('CREATE VIEW schema_migrations AS SELECT unknown_protocol()')
        elif shape == 'records_only':
            connection.execute('ALTER TABLE schema_migrations RENAME TO schema_migration_records')
        elif shape == 'marker_only':
            connection.execute('CREATE TABLE StOrAgE_PrOtOcOl(required INTEGER)')
        elif shape == 'missing':
            connection.execute('DROP TABLE schema_migrations')
        elif shape == 'empty':
            connection.execute('DELETE FROM schema_migrations')
        elif shape == 'gap':
            connection.execute('DELETE FROM schema_migrations WHERE version=1')
        else:
            connection.execute("INSERT INTO schema_migrations VALUES(999,'future','fixture','fixture')")
        connection.commit()
    before = paths.state_db.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('writer setup/maintenance must not run')
    monkeypatch.setattr(StorageRuntime, '_apply_migrations', forbidden)
    monkeypatch.setattr(StorageRuntime, 'maintain_cas', forbidden)
    with pytest.raises(StorageProtocolUnsupported):
        StorageRuntime(workspace, state_home=state_home)
    assert paths.state_db.read_bytes() == before
    # Failed construction released its lease; explicit maintenance can inspect.
    with StorageLease(paths.project_root, exclusive=True):
        pass


def test_admission_is_read_only_and_does_not_activate_protocol(tmp_path_factory):
    workspace, state_home, paths = _store(tmp_path_factory)
    with closing(sqlite3.connect(paths.state_db)) as connection:
        before = connection.execute('SELECT * FROM schema_migrations').fetchall()
        statements = []
        connection.set_trace_callback(statements.append)
        require_legacy_storage_format(connection, STATE_MIGRATIONS)
        assert connection.total_changes == 0
        assert all(statement.lstrip().upper().startswith('SELECT') for statement in statements)
    with StorageRuntime(workspace, state_home=state_home) as runtime:
        assert runtime.get_state_ref('fixture', 'fact') == 'opaque-fixture'
        assert runtime._state.execute('SELECT * FROM schema_migrations').fetchall()
    with closing(sqlite3.connect(paths.state_db)) as connection:
        assert connection.execute('SELECT * FROM schema_migrations').fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name='storage_protocol'").fetchone()[0] == 0


def test_unreadable_history_is_not_treated_as_new_store():
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('CREATE TABLE schema_migrations(incomplete INTEGER)')
        with pytest.raises(sqlite3.OperationalError):
            require_legacy_storage_format(connection, STATE_MIGRATIONS)
        assert not connection.in_transaction


def test_checksum_still_checked_before_migration(tmp_path_factory):
    _, _, paths = _store(tmp_path_factory)
    with closing(sqlite3.connect(paths.state_db)) as connection:
        connection.execute("UPDATE schema_migrations SET checksum='fixture' WHERE version=1")
        connection.commit()
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        StorageRuntime.require_legacy_state_file(paths.state_db)


@pytest.mark.parametrize('tool', ['salvage', 'reset', 'relocate_reset', 'relocate_preserving'])
@pytest.mark.parametrize('shape', ['guard', 'mixed_guard', 'missing', 'empty'])
def test_offline_tools_refuse_to_strip_future_protocol(tmp_path_factory, monkeypatch, tool, shape):
    workspace, state_home, paths = _store(tmp_path_factory)
    with closing(sqlite3.connect(paths.state_db)) as connection:
        if shape == 'guard':
            _guard(connection)
        elif shape == 'mixed_guard':
            connection.execute('ALTER TABLE schema_migrations RENAME TO SCHEMA_MIGRATION_RECORDS')
            connection.execute('''CREATE VIEW SCHEMA_MIGRATIONS AS
                SELECT * FROM SCHEMA_MIGRATION_RECORDS WHERE gitgo_storage_protocol(2)=1''')
            connection.execute('''CREATE TRIGGER SCHEMA_MIGRATIONS_APPEND
                INSTEAD OF INSERT ON SCHEMA_MIGRATIONS BEGIN
                INSERT INTO SCHEMA_MIGRATION_RECORDS VALUES(
                    NEW.version,NEW.name,NEW.checksum,NEW.applied_at); END''')
        elif shape == 'missing':
            connection.execute('DROP TABLE schema_migrations')
        else:
            connection.execute('DELETE FROM schema_migrations')
        connection.commit()
    before = paths.state_db.read_bytes()
    destination_home = tmp_path_factory / 'destination'
    destination_db = tmp_path_factory / 'salvaged.sqlite3'
    monkeypatch.setenv('GITGO_STATE_HOME', str(state_home))
    with pytest.raises(StorageProtocolUnsupported):
        if tool == 'salvage':
            recover(paths.state_db, destination_db, paths.cas_dir)
        elif tool == 'reset':
            reset_disposable(workspace, expected_project_id=paths.project_id, discard_test_data=True)
        elif tool == 'relocate_reset':
            relocate_disposable(workspace, expected_project_id=paths.project_id,
                source_home=state_home, destination_home=destination_home, discard_test_data=True)
        else:
            relocate_preserving(workspace, expected_project_id=paths.project_id,
                source_home=state_home, destination_home=destination_home)
    assert paths.state_db.read_bytes() == before
    assert not destination_db.exists()
    assert not (destination_home / 'projects' / paths.project_id).exists()
    assert not (paths.project_root / 'storage-recovery-in-progress.json').exists()
    assert not (state_home / 'recovery').exists()


@pytest.mark.parametrize('initial_history', [False, True])
def test_empty_store_and_uncommitted_first_migration_can_initialize(initial_history):
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.row_factory = sqlite3.Row
        if initial_history:
            connection.execute('''CREATE TABLE SCHEMA_MIGRATIONS (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                checksum TEXT NOT NULL, applied_at TEXT NOT NULL)''')
            # Migration DDL and its history row roll back together.
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE unfinished(value INTEGER)')
            connection.rollback()
        require_legacy_storage_format(connection, STATE_MIGRATIONS)
        StorageRuntime._apply_migrations(connection, STATE_MIGRATIONS)
        assert connection.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == len(STATE_MIGRATIONS)


def test_known_prefix_upgrades_without_changing_existing_history():
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.row_factory = sqlite3.Row
        StorageRuntime._apply_migrations(connection, STATE_MIGRATIONS[:2])
        before = [tuple(row) for row in connection.execute('SELECT * FROM schema_migrations ORDER BY version')]
        require_legacy_storage_format(connection, STATE_MIGRATIONS)
        StorageRuntime._apply_migrations(connection, STATE_MIGRATIONS)
        after = [tuple(row) for row in connection.execute('SELECT * FROM schema_migrations ORDER BY version')]
        assert after[:2] == before
        assert len(after) == len(STATE_MIGRATIONS)


@pytest.mark.parametrize('shape', ['duplicate', 'text_version', 'gap', 'extra_trigger'])
def test_noncanonical_history_is_not_evidence_of_supported_format(shape):
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.execute('CREATE TABLE schema_migrations(version, checksum)')
        first = STATE_MIGRATIONS[0]
        connection.execute('INSERT INTO schema_migrations VALUES(?,?)', (first.version, first.checksum))
        if shape == 'duplicate':
            connection.execute('INSERT INTO schema_migrations VALUES(?,?)', (first.version, first.checksum))
        elif shape == 'text_version':
            connection.execute("UPDATE schema_migrations SET version='1'")
        elif shape == 'gap':
            connection.execute('DELETE FROM schema_migrations')
            second = STATE_MIGRATIONS[1]
            connection.execute('INSERT INTO schema_migrations VALUES(?,?)', (second.version, second.checksum))
        else:
            connection.execute('''CREATE TRIGGER schema_migrations AFTER INSERT ON schema_migrations
                BEGIN SELECT 1; END''')
        with pytest.raises(StorageProtocolUnsupported):
            require_legacy_storage_format(connection, STATE_MIGRATIONS)


def _migration_process(root, committed, ready, release):
    with StorageLease(Path(root), exclusive=True):
        connection = sqlite3.connect(Path(root) / 'state.sqlite3', isolation_level=None)
        connection.execute('BEGIN IMMEDIATE')
        _guard(connection)
        if committed:
            connection.execute('COMMIT')
        ready.set()
        if not release.wait(15):
            os._exit(24)
        os._exit(23)


@pytest.mark.parametrize('committed', [False, True])
def test_protocol_transition_process_exit_preserves_known_format(tmp_path_factory, committed):
    workspace, state_home, paths = _store(tmp_path_factory)
    context = multiprocessing.get_context('spawn')
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_migration_process,
        args=(str(paths.project_root), committed, ready, release))
    process.start()
    try:
        assert ready.wait(15)
        with pytest.raises(StorageMaintenanceActive):
            StorageRuntime(workspace, state_home=state_home)
        release.set()
        process.join(15)
        assert process.exitcode == 23
        if committed:
            with pytest.raises(StorageProtocolUnsupported):
                StorageRuntime(workspace, state_home=state_home)
            with closing(sqlite3.connect(paths.state_db)) as connection:
                with pytest.raises(sqlite3.OperationalError, match='no such function'):
                    connection.execute('SELECT * FROM schema_migrations').fetchall()
        else:
            with StorageRuntime(workspace, state_home=state_home) as runtime:
                assert runtime.get_state_ref('fixture', 'fact') == 'opaque-fixture'
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()
