"""Reviews retain their scope; intent is neither an authorization nor a test pass."""
import pytest

from tests.test_publish_commands import setup, call
from tests.test_publish_formal import formal_setup, prepare, protocol, command
from tests.test_publish_environment import environment_root
from backend.core.publish.state import read_ledger


def decide(setup, item, decision, request):
    return call(setup, "decide", request, item_id=item["id"], expected_revision=item["revision"],
                decision=decision, note="Inspect and repair the declared failure", owner="review-owner")["item"]


def test_review_rework_blocks_formal_but_preserves_checks(formal_setup, environment_root, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    reviewed = decide(formal_setup, checked, "rework", "review1")
    assert not reviewed["formal_eligible"] and reviewed["reason"] == "PUBLISH_REVIEW_REWORK"
    assert reviewed["rework"] and reviewed["owner"] == "review-owner"
    assert command(formal_setup, "execute", reviewed)["error"] == "PUBLISH_REVIEW_REQUIRED"
    accepted = decide(formal_setup, reviewed, "accept", "review2")
    assert accepted["formal_eligible"] and len(accepted["reviews"]) == 2
    assert command(formal_setup, "execute", accepted, "approved-execute")["item"]["status"] == "integrated"


def test_review_cannot_promote_failed_checks_or_survive_new_candidate(formal_setup, environment_root, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch, passed=False)
    checked = command(formal_setup, "verify", item)["item"]
    accepted = decide(formal_setup, checked, "accept", "review")
    assert not accepted["formal_eligible"]
    revised = prepare(formal_setup, "revision", item_id=item["id"], expected_revision=accepted["revision"])
    assert revised["reviews"][0]["binding"] != revised["binding"]
    assert not revised["formal_eligible"]


def test_operation_query_is_read_only_and_keeps_task_and_evidence(formal_setup, environment_root, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    before = read_ledger(formal_setup[2])
    result = formal_setup[0].invoke("publish.operation", {"project": formal_setup[1].name,
        "item_id": item["id"], "operation_id": checked["operations"][0]["id"]})
    assert result["operation"]["actor"] == "local_operator"
    assert {e["kind"] for e in result["evidence"]} == {"environment", "project_test"}
    assert read_ledger(formal_setup[2]) == before
