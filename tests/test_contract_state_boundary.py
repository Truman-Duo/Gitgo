"""Contract facts distinguish absence, unreadable state, and scoped evidence."""
from pathlib import Path
import os
import pytest
from backend.core.contract import ContractManager,ProjectContract,DecidedFeature,ContractStateUnavailable,get_changed_symbols
from backend.core.loop.tool_wrappers import contract_detect_drift


def contract():return ProjectContract(project="p",decided_features=[DecidedFeature(name="feature",location="missing.py")])


def test_default_query_actually_loads_the_project_contract(tmp_path_factory):
    ContractManager.save(tmp_path_factory,contract())
    result=contract_detect_drift({"workspace_path":str(tmp_path_factory),"changed_files":["missing.py"]})
    assert result["count"]==1 and result["drifts"][0]["rule"]=="feature_deleted"
    assert result["contract_state"]=="present"


def test_explicit_contract_filename_has_the_same_workspace_meaning(tmp_path_factory):
    path=ContractManager.save(tmp_path_factory,contract())
    result=contract_detect_drift({"workspace_path":str(tmp_path_factory),"contract_path":str(path),"changed_files":["missing.py"]})
    assert result["count"]==1


def test_absent_and_malformed_contract_are_not_the_same_empty_success(tmp_path_factory):
    absent=contract_detect_drift({"workspace_path":str(tmp_path_factory)})
    assert absent["contract_state"]=="absent" and absent["evaluation_state"]=="not_evaluated"
    meta=tmp_path_factory/".gitgo";meta.mkdir();(meta/"contract.yaml").write_text("{broken",encoding="utf8")
    failed=contract_detect_drift({"workspace_path":str(tmp_path_factory)})
    assert failed["error"]=="CONTRACT_STATE_UNAVAILABLE" and "drifts" not in failed


def test_metadata_hardlinks_are_neither_read_as_trusted_nor_written_through(tmp_path_factory):
    marker=tmp_path_factory/"outside";marker.write_bytes(b"outside synthetic")
    ws=tmp_path_factory/"workspace";meta=ws/".gitgo";meta.mkdir(parents=True);os.link(marker,meta/"contract.yaml")
    with pytest.raises(ContractStateUnavailable):ContractManager.load(ws)
    with pytest.raises(PermissionError):ContractManager.save(ws,contract())
    assert marker.read_bytes()==b"outside synthetic"


def test_stale_contract_cannot_overwrite_a_newer_source(tmp_path_factory):
    from backend.core.storage.metadata_files import MetadataFileChanged
    ContractManager.save(tmp_path_factory,contract())
    left=ContractManager.load(tmp_path_factory);right=ContractManager.load(tmp_path_factory)
    left.project="newer";ContractManager.save(tmp_path_factory,left)
    right.project="stale"
    with pytest.raises(MetadataFileChanged):ContractManager.save(tmp_path_factory,right)
    assert ContractManager.load(tmp_path_factory).project=="newer"


def test_feature_update_preserves_user_gate_extensions(tmp_path_factory):
    import yaml
    meta=tmp_path_factory/".gitgo";meta.mkdir();path=meta/"contract.yaml"
    gates={"sync":{"disabled":["contract_drift"],"config":{"privacy_scan":{"fail_action":"block"}}}}
    path.write_text(yaml.safe_dump({"project":"p","gates":gates}),encoding="utf8")
    ContractManager.update_feature(tmp_path_factory,"p","feature")
    assert yaml.safe_load(path.read_text(encoding="utf8"))["gates"]==gates


def test_blank_optional_yaml_fields_are_explicit_empty_values(tmp_path_factory):
    meta=tmp_path_factory/".gitgo";meta.mkdir();(meta/"contract.yaml").write_text("project: p\ntech_stack: null\ndecided_features:\n- name: feature\n  location: null\n",encoding="utf8")
    loaded=ContractManager.load(tmp_path_factory)
    assert loaded.tech_stack==[] and loaded.decided_features[0].location==""


def test_async_function_addition_is_in_the_declared_name_comparison():
    assert get_changed_symbols(Path("example.py"),"","async def sample():\n    return 1\n")==["sample"]


def test_function_body_change_does_not_pretend_to_be_semantic_impact():
    assert get_changed_symbols(Path("example.py"),"def sample():\n    return 1\n","def sample():\n    return 2\n")==[]


def test_relative_contract_path_is_relative_to_selected_workspace(tmp_path_factory):
    ContractManager.save(tmp_path_factory, contract())
    result = contract_detect_drift({"workspace_path": str(tmp_path_factory), "contract_path": ".gitgo/contract.yaml"})
    assert result["contract_state"] == "present"


@pytest.mark.parametrize("body", ["{broken", "[]", "false", "gates: invalid"])
def test_failed_gate_configuration_blocks_publication_and_keeps_privacy(tmp_path_factory, body):
    from backend.core.policy.gates import load_gates
    meta = tmp_path_factory / ".gitgo"; meta.mkdir()
    (meta / "contract.yaml").write_text(body, encoding="utf8")
    for context in ("sync", "push"):
        gates = load_gates(context, str(tmp_path_factory))
        assert gates[0].name == "contract_configuration"
        assert gates[0].check(None, None, None, []).blocked
        assert any(g.name == "privacy_scan" and g.fail_action == "block" for g in gates)


