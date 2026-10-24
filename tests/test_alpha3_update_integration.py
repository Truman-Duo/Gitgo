"""Contracts at the Alpha 2.1 sandbox/managed-update merge seams."""

import io
import subprocess
import time
from types import SimpleNamespace

import pytest


def test_update_health_and_protected_checkpoint_share_the_storage_facade(monkeypatch):
    from backend.core.config import ConfigManager
    from backend.core.storage import runtime
    from backend.core.update.runtime_contract import data_probe

    def forbidden(*args, **kwargs):
        pytest.fail("Merge validation must not open project state or load configuration")

    monkeypatch.setattr(ConfigManager, "load", forbidden)
    monkeypatch.setattr(runtime, "resolve_storage_paths", forbidden)
    report = data_probe("a" * 32)
    schema, columns = runtime._protected_checkpoint_catalogue()
    assert report["memory_sqlite_check"] is True
    assert schema and columns
    assert {name for name, _ in columns} >= {
        "schema_migration_records", "storage_protocol", "storage_batches", "storage_batch_members",
    }
    assert runtime._protected_checkpoint_catalogue() == (schema, columns)


def test_search_preserves_installation_lease_and_private_stdin_eof(monkeypatch, tmp_path_factory):
    from backend.core import installation_lease
    from backend.core.tools import workspace_search

    captured = {}
    proc = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(b"match\n"),
        stderr=io.BytesIO(), poll=lambda: 0, wait=lambda **kwargs: 0)

    def spawn(argv, **options):
        captured.update(options)
        return proc

    monkeypatch.setattr(installation_lease, "child_options", lambda: {"pass_fds": (17,)})
    monkeypatch.setattr(workspace_search.subprocess, "Popen", spawn)
    monkeypatch.setattr(workspace_search, "attach_kill_job", lambda child: None)
    records = []
    result = workspace_search.stream_process(["search-fixture"], tmp_path_factory, b"\n",
        lambda record: records.append(record) or True, time.monotonic() + 5)
    assert result.exit_code == 0 and not result.stopped
    assert records == [b"match"]
    assert captured["pass_fds"] == (17,)
    assert captured["stdin"] == subprocess.PIPE and proc.stdin.closed


@pytest.mark.parametrize("active", [False, True])
def test_managed_job_close_uses_owned_scope_and_cancellation_state(monkeypatch, active):
    from backend.core import process_control

    calls = []
    scope = SimpleNamespace(proc=SimpleNamespace(poll=lambda: None if active else 0),
        finish=lambda **options: calls.append(options))
    monkeypatch.setattr(process_control, "sys", SimpleNamespace(platform="win32"))
    process_control.close_job(scope)
    assert calls == [{"cancel": active}]


def test_sandbox_native_job_close_keeps_its_existing_cleanup_authority(monkeypatch):
    from backend.core import process_control

    calls = []
    native_job = SimpleNamespace(close=lambda: calls.append("native retirement"))
    monkeypatch.setattr(process_control, "sys", SimpleNamespace(platform="win32"))
    process_control.close_job(native_job)
    assert calls == ["native retirement"]
