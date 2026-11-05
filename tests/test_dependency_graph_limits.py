"""Ubiquitous lexical words are weak hints, not resolved cross-file calls."""
import json
from backend.core.dependency_graph import build_dependency_graph, DependencyGraph, GRAPH_FILE


def test_high_reference_fanout_is_explicit_and_does_not_create_global_word_edges(tmp_path_factory):
    (tmp_path_factory / "generic.py").write_text("def payload(): pass\n",encoding="utf8")
    for index in range(40):
        (tmp_path_factory / f"user_{index}.py").write_text("# payload\n",encoding="utf8")
    graph = build_dependency_graph(tmp_path_factory)
    assert graph.get_dependents("generic.py") == []
    assert graph.signal_limits["ignored_high_fanout_symbols"] > 0
    restored = DependencyGraph.from_dict(tmp_path_factory, graph.to_dict())
    assert restored.signal_limits == graph.signal_limits


def test_explicit_import_edges_survive_high_lexical_reference_fanout(tmp_path_factory):
    (tmp_path_factory / "important.py").write_text("def domain_operation(): pass\n",encoding="utf8")
    expected = set()
    for index in range(40):
        name = f"consumer_{index}.py"
        expected.add(name)
        (tmp_path_factory / name).write_text("from important import domain_operation\n",encoding="utf8")
    graph = build_dependency_graph(tmp_path_factory)
    assert {row["dependent"] for row in graph.get_dependents("important.py")} == expected
    stored = json.loads((tmp_path_factory / ".gitgo" / GRAPH_FILE).read_text(encoding="utf8"))
    assert DependencyGraph.from_dict(tmp_path_factory, stored).to_dict() == graph.to_dict()
