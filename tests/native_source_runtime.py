"""Populate only an owned test runtime from actual import locations."""
import importlib.util
from pathlib import Path
import shutil


def populate_source_runtime(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(__file__).resolve().parents[1] / "backend", root / "backend",
        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Embedded Python may have no sys.prefix/Lib/site-packages at all. Never
    # copy/grant the user's entire installation to compensate for that layout.
    for name in ("httpx", "httpcore", "anyio", "idna", "sniffio", "certifi",
                 "typing_extensions", "h11", "yaml", "_yaml"):
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        if spec.submodule_search_locations:
            shutil.copytree(next(iter(spec.submodule_search_locations)), root / name,
                dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif spec.origin:
            shutil.copy2(spec.origin, root / Path(spec.origin).name)
    return root


import pytest


@pytest.fixture
def workspace_native_launcher(isolated_python, monkeypatch):
    """A real native launch with only an owned runtime receiving access."""
    from backend.core.child_process import tool_runner_command
    runtime = populate_source_runtime(isolated_python)
    command = tool_runner_command(runtime)
    command[0] = str(runtime / "python.exe")
    monkeypatch.setattr("backend.core.loop.process_tool_runner.tool_runner_command", lambda: command)
    monkeypatch.setattr("backend.core.loop.process_tool_runner.owned_child_cwd", lambda _: runtime)
    monkeypatch.setattr("backend.core.sandbox_access.native_runtime_access", lambda: ([runtime], []))
    return runtime
