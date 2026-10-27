"""Real Git/state regressions; native execution is an explicit protocol seam."""
from copy import deepcopy
import json

import pytest

from tests.test_publish_commands import setup, call, git, prepare as work_prepare, adopt
from tests.test_publish_formal import formal_setup, prepare, command, protocol
from backend.core.publish.git import CandidateGit
from backend.core.publish.state import Repository, MAX_BYTES, encoded, read_ledger
from backend.core.storage import get_storage, close_owned_storage


def test_prewrite_target_change_is_failed_and_can_be_reprepared(formal_setup, monkeypatch):
    _, _, repo = formal_setup
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    original = CandidateGit.integrate
    peer = git(repo, "rev-parse", "HEAD")
    def race(self, candidate, plan, result_oid):
        git(repo, "update-ref", "refs/heads/formal", peer)
        return original(self, candidate, plan, result_oid)
    monkeypatch.setattr(CandidateGit, "integrate", race)
    failed = command(formal_setup, "execute", checked)
    assert failed["error"] == "PUBLISH_TARGET_CHANGED"
    item = failed["item"]
    assert item["status"] == "failed" and item["allowed_actions"] == ["revise"]
    assert not item["formal_eligible"] and not any(e["current"] for e in item["evidence"])
    assert git(repo, "rev-parse", "formal") == peer
    (repo / "followup.txt").write_bytes(b"new candidate\n")
    git(repo, "add", "followup.txt"); git(repo, "commit", "-m", "followup")
    revised = prepare(formal_setup, "reprepare", item_id=item["id"], expected_revision=item["revision"])
    assert revised["status"] == "prepared" and not revised["formal_eligible"]
    assert revised["plan"]["expected_target_oid"] == peer


@pytest.mark.parametrize("exit_code,status", [(128, "failed"), (-1, "executing")])
def test_rejected_transaction_and_timeout_have_different_recovery(formal_setup, monkeypatch, exit_code, status):
    from subprocess import CompletedProcess
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    monkeypatch.setattr(CandidateGit, "integrate", lambda *a: CompletedProcess([], exit_code, "", "injected"))
    result = command(formal_setup, "execute", checked)
    assert result["item"]["status"] == status
    if status == "executing":
        assert result["effect_state"] == "ambiguous"
        assert command(formal_setup, "reconcile", result["item"])["effect_state"] == "ambiguous"


def test_adoption_prewrite_refusal_is_revisable(setup, monkeypatch):
    item = work_prepare(setup)["item"]
    original = CandidateGit.adopt
    def race(self, candidate, ref):
        git(setup[2], "branch", "publish/first")
        return original(self, candidate, ref)
    monkeypatch.setattr(CandidateGit, "adopt", race)
    failed = adopt(setup, item)
    assert failed["error"] == "PUBLISH_TARGET_EXISTS"
    item = failed["item"]
    assert item["status"] == "failed" and item["allowed_actions"] == ["revise"]
    revised = work_prepare(setup, "new-plan", item_id=item["id"], expected_revision=item["revision"], target_branch="publish/second")
    assert revised["item"]["status"] == "prepared"


@pytest.mark.parametrize("method", ["rebase", "cherry_pick"])
def test_replay_retains_each_original_author_and_date(formal_setup, monkeypatch, method):
    _, project, repo = formal_setup
    assert project.authorship["mode"] == "mixed"
    (repo / "alice.txt").write_bytes(b"contribution\n")
    git(repo, "add", "alice.txt")
    git(repo, "commit", "--author=Alice <alice@review.invalid>", "--date=2001-02-03T04:05:06+05:30", "-m", "alice contribution")
    available = git(repo, "rev-list", "--reverse", "formal..HEAD").splitlines()
    item = prepare(formal_setup, method=method, **({"selected_commits": available} if method == "cherry_pick" else {}))
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    integrated = command(formal_setup, "execute", checked)["item"]
    assert integrated["status"] == "integrated"
    result = git(repo, "rev-list", "--reverse", checked["plan"]["expected_target_oid"] + "..formal").splitlines()
    assert len(result) == len(available) == 2
    for source, replay in zip(available, result):
        assert git(repo, "show", "-s", "--format=%an <%ae>%n%at %ai", source) == git(repo, "show", "-s", "--format=%an <%ae>%n%at %ai", replay)
        assert git(repo, "show", "-s", "--format=%cn <%ce>", replay) == "Tester <test@local.gitgo.invalid>"
    assert git(repo, "rev-parse", "formal^{tree}") == checked["plan"]["tree_oid"]


