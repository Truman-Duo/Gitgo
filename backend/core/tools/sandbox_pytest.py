"""Invocation-only adapter on pytest's existing temporary-fixture factory.

Use the fixture override/dependency interface and two public factory methods.
Windows mode-0700 descriptors omit the AppContainer profile SID; directories
here inherit the existing private view's access and retire with that view.
No global Python filesystem operation or pytest class is replaced.
"""
from pathlib import Path
from types import MethodType
import uuid
import pytest


def _getbasetemp(factory):
    if not hasattr(factory, "_gitgo_base"):
        parent = factory._gitgo_workspace / ".gitgo" / "sandbox" / "pytest-tmp"
        parent.mkdir(parents=True, exist_ok=True)
        factory._gitgo_base = parent / ("run-" + uuid.uuid4().hex)
        factory._gitgo_base.mkdir()
        factory._gitgo_counts = {}
    return factory._gitgo_base


def _mktemp(factory, basename, numbered=True):
    base = factory.getbasetemp()
    basename = str(basename)
    if not basename or basename in {".", ".."} or Path(basename).name != basename:
        raise ValueError("temporary fixture basename must be a single relative name")
    if not numbered:
        path = base / basename
        path.mkdir()
        return path
    first = factory._gitgo_counts.get(basename, 0)
    for number in range(first, first + 128):
        path = base / (basename + str(number))
        try:
            path.mkdir()
        except FileExistsError:
            continue
        factory._gitgo_counts[basename] = number + 1
        return path
    raise FileExistsError("temporary fixture name collisions exceeded the allocation bound")


@pytest.fixture(scope="session")
def tmp_path_factory(request, tmp_path_factory):
    # Pytest resolves the same-name dependency to its existing lower-priority
    # fixture. Keep its identity/configuration and other plugin integration.
    factory = tmp_path_factory
    factory._gitgo_workspace = Path(request.config.invocation_params.dir).resolve()
    factory.getbasetemp = MethodType(_getbasetemp, factory)
    factory.mktemp = MethodType(_mktemp, factory)
    return factory
