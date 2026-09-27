from types import SimpleNamespace


def test_answer_policy_skips_workspace_mutation_checks():
    from backend.core.policy import PolicyEngine
    from backend.core.policy.base import PolicyCheck

    calls: list[str] = []

    class MutationCheck(PolicyCheck):
        name = "mutation"
        applicable_task_kinds = frozenset({"action", "supervisor", "review"})

        def check(self, session, project):
            calls.append("mutation")
            return []

    assert PolicyEngine(checks=[MutationCheck()]).run(
        SimpleNamespace(), SimpleNamespace(), task_kind="answer",
    ) == {}
    assert calls == []


def test_dependency_policy_loads_one_graph_snapshot_for_all_changed_files(
    monkeypatch, tmp_path_factory,
):
    from backend.core.policy.dependency import DependencyChainCheck
    import backend.core.dependency_graph as dependency_graph

    tmp_path = tmp_path_factory
    (tmp_path / "consumer.py").write_text("", encoding="utf-8")
    loads: list[object] = []
    queries: list[str] = []

    class Graph:
        file_fingerprints = {"first.py": "first-hash-long", "second.py": "second-hash-long"}

        def get_dependents(self, path: str):
            queries.append(path)
            return [{"dependent": "consumer.py"}]

    def load_once(workspace, *, allow_stale=False):
        loads.append((workspace, allow_stale))
        return Graph()

    monkeypatch.setattr(dependency_graph, "load_dependency_graph", load_once)
    session = SimpleNamespace(
        workspace_path=str(tmp_path),
        entries=[
            SimpleNamespace(rel_path="first.py", status="modified", workspace_hash="first-hash-long"),
            SimpleNamespace(rel_path="second.py", status="modified", workspace_hash="second-hash-long"),
        ],
    )

    alerts = DependencyChainCheck().check(session, SimpleNamespace())

    assert len(loads) == 1
    assert loads[0][1] is True
    assert queries == ["first.py", "second.py"]
    assert [item["dependent"] for item in alerts] == ["consumer.py"]


def test_dependency_policy_marks_a_stale_snapshot_without_rebuilding(monkeypatch, tmp_path_factory):
    from backend.core.policy.dependency import DependencyChainCheck
    import backend.core.dependency_graph as dependency_graph

    class Graph:
        file_fingerprints = {"changed.py": "old"}

        def get_dependents(self, _path: str):
            return []

    monkeypatch.setattr(
        dependency_graph, "load_dependency_graph",
        lambda _workspace, *, allow_stale=False: Graph(),
    )
    session = SimpleNamespace(
        workspace_path=str(tmp_path_factory),
        entries=[SimpleNamespace(
            rel_path="changed.py", status="modified", workspace_hash="new-value",
        )],
    )

    alerts = DependencyChainCheck().check(session, SimpleNamespace())

    assert alerts == [{
        "rule": "dependency_snapshot_stale",
        "level": "info",
        "message": (
            "Dependency impact uses the last validated snapshot; "
            "changed or new paths will be refreshed at source promotion."
        ),
        "affected_files": ["changed.py"],
        "snapshot_state": "stale_candidate",
    }]
