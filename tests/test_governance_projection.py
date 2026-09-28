from dataclasses import dataclass
from pathlib import Path

from backend.core.loop.governance_projection import GovernanceProjection
from backend.core.loop.signal_normalizer import SignalNormalizer
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.context_store import ContextObjectStore


def test_stable_signal_identity_includes_severity_and_deduplicates():
    normalizer = SignalNormalizer()
    data = {"identity_integrity": [{"message": "missing identity", "level": "warning"}]}
    one = normalizer.normalize(policy_results=data)
    two = normalizer.normalize(policy_results=data)
    assert one[0].signal_id == two[0].signal_id
    data["identity_integrity"].append(dict(data["identity_integrity"][0]))
    assert len(normalizer.normalize(policy_results=data)) == 1
    data["identity_integrity"] = [{"message": "missing identity", "level": "error"}]
    assert normalizer.normalize(policy_results=data)[0].signal_id != one[0].signal_id


def test_history_dataclass_and_wire_have_same_rejection_path():
    @dataclass
    class Entry:
        correlation_id: str
        detail: dict
    event = Entry("r1", {"instruction": "check artifact"})
    normalizer = SignalNormalizer()
    one = normalizer.normalize(rejections=[event])
    two = normalizer.normalize(rejections=[{"correlation_id": "r1", "detail": event.detail}])
    assert one[0].signal_id == two[0].signal_id
    event.detail["resolved"] = True
    assert normalizer.normalize(rejections=[event]) == []


def test_empty_snapshot_clears_old_brief_without_claiming_repair():
    signals = SignalNormalizer().normalize(policy_results={"identity_integrity": [{"message": "old missing identity"}]})
    old = GovernanceProjection.compose({}, signals, base_brief="current project contract")
    new = GovernanceProjection.compose(old, [], base_brief="current project contract")
    assert new["signals"] == []
    assert "old missing identity" not in new["brief"]
    assert signals[0].signal_id in new["removed_governance_signal_ids"]
    assert "not proof of a successful repair" in new["brief"]


def test_publish_once_and_do_not_add_parallel_mailbox_messages(tmp_path_factory: Path):
    process = AgentProcess(process_id="p", role="worker", ring_level=RingLevel.RING_3, context_snapshot={
        "workspace_path": str(tmp_path_factory), "task_contract": {"task_id": "keep"},
    })
    assert GovernanceProjection.publish(process, [], base_brief="contract")
    version = process.context_version
    assert not GovernanceProjection.publish(process, [], base_brief="contract")
    assert process.context_version == version
    assert process.read_context_snapshot()[0]["task_contract"] == {"task_id": "keep"}


def test_unchanged_context_ref_is_not_rewritten(tmp_path_factory: Path, monkeypatch):
    store = ContextObjectStore(tmp_path_factory)
    store.put("governance/active", {"signals": []})
    def unexpected(*_args, **_kwargs):
        raise AssertionError("unchanged context should not write")
    monkeypatch.setattr(store, "_atomic_write", unexpected)
    store.put("governance/active", {"signals": []})


def test_publication_preserves_concurrent_task_contract(tmp_path_factory: Path, monkeypatch):
    process = AgentProcess(process_id="p", role="worker", ring_level=RingLevel.RING_3,
                           context_snapshot={"workspace_path": str(tmp_path_factory)})
    real_put = ContextObjectStore.put
    def interleaved(store, *args, **kwargs):
        process.update_context_snapshot(lambda current: {**current, "task_contract": {"task_id": "new"}})
        return real_put(store, *args, **kwargs)
    monkeypatch.setattr(ContextObjectStore, "put", interleaved)
    GovernanceProjection.publish(process, [])
    assert process.read_context_snapshot()[0]["task_contract"]["task_id"] == "new"


def test_history_is_retained_for_recheck_not_promoted_to_a_current_block(tmp_path_factory):
    from backend.core.loop.evidence_applicability import EvidenceApplicability
    from backend.core.loop.signals import GovernanceSignal, SignalSeverity, SignalCategory
    old = GovernanceSignal(source="rejection", rule="worker profile not available",
                           severity=SignalSeverity.CRITICAL, category=SignalCategory.BLOCK)
    signals = EvidenceApplicability.annotate([old], kind="history", project_name="p",
                                             workspace_path=str(tmp_path_factory))
    projection = GovernanceProjection.compose({"project_name": "p", "workspace_path": str(tmp_path_factory)}, signals)
    assert projection["signals"] == []
    assert projection["evidence_candidates"][0]["metadata"]["applicability"]["state"] == "historical"
    assert "worker profile not available" not in projection["brief"]
    assert old.rule == "worker profile not available"


def test_stale_catalog_lesson_is_retrieval_only_not_an_active_signal():
    from backend.core.loop.signals import GovernanceSignal, SignalSeverity, SignalCategory

    lesson = GovernanceSignal(
        source="lesson_trigger",
        severity=SignalSeverity.MEDIUM,
        category=SignalCategory.SUGGEST,
        rule="rerun the old workflow",
        metadata={
            "enforcement_authority": False,
            "applicability": {
                "state": "needs_recheck",
                "reason": "source_file_changed",
                "path": "acceptance.txt",
            },
        },
    )

    projection = GovernanceProjection.compose({}, [lesson])

    assert projection["signals"] == []
    assert projection["evidence_candidates"][0]["metadata"]["applicability"] == {
        "state": "needs_recheck",
        "reason": "source_file_changed",
        "path": "acceptance.txt",
    }
    assert "rerun the old workflow" not in projection["brief"]


