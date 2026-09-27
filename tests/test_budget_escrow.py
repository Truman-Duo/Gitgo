import pytest

from backend.core.loop.budget import (
    TaskBudgetExceeded,
    TaskTreeBudget,
)


def _budget():
    budget = TaskTreeBudget.create("budget-test", {
        "max_agents": 5,
        "max_provider_calls": 10,
        "max_output_tokens": 10_000,
        "max_seconds": 300,
        "verification_reserve_ratio": 0.20,
        "default_child_share": 0.30,
    })
    budget.register_process(
        "root", parent_id=None, actor_kind="supervisor", max_steps=50,
    )
    return budget


def test_child_escrow_is_protected_from_root_and_hard_bounded():
    budget = _budget()
    budget.reserve_agent()
    lease = budget.register_process(
        "child", parent_id="root", actor_kind="worker", max_steps=20,
        request={"provider_calls": 3, "output_tokens": 2_000},
    )
    assert lease["reserved_provider_calls"] == 3

    # Work pool is 8 calls (20% is protected for verification). The root may
    # use five while three are escrowed to the child, but not steal the sixth.
    for _ in range(5):
        budget.begin_provider_call("root")
    with pytest.raises(TaskBudgetExceeded) as error:
        budget.begin_provider_call("root")
    assert error.value.code == "TASK_TREE_VERIFICATION_RESERVE_PROTECTED"

    for _ in range(3):
        budget.begin_provider_call("child")
    with pytest.raises(TaskBudgetExceeded) as error:
        budget.begin_provider_call("child")
    assert error.value.code == "TASK_TREE_CHILD_PROVIDER_ESCROW_EXHAUSTED"


def test_terminal_child_refunds_only_unused_escrow_and_snapshot_restores_it():
    budget = _budget()
    budget.reserve_agent()
    budget.register_process(
        "child", parent_id="root", actor_kind="worker", max_steps=20,
        request={"provider_calls": 3, "output_tokens": 2_000},
    )
    budget.begin_provider_call("child")
    released = budget.release_process("child", status="completed")
    assert released["refunded_provider_calls"] == 2
    assert released["used_provider_calls"] == 1

    restored = TaskTreeBudget.from_snapshot(budget.snapshot())
    assert restored is not None
    card = restored.decision_card("child")
    assert card["current_process"]["state"] == "released"
    assert card["current_process"]["refunded_provider_calls"] == 2

    # Refunded capacity can be escrowed to a later worker; the consumed call
    # remains charged to the tree.
    restored.reserve_agent()
    successor = restored.register_process(
        "successor", parent_id="root", actor_kind="worker", max_steps=20,
        request={"provider_calls": 3, "output_tokens": 2_000},
    )
    assert successor["reserved_provider_calls"] == 3


def test_budget_card_is_concise_and_keeps_capability_separate_from_resources():
    budget = _budget()
    card = budget.decision_card("root")
    assert card["state"] == "normal"
    assert card["agents"] == {"used": 1, "maximum": 5, "remaining": 4}
    assert card["provider_calls"]["verification_reserve"] == 2
    assert "capability" not in card
    assert "Routine persistence/testing" in card["policy"]


def test_default_reviewer_escrow_fits_inside_protected_verification_pool():
    budget = _budget()
    budget.reserve_agent()
    lease = budget.register_process(
        "reviewer", parent_id="root", actor_kind="reviewer", max_steps=10,
    )
    assert lease["purpose"] == "verification"
    assert 1 <= lease["reserved_provider_calls"] <= 2
    budget.begin_provider_call("reviewer")
    released = budget.release_process("reviewer", status="completed")
    assert released["state"] == "released"


def test_default_reviewer_escrow_can_finish_a_multifile_production_review():
    budget = TaskTreeBudget.create("review-production", {
        "max_agents": 8,
        "max_provider_calls": 100,
        "max_output_tokens": 131_072,
        "max_seconds": 600,
        "verification_reserve_ratio": 0.15,
        "default_child_share": 0.20,
    })
    budget.register_process(
        "root", parent_id=None, actor_kind="supervisor", max_steps=50,
    )
    budget.reserve_agent()
    lease = budget.register_process(
        "reviewer", parent_id="root", actor_kind="reviewer", max_steps=12,
    )
    assert lease["reserved_provider_calls"] == 10
    assert lease["reserved_output_tokens"] >= 16_384


def test_failed_production_review_cannot_reserve_an_unbounded_replacement():
    budget = TaskTreeBudget.create("review-production", {
        "max_agents": 8,
        "max_provider_calls": 100,
        "max_output_tokens": 131_072,
        "max_seconds": 600,
        "verification_reserve_ratio": 0.15,
        "default_child_share": 0.20,
    })
    budget.register_process(
        "root", parent_id=None, actor_kind="supervisor", max_steps=50,
    )
    budget.reserve_agent()
    first = budget.register_process(
        "reviewer-1", parent_id="root", actor_kind="reviewer", max_steps=12,
    )
    for _ in range(first["reserved_provider_calls"]):
        budget.begin_provider_call("reviewer-1")
    budget.release_process("reviewer-1", status="failed")

    budget.reserve_agent()
    with pytest.raises(TaskBudgetExceeded, match="uncommitted task-tree budget"):
        budget.register_process(
            "reviewer-2", parent_id="root", actor_kind="reviewer", max_steps=12,
        )


def test_streaming_fragment_accounting_matches_cumulative_ascii_estimate():
    budget = _budget()
    for fragment in ["a", "b", "c", "d", "e"]:
        budget.consume_output_fragment(fragment, "root", channel="call-1:text")
    assert budget.snapshot()["used"]["estimated_output_tokens"] == 2
    budget.finish_output_fragments("root", prefix="call-1:")