def test_history_capacity_cannot_strand_successful_ref_or_cold_recovery(setup, monkeypatch):
    _, _, repo = setup
    item = work_prepare(setup)["item"]
    original = Repository.save
    filled = False
    def fill_at_intent(self, ledger, expected):
        nonlocal filled
        row = ledger["items"][item["id"]]
        if row["status"] == "executing" and not filled:
            filled = True
            # Historical-shaped synthetic stress data, never production facts.
            while True:
                evidence = {"id": "old-" + str(len(row["evidence"])), "binding": "old", "kind": "git_diff_check", "outcome": "passed", "summary": ""}
                row["evidence"].append(evidence)
                remaining = MAX_BYTES - 64 - len(encoded(ledger))
                assert remaining >= 0
                evidence["summary"] = "x" * min(remaining, 8000)
                if remaining <= 8000:
                    break
        return original(self, ledger, expected)
    monkeypatch.setattr(Repository, "save", fill_at_intent)
    saved = adopt(setup, item)
    assert saved["item"]["status"] == "adopted", saved
    assert git(repo, "rev-parse", "publish/first") == item["candidate_oid"]
    ledger, ref = read_ledger(repo)
    assert len(encoded(ledger)) > MAX_BYTES
    storage = get_storage(repo)
    manifest = json.loads(storage.read_blob(ref))
    assert manifest["schema_version"] == 2
    assert all(p[7:] in storage._cas_reference_inventory().require_complete() for p in manifest["pages"])
    close_owned_storage()
    assert read_ledger(repo)[0] == ledger


def test_paged_state_cas_conflict_does_not_publish_page_ownership(setup, monkeypatch):
    _, _, repo = setup
    item = work_prepare(setup)["item"]
    storage = get_storage(repo)
    repository = Repository(str(repo), storage)
    ledger, before = repository.load()
    ledger["items"][item["id"]]["rework"] = "x" * MAX_BYTES
    other = deepcopy(ledger)
    other["items"][item["id"]]["rework"] = "competing change"
    latest = repository.save(other, before)
    with pytest.raises(ValueError, match="PUBLISH_REVISION_CONFLICT"):
        repository.save(ledger, before)
    assert repository.load()[1] == latest
    assert storage._state.execute("SELECT count(*) FROM object_refs WHERE owner_type='state_revision'").fetchone()[0] == 0


def test_paged_state_missing_page_never_falls_back(setup):
    _, _, repo = setup
    item = work_prepare(setup)["item"]
    storage = get_storage(repo)
    repository = Repository(str(repo), storage)
    ledger, before = repository.load()
    ledger["items"][item["id"]]["rework"] = "x" * MAX_BYTES
    ref = repository.save(ledger, before)
    manifest = json.loads(storage.read_blob(ref))
    page = manifest["pages"][0][7:]
    (storage.paths.cas_dir / page[:2] / page[2:]).unlink()
    with pytest.raises((OSError, RuntimeError, ValueError)):
        read_ledger(repo)


def test_storage_capacity_failure_after_effect_recovers_without_replay(setup, monkeypatch):
    from backend.core.storage.models import StorageBlocked
    item = work_prepare(setup)["item"]
    original = Repository.save
    failed = False
    def capacity(self, ledger, expected):
        nonlocal failed
        if ledger["items"][item["id"]]["status"] == "adopted" and not failed:
            failed = True
            raise StorageBlocked("injected physical capacity failure")
        return original(self, ledger, expected)
    monkeypatch.setattr(Repository, "save", capacity)
    with pytest.raises(StorageBlocked):
        adopt(setup, item)
    ledger, _ = read_ledger(setup[2])
    pending = ledger["items"][item["id"]]
    assert pending["status"] == "executing"
    monkeypatch.setattr(CandidateGit, "adopt", lambda *a: pytest.fail("recovery repeated a ref write"))
    result = call(setup, "reconcile", "recover-capacity", item_id=item["id"], expected_revision=pending["revision"])
    assert result["item"]["status"] == "adopted"


def test_lost_ack_then_peer_advance_is_never_reclassified_as_refused(formal_setup, monkeypatch):
    item = prepare(formal_setup)
    protocol(monkeypatch)
    checked = command(formal_setup, "verify", item)["item"]
    original = CandidateGit.integrate
    def lose_ack(self, *args):
        assert original(self, *args).returncode == 0
        git(formal_setup[2], "update-ref", "refs/heads/formal", "HEAD")
        raise OSError("lost acknowledgement after write and later peer update")
    monkeypatch.setattr(CandidateGit, "integrate", lose_ack)
    pending = command(formal_setup, "execute", checked)
    assert pending["effect_state"] == "ambiguous" and pending["item"]["status"] == "executing"
    monkeypatch.setattr(CandidateGit, "integrate", lambda *a: pytest.fail("recovery repeated a ref write"))
    observed = command(formal_setup, "reconcile", pending["item"])
    assert observed["effect_state"] == "ambiguous" and observed["item"]["status"] == "executing"


@pytest.mark.parametrize("identity", [
    {"name": "Alice\nGIT_CONFIG=x", "email": "alice@review.invalid", "date": "1 +0000"},
    {"name": "Alice", "email": "alice@review.invalid", "date": "yesterday"},
])
def test_author_override_cannot_expand_the_git_boundary(identity):
    from backend.core.loop.git_execution import commit_author_environment, GitBoundaryError
    with pytest.raises(GitBoundaryError):
        commit_author_environment(["commit-tree"], identity)
    with pytest.raises(GitBoundaryError):
        commit_author_environment(["update-ref"], {"name": "Alice", "email": "a@review.invalid", "date": "1 +0000"})