def test_scope_and_source_versions_never_imply_a_repair():
    from backend.core.loop.evidence_applicability import EvidenceApplicability
    evidence = {"kind": "observation", "scope": {"project_name": "p"},
                "depends_on": {"task_contract": "old"}}
    assert EvidenceApplicability.assess(evidence, {"project_name": "other"})["state"] == "out_of_scope"
    context = {"project_name": "p", "context_refs": {"task_contract": {"digest": "new"}}}
    changed = EvidenceApplicability.assess(evidence, context)
    assert changed["state"] == "needs_recheck"
    assert changed["reason"] == "source_version_changed"
    context["context_refs"]["task_contract"]["digest"] = "old"
    assert EvidenceApplicability.assess(evidence, context)["state"] == "current"


def test_transitive_source_change_invalidates_unchanged_producer_version():
    from backend.core.loop.evidence_applicability import EvidenceApplicability
    evidence = {"kind": "observation", "depends_on": {"policy_snapshot": "policy-v1"}}
    context = {"evidence_sources": {
        "policy_snapshot": {
            "digest": "policy-v1", "producer": "PolicyEngine.run",
            "depends_on": {"workspace_snapshot": "workspace-v1"},
        },
        "workspace_snapshot": {"digest": "workspace-v2", "producer": "SyncSession.step_scan"},
    }}
    result = EvidenceApplicability.assess(evidence, context)
    assert result["state"] == "needs_recheck"
    assert result["reason"] == "source_version_changed"
    assert result["dependency_path"] == ["policy_snapshot", "workspace_snapshot"]


def test_evidence_dependency_cycle_fails_closed_instead_of_recursing():
    from backend.core.loop.evidence_applicability import EvidenceApplicability
    evidence = {"kind": "observation", "depends_on": {"one": "1"}}
    context = {"evidence_sources": {
        "one": {"digest": "1", "depends_on": {"two": "2"}},
        "two": {"digest": "2", "depends_on": {"one": "1"}},
    }}
    result = EvidenceApplicability.assess(evidence, context)
    assert result["state"] == "needs_recheck"
    assert result["reason"] == "dependency_cycle"


def test_current_policy_observations_are_bound_to_host_source_graph(tmp_path_factory):
    from backend.core.loop.context_builder import build_governance_context
    context = build_governance_context(
        "p", tmp_path_factory,
        current_policy_results={"identity_integrity": [{"message": "missing", "level": "warning"}]},
        source_snapshot={"entries": [{"path": "identity.md", "workspace_hash": "v1"}]},
    )
    evidence = context["signals"][0].metadata["evidence"]
    assert evidence["depends_on"] == {
        "policy_snapshot": context["evidence_sources"]["policy_snapshot"]["digest"],
    }
    assert context["evidence_graph"]["policy_snapshot"]["depends_on"]["workspace_snapshot"]


def test_seeded_context_refs_publish_their_producer_dependencies(tmp_path_factory):
    from backend.core.loop.context_store import seed_context_objects
    context = seed_context_objects(str(tmp_path_factory), {
        "task_contract": {"goal": "test"}, "lessons": [], "signals": [],
        "evidence_sources": {"policy_snapshot": {"digest": "policy-v1"}},
    })
    refs = context["context_refs"]
    assert refs["task_contract"]["producer"] == "task_contract_admission"
    assert refs["governance_active"]["depends_on"] == {
        "task_contract": refs["task_contract"]["digest"],
        "project_lessons": refs["project_lessons"]["digest"],
        "policy_snapshot": "policy-v1",
    }


def test_seeded_context_normalizes_legacy_unpaired_surrogates(tmp_path_factory):
    from backend.core.loop.context_store import seed_context_objects

    context = seed_context_objects(str(tmp_path_factory), {
        "task_contract": {"goal": "中文\udc80tail"},
        "lessons": [], "signals": [],
    })

    resolved = ContextObjectStore(tmp_path_factory).resolve(
        context["context_refs"]["task_contract"]["latest"],
    )
    assert "中文" in resolved.content
    assert "\\udc80tail" in resolved.content
    assert resolved.content.encode("utf-8")


def test_live_publication_replaces_project_lessons_object(tmp_path_factory):
    process = AgentProcess(
        process_id="p", role="worker", ring_level=RingLevel.RING_3,
        context_snapshot={"workspace_path": str(tmp_path_factory)},
    )
    first = [{"id": "L1", "rule": "first", "applicability": {"state": "current"}}]
    second = [{"id": "L2", "rule": "second", "applicability": {"state": "current"}}]
    assert GovernanceProjection.publish(process, [], lessons=first)
    old = process.read_context_snapshot()[0]["context_refs"]["project_lessons"]
    assert GovernanceProjection.publish(process, [], lessons=second)
    context = process.read_context_snapshot()[0]
    new = context["context_refs"]["project_lessons"]
    assert new["digest"] != old["digest"]
    resolved = ContextObjectStore(tmp_path_factory).resolve(new["latest"])
    assert '"id":"L2"' in resolved.content
    assert '"id":"L1"' not in resolved.content


def test_capability_shortcut_uses_host_registry():
    from backend.core.loop.decision_support import collect_decision_evidence
    process = AgentProcess(process_id="p", role="supervisor", ring_level=RingLevel.RING_0,
                           capability_profile_id="supervisor.control")
    evidence = collect_decision_evidence(process, "capabilities")
    assert evidence["capabilities"]["current_profile_id"] == "supervisor.control"
    assert any(item["profile_id"] == "development.workspace"
               for item in evidence["capabilities"]["worker_profiles"])
