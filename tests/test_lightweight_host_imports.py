"""Child transports must not pay for, or initialize, the application facade."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


def isolated(code):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-B", "-c",
        "import sys; sys.path.insert(0, sys.argv.pop(1)); " + code, str(root)],
        cwd=root, capture_output=True, text=True, encoding="utf8", timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_transport_import_does_not_initialize_application_or_providers():
    loaded = json.loads(isolated("import json; import backend.core.native_host_entry; "
        "import backend.core.windows_process_scope; print(json.dumps(sorted(sys.modules)))"))
    assert not set(loaded).intersection({"backend.core.config", "backend.core.sync_session",
        "backend.core.native_host", "backend.core.update.installation", "backend.remote", "httpx"})


def test_facade_exports_keep_the_original_objects_and_submodule_imports():
    isolated("import importlib; import backend; import backend.core; "
        "from backend.core import process_control; "
        "assert process_control is importlib.import_module('backend.core.process_control'); "
        "exec(\"for facade in (backend, backend.core):\\n"
        " for name, module in facade._EXPORTS.items():\\n"
        "  assert getattr(facade, name) is getattr(importlib.import_module(module), name)\\n"
        " assert set(facade.__all__).issubset(dir(facade))\\n"
        " try: facade.unknown_public_export\\n"
        " except AttributeError: pass\\n"
        " else: raise AssertionError('unknown export accepted')\")")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle-only helper dispatch")
@pytest.mark.parametrize("role", ["windows-scope", "windows-job-lease"])
def test_windows_transport_dispatch_does_not_load_update_or_application(role):
    # Invalid bounded handles return before any OS handle is opened. The real
    # owned-Job tests separately exercise launch, cancellation and parent death.
    isolated("from backend.core.native_host_entry import main; "
        f"assert main(['--gitgo-internal-role', {role!r}, 'invalid']) == 2; "
        "assert 'backend.core.update.installation' not in sys.modules; "
        "assert 'backend.core.config' not in sys.modules")


def test_payload_roles_still_enter_installation_admission(monkeypatch):
    from contextlib import contextmanager
    from backend.core import native_host_entry
    from backend.core.update import installation
    calls = []

    @contextmanager
    def admission(role):
        calls.append(("enter", role))
        yield
        calls.append(("exit", role))

    monkeypatch.setattr(installation, "installed_runtime", admission)
    monkeypatch.setattr(native_host_entry, "_main", lambda values: calls.append(("payload", values)) or 0)
    for role in ("python", "daemon", "tool-runner", "unix-scope"):
        values = ["--gitgo-internal-role", role]
        assert native_host_entry.main(values) == 0
        assert calls[-3:] == [("enter", role), ("payload", values), ("exit", role)]
