"""Shared bounded dependency scheduling; callers retain execution authority."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from .release_engine import ReleaseCancelled


def validate_dag(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 128:
        raise ValueError("Release needs a bounded nonempty dependency graph")
    names = [step.get("id") for step in steps if isinstance(step, dict)]
    if len(names) != len(steps) or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("Duplicate or missing release step identity")
    known = set(names)
    for step in steps:
        needs = step.get("needs", [])
        if (not isinstance(needs, list) or any(not isinstance(name, str) for name in needs)
                or len(needs) != len(set(needs)) or not set(needs) <= known or step["id"] in needs):
            raise ValueError("Invalid release dependencies")
    complete = set()
    while len(complete) != len(names):
        ready = {step["id"] for step in steps if step["id"] not in complete and set(step.get("needs", [])) <= complete}
        if not ready:
            raise ValueError("Release dependency cycle")
        complete.update(ready)


def run_dag(steps, execute, *, workers=4, cancelled=lambda: False):
    validate_dag(steps)
    if type(workers) is not int or not 1 <= workers <= 16:
        raise ValueError("Invalid release concurrency")
    complete, pending = {}, {}
    remaining = {step["id"]: step for step in steps}
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while remaining or pending:
            if cancelled() and not errors:
                errors.append(ReleaseCancelled("dependency_graph"))
            if not errors:
                ready = [step for step in remaining.values() if set(step.get("needs", [])) <= complete.keys()]
                for step in ready[:workers - len(pending)]:
                    dependencies = {name: complete[name] for name in step.get("needs", [])}
                    pending[pool.submit(execute, step, dependencies)] = step["id"]
                    del remaining[step["id"]]
            if not pending:
                if errors: raise errors[0]
                raise ValueError("Unsatisfied release dependencies")
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                name = pending.pop(future)
                try:
                    complete[name] = future.result()
                except Exception as error:
                    errors.append(error)
            # Running adapters observe the same cancellation source and own
            # their process/transfer cleanup. A Future is never a kill handle.
            if errors and not pending:
                raise errors[0]
    return complete
