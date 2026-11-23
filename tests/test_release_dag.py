import threading
import pytest
from backend.core.publish.release_dag import run_dag
from backend.core.publish.release_engine import ReleaseCancelled


def test_independent_release_work_overlaps_and_dependents_receive_complete_results():
    barrier = threading.Barrier(2)
    seen = []
    def execute(step, dependencies):
        if step["id"] in {"a", "b"}:
            barrier.wait(timeout=2)
        else:
            assert dependencies == {"a": "A", "b": "B"}
        seen.append(step["id"])
        return step["id"].upper()
    result = run_dag([{"id":"a"},{"id":"b"},{"id":"c","needs":["a","b"]}], execute, workers=2)
    assert result["c"] == "C" and seen[-1] == "c"


@pytest.mark.parametrize("steps", [
    [{"id":"a","needs":["missing"]}], [{"id":"a"},{"id":"a"}],
    [{"id":"a","needs":["b"]},{"id":"b","needs":["a"]}],
])
def test_invalid_release_graph_never_starts_an_adapter(steps):
    started = []
    with pytest.raises(ValueError): run_dag(steps, lambda *args: started.append(args))
    assert not started


def test_failure_or_cancellation_does_not_start_dependent_work():
    started = []
    def failed(step, _):
        started.append(step["id"])
        raise ValueError("check failed")
    graph = [{"id":"a"},{"id":"dependent","needs":["a"]}]
    with pytest.raises(ValueError): run_dag(graph, failed)
    assert started == ["a"]
    started.clear()
    with pytest.raises(ReleaseCancelled):
        run_dag(graph, lambda *args: started.append(args), cancelled=lambda: True)
    assert not started
