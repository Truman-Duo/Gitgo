"""Lifecycle contracts independent of Gitgo packaging, gh, or a file journal."""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from backend.core.publish.release_engine import ReleaseContext, ReleaseEngine, ReleaseCancelled


class Journal:
    def __init__(self):
        self.records, self.leases = [], 0

    @contextmanager
    def locked(self):
        assert self.leases == 0
        self.leases += 1
        try:
            yield self
        finally:
            self.leases -= 1

    def record(self, name, state, **fields):
        assert self.leases == 1
        value = {"name": name, "state": state, **deepcopy(fields)}
        self.records.append(value)
        return value


class ProjectRecipe:
    identity = {"id": "fixture.other-project", "version": 2}
    supported_phases = ("prepared", "draft", "published", "verified")

    def __init__(self):
        self.calls, self.writes, self.cancelled = [], [], False
        self.fail = False

    def validate(self, context):
        assert context.plan == {"source_tree": "fixture-tree"}

    def prepare(self, context):
        self.calls.append("prepare")
        return b"artifact"

    def guard(self, context, prepared):
        assert prepared == b"artifact"

    def draft(self, context, prepared):
        self.calls.append("draft")
        if "draft" not in self.writes:
            self.writes.append("draft")
        return {"id": "remote-identity"}

    def publish(self, context, prepared, draft):
        self.calls.append("publish")
        if "publish" not in self.writes:
            self.writes.append("publish")
        if self.fail:
            self.fail = False
            raise RuntimeError("secret-shaped diagnostic must not enter phase receipts")

    def verify(self, context, prepared):
        self.calls.append("verify")

    def result(self, phase, draft):
        return {"phase": phase, "remote_id": draft["id"] if draft else None}


def context(recipe):
    return ReleaseContext({"source_tree": "fixture-tree"}, Journal(), object(),
                          cancelled=lambda: recipe.cancelled)


@pytest.mark.parametrize("phase,count", [("prepared", 1), ("draft", 2), ("published", 3), ("verified", 4)])
def test_requested_phase_is_a_boundary_and_preparation_cannot_publish(phase, count):
    recipe, journal = ProjectRecipe(), Journal()
    result = ReleaseEngine(recipe).run(ReleaseContext({"source_tree":"fixture-tree"}, journal, object()), phase)
    assert result["phase"] == phase
    assert recipe.calls == ["prepare", "draft", "publish", "verify"][:count]
    if phase == "prepared":
        assert recipe.writes == []
    assert journal.records[-1]["state"] == "success"
    assert journal.records[-1]["recipe"] == recipe.identity


def test_resume_reconciles_a_committed_effect_without_engine_rollback_or_repetition():
    recipe = ProjectRecipe()
    engine, ctx = ReleaseEngine(recipe), context(recipe)
    recipe.fail = True
    with pytest.raises(RuntimeError):
        engine.run(ctx, "verified")
    assert recipe.writes == ["draft", "publish"]
    assert ctx.evidence.records[-1]["state"] == "failed"
    assert "secret-shaped" not in str(ctx.evidence.records)
    engine.run(ctx, "verified")
    assert recipe.writes == ["draft", "publish"]
    assert recipe.calls[-1] == "verify"


def test_cancellation_after_draft_leaves_the_draft_for_adapter_reconciliation():
    recipe = ProjectRecipe()
    original = recipe.draft
    def draft(*args):
        result = original(*args)
        recipe.cancelled = True
        return result
    recipe.draft = draft
    engine, ctx = ReleaseEngine(recipe), context(recipe)
    with pytest.raises(ReleaseCancelled) as stopped:
        engine.run(ctx, "published")
    assert stopped.value.before_phase == "published"
    assert recipe.writes == ["draft"]
    assert ctx.evidence.records[-1]["state"] == "cancelled"
    recipe.draft, recipe.cancelled = original, False
    engine.run(ctx, "published")
    assert recipe.writes == ["draft", "publish"]


def test_invalid_or_unsupported_requests_do_not_acquire_a_lease_or_execute():
    recipe = ProjectRecipe()
    ctx = context(recipe)
    for phase in ("unknown", None):
        with pytest.raises(ValueError):
            ReleaseEngine(recipe).run(ctx, phase)
    recipe.supported_phases = ("prepared",)
    with pytest.raises(ValueError, match="does not support"):
        ReleaseEngine(recipe).run(ctx, "published")
    assert ctx.evidence.records == [] and recipe.calls == []


def test_a_failed_artifact_guard_cannot_mark_preparation_success_or_publish():
    recipe = ProjectRecipe()
    def changed(*args):
        raise ValueError("artifact changed")
    recipe.guard = changed
    ctx = context(recipe)
    with pytest.raises(ValueError, match="artifact changed"):
        ReleaseEngine(recipe).run(ctx, "published")
    assert recipe.calls == ["prepare"] and recipe.writes == []
    assert not any(row["name"] == "phase-prepared" and row["state"] == "success" for row in ctx.evidence.records)
