"""Explicit legacy handoff keeps backups and prevents competing old writers."""
from copy import deepcopy
import json

import pytest

from backend.core.config import Config
from backend.core.publish.state import PublishError, read_ledger
from backend.core.storage import get_storage
from backend.core.sync_session import SyncSession
from backend.core.sync_session.state import read_state_snapshot, save_state
from tests.test_publish_commands import setup, call
from tests.test_publish_formal import formal_setup


@pytest.fixture
def old(formal_setup):
    data = {"stage": "IDLE", "formal_commits": [{"prefix": "OLD", "number": 17,
        "message": "[OLD-17] feat: previous output", "synced": True, "pushed": False,
        "created_at": "2025-10-01", "source_indices": [0]}]}
    save_state(formal_setup[2], data, expected_ref=None)
    return formal_setup


def observe(setup):
    return setup[0].invoke("publish.items", {"project": setup[1].name})["items"][-1]


def migrate(setup, item=None, request="migration1"):
    return call(setup, "migrate", request, legacy_id=(item or observe(setup))["id"], source_role="workspace",
        source_ref="HEAD", target_branch="formal", owner="maintainer", test_target="test_release.py")


def test_explicit_migration_backs_up_and_never_promotes_legacy_flags(old):
    before, reference = read_state_snapshot(old[2])
    source = observe(old)
    result = migrate(old, source)
    item = result["item"]
    assert item["status"] == "prepared" and not item["formal_eligible"]
    assert item["legacy_source"]["record"]["synced"] is True
    backup = json.loads(get_storage(old[2]).read_blob(item["legacy_source"]["backup_ref"]))
    assert backup == {"state": before, "reference": reference}
    assert read_state_snapshot(old[2]) == (before, reference)
    imported = observe(old)
    assert imported["reason"] == "LEGACY_IMPORTED" and imported["allowed_actions"] == []
    assert imported["source"]["imported_item_id"] == item["id"]
    assert migrate(old, source) == result


def test_snapshot_change_and_duplicate_import_are_rejected(old):
    stale = observe(old)
    data, ref = read_state_snapshot(old[2])
    data["formal_commits"][0]["message"] = "[OLD-17] fix: changed observation"
    save_state(old[2], data, expected_ref=ref)
    assert migrate(old, stale)["error"] == "PUBLISH_LEGACY_SNAPSHOT_CHANGED"
    assert "item" in migrate(old, request="migration2")
    assert migrate(old, request="migration3")["error"] == "PUBLISH_LEGACY_ALREADY_MIGRATED"


def test_old_record_is_readable_but_cannot_be_edited_synced_or_pushed(old, monkeypatch):
    # A preexisting session cannot bypass a later handoff.
    session = SyncSession(old[1], Config(projects=[old[1]]))
    migrate(old)
    original = session.formal_commits[0].message
    for action in [lambda: session.step_edit_formal_message(0, "[OLD-17] fix: changed"),
                   lambda: session.step_edit_formal_number(0, 18), lambda: session.step_delete_formal(0),
                   lambda: session.step_clear_formal_sources(0), lambda: session.step_dissolve_formal(0),
                   lambda: session.step_sync(0), lambda: session.step_push()]:
        with pytest.raises(PublishError, match="PUBLISH_LEGACY_MIGRATED"):
            action()
        assert session.formal_commits[0].message == original


def test_old_state_cas_cannot_erase_import_mapping_or_mutate_frozen_record(old):
    migrate(old)
    data, ref = read_state_snapshot(old[2])
    changed = deepcopy(data)
    changed["formal_commits"] = []
    with pytest.raises(PublishError, match="PUBLISH_LEGACY_MIGRATED"):
        save_state(old[2], changed, expected_ref=ref)
    # Unrelated old records may still be added; the handed-off row stays frozen.
    data["formal_commits"].append({"prefix": "OLD", "number": 18, "message": "unrelated"})
    save_state(old[2], data, expected_ref=ref)
    assert len(read_state_snapshot(old[2])[0]["formal_commits"]) == 2


def test_ambiguous_old_identity_is_never_guessed(old):
    data, ref = read_state_snapshot(old[2])
    data["formal_commits"].append(deepcopy(data["formal_commits"][0]))
    save_state(old[2], data, expected_ref=ref)
    assert migrate(old)["error"] == "PUBLISH_LEGACY_IDENTITY_AMBIGUOUS"
    assert not (read_ledger(old[2])[0] or {}).get("items")


def test_explicit_already_present_history_is_observed_without_claiming_checks(old):
    from tests.test_publish_commands import git
    from tests.test_publish_formal import command
    git(old[2], "update-ref", "refs/heads/formal", git(old[2], "rev-parse", "HEAD"))
    result = migrate(old)
    assert "item" in result, result
    item = result["item"]
    assert item["status"] == "observed" and not item["formal_eligible"]
    assert item["evidence"][-1]["outcome"] == "unverified"
    assert item["evidence"][-1]["target_oid"] == git(old[2], "rev-parse", "formal")
    assert item["recorded"]["synced"] is False and item["recorded"]["pushed"] is False
    assert item["legacy_source"]["backup_ref"].startswith("sha256:")
    assert command(old, "execute", item)["error"] == "PUBLISH_ACTION_UNAVAILABLE"