def test_malformed_contract_keeps_conversation_and_kernel_tools_available(tmp_path_factory):
    from backend.core.policy import PolicyEngine
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.context_builder import _contract_summary
    from types import SimpleNamespace
    meta = tmp_path_factory / ".gitgo"; meta.mkdir()
    (meta / "contract.yaml").write_text("{broken", encoding="utf8")
    engine = PolicyEngine.from_project("p", tmp_path_factory)
    # Contract check is not applicable to conversational tasks.
    check = next(c for c in engine._checks if c.name == "contract_drift")
    assert not check.applies_to("conversation")
    failed = check.check(SimpleNamespace(workspace_path=tmp_path_factory), None)
    assert failed[0]["code"] == "CONTRACT_STATE_UNAVAILABLE"
    assert ToolRegistry.from_contract(tmp_path_factory, ["push"]).is_ring_0("push")
    assert "CONTRACT_STATE_UNAVAILABLE" in _contract_summary("p", tmp_path_factory, [])


@pytest.mark.parametrize("kind", ["escape", "hardlink"])
def test_source_observation_cannot_escape_or_follow_hardlinks(tmp_path_factory, kind):
    outside = tmp_path_factory / "outside.py"; outside.write_text("private synthetic marker", encoding="utf8")
    ws = tmp_path_factory / "workspace"; ws.mkdir()
    if kind == "hardlink":
        os.link(outside, ws / "linked.py"); location = "linked.py"
    else:
        location = "../outside.py"
    item = ProjectContract(decided_features=[DecidedFeature(name="x", location=location, signature="private")])
    ContractManager.save(ws, item)
    result = contract_detect_drift({"workspace_path": str(ws), "changed_files": [location]})
    assert result["error"] == "CONTRACT_STATE_UNAVAILABLE" and "drifts" not in result


def test_supplied_symbol_comparison_distinguishes_invalid_from_empty():
    from backend.core.loop.tool_wrappers import contract_get_changed_symbols as compare
    assert compare({"file_path": "sample.py"})["query_state"] == "failed"
    assert compare({"file_path": "sample.py", "old_content": "", "new_content": "def broken("})["query_state"] == "failed"
    good = compare({"file_path": "nonexistent.py", "old_content": "", "new_content": "async def sample(): pass"})
    assert good["symbols"] == ["sample"] and good["evaluation_state"] == "evaluated_name_sets"


def test_project_bound_services_run_through_original_pipeline(tmp_path_factory):
    from backend.core.tools.contract_service import build_contract_tools
    from backend.core.loop.tool_pipeline import ToolPipeline
    from tests.test_execution_contract import context
    ContractManager.save(tmp_path_factory, contract())
    tools = build_contract_tools(tmp_path_factory)
    result = ToolPipeline().execute({"name": "contract_detect_drift", "args": {}},
                                   tools["contract_detect_drift"], context(tmp_path_factory), "check")
    assert not result.is_error and result.data["count"] == 1
    child = tmp_path_factory / "child"; child.mkdir()
    denied = ToolPipeline().execute({"name": "contract_detect_drift", "args": {}},
                                   tools["contract_detect_drift"], context(child), "child-check")
    assert denied.data["error"] == "GOVERNANCE_WORKSPACE_MISMATCH"


def test_clean_cache_cannot_override_changed_contract_sources(tmp_path_factory):
    from backend.core.policy.gates import ContractDriftGate
    from types import SimpleNamespace
    ContractManager.save(tmp_path_factory, contract())
    gate = ContractDriftGate(); gate._cache = {"dirty": False, "alerts": []}
    result = gate.check(SimpleNamespace(workspace_path=tmp_path_factory), None, None, [])
    assert result.blocked and result.alerts[0]["rule"] == "feature_deleted"


def test_graph_and_privacy_fail_closed_for_linked_sources(tmp_path_factory):
    from backend.core.dependency_graph import build_dependency_graph
    from backend.core.loop.tool_wrappers import privacy_scan
    outside = tmp_path_factory / "outside.py"; outside.write_bytes(b"synthetic private contents")
    ws = tmp_path_factory / "workspace"; ws.mkdir(); os.link(outside, ws / "linked.py")
    with pytest.raises(PermissionError):
        build_dependency_graph(ws)
    result = privacy_scan({"workspace_path": str(ws), "file_list": ["linked.py"]})
    assert result["error"] == "PRIVACY_OBSERVATION_UNAVAILABLE"
    assert b"synthetic private contents" not in str(result).encode()


def test_dependency_cache_hardlink_cannot_be_read_or_replaced(tmp_path_factory):
    from backend.core.dependency_graph import build_dependency_graph
    marker = tmp_path_factory / "marker.json"; marker.write_bytes(b"synthetic outside")
    ws = tmp_path_factory / "workspace"; meta = ws / ".gitgo"; meta.mkdir(parents=True)
    (ws / "safe.py").write_text("def sample(): pass", encoding="utf8")
    os.link(marker, meta / "dependency_graph.v2.json")
    with pytest.raises(PermissionError):
        build_dependency_graph(ws)
    assert marker.read_bytes() == b"synthetic outside"


def test_query_result_identifies_the_actually_observed_contract_version(tmp_path_factory):
    import hashlib
    path = ContractManager.save(tmp_path_factory, contract())
    observed = contract_detect_drift({"workspace_path": str(tmp_path_factory)})
    assert observed["contract_source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    from backend.core.storage.metadata_files import write_metadata_bytes
    write_metadata_bytes(tmp_path_factory, "contract.yaml", b"{synthetic broken")
    failed = contract_detect_drift({"workspace_path": str(tmp_path_factory)})
    assert failed["error"] == "CONTRACT_STATE_UNAVAILABLE"
    assert failed["contract_source_sha256"] != observed["contract_source_sha256"]
