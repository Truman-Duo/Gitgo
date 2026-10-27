"""Packaging compatibility remains enabled for every application/payload role."""
import builtins
from pathlib import Path
import runpy
import sys
from types import ModuleType

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "packaging" / "pyinstaller-hooks" / "rthooks"


@pytest.mark.parametrize("role", ["windows-scope", "windows-job-lease"])
def test_handle_only_helpers_do_not_import_packaging_or_traitlets(monkeypatch, role):
    original = builtins.__import__
    imported = []

    def record(name, *args, **kwargs):
        if name.startswith(("setuptools", "traitlets", "_distutils_hack")):
            imported.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["gitgo-host.exe", "--gitgo-internal-role", role, "1", "2", "3"])
    monkeypatch.setattr(builtins, "__import__", record)
    for hook in ("gitgo_setuptools.py", "gitgo_traitlets.py"):
        runpy.run_path(str(HOOKS / hook))
    assert imported == []


@pytest.mark.parametrize("role", [None, "python", "daemon", "tool-runner", "unknown"])
@pytest.mark.parametrize("version,preference,shim_expected", [(82, None, True), (58, None, False), (82, "stdlib", False)])
def test_other_roles_keep_packaged_compatibility(monkeypatch, role, version, preference, shim_expected):
    setup, hack, traits, implementation = (ModuleType(name) for name in
        ("setuptools", "_distutils_hack", "traitlets", "traitlets.traitlets"))
    calls = []
    setup.__version__ = str(version) + ".0.0"
    hack.add_shim = lambda: calls.append("shim")
    traits.traitlets = implementation
    original_warning = object()
    implementation._deprecated_method = original_warning
    for module in (setup, hack, traits, implementation):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["gitgo-host.exe", *(["--gitgo-internal-role", role] if role else [])])
    if preference is None:
        monkeypatch.delenv("SETUPTOOLS_USE_DISTUTILS", raising=False)
    else:
        monkeypatch.setenv("SETUPTOOLS_USE_DISTUTILS", preference)
    for hook in ("gitgo_setuptools.py", "gitgo_traitlets.py"):
        runpy.run_path(str(HOOKS / hook))
    assert calls == (["shim"] if shim_expected else [])
    assert implementation._deprecated_method is not original_warning
    assert implementation._deprecated_method(None, None, None, None) is None
