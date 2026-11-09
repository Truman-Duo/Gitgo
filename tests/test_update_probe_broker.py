"""Real private IPC, kernel lease passing and owned subprocesses; not UI QA."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.core.process_control import spawn_managed, terminate_tree
from backend.core.process_output import collect_output
from backend.core.update.persistence import descriptor_identity
from backend.core.update.probe_broker import ProbeServer, _frame
from backend.core.update.validation import UpdateError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix inherited descriptor transport")


@pytest.fixture
def broker_lab(tmp_path_factory):
    import fcntl
    root = tmp_path_factory / "installation"
    root.mkdir(mode=0o700)
    control = root / "control"
    control.mkdir(mode=0o700)
    lease = os.open(control / "runtime.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    program = root / "active/internal/gitgo-host/gitgo-host"
    program.parent.mkdir(mode=0o700, parents=True)
    program.write_text("#!/bin/sh\nprintf 'probe-output'\n")
    program.chmod(0o700)
    profile = root / "probe"
    profile.mkdir(mode=0o700)
    store = SimpleNamespace(root=root, registration={"lock_identities": {"runtime.lock": descriptor_identity(lease)}})
    try:
        yield store, lease, program, profile
    finally:
        os.close(lease)


@pytest.mark.parametrize("timeout", [False, True])
def test_forkless_engine_gets_a_separately_owned_probe(broker_lab, timeout):
    store, lease, program, profile = broker_lab
    if timeout:
        program.write_text("#!/bin/sh\nwhile :; do :; done\n")
    source = Path(__file__).resolve().parents[1]
    code = """import pathlib,sys
sys.path.insert(0,sys.argv[1])
from backend.core.installation_lease import retain_descriptor
from backend.core.update.probe_broker import engine_client
from backend.core.update.health import execute_probe,ProbeFailure
with retain_descriptor(int(sys.argv[3])), engine_client(int(sys.argv[2])):
    try:
        result=execute_probe(pathlib.Path(sys.argv[4]),pathlib.Path(sys.argv[5]),'update-data-probe',timeout=1,nonce='a'*32)
    except ProbeFailure as error:
        assert sys.argv[6]=='True' and error.diagnostic['reason']=='collect_timeout',error.diagnostic
        assert error.cleanup['complete'] is True
    else:
        assert sys.argv[6]=='False' and result.output=='probe-output' and result.cleanup['complete'] is True
print('engine-complete')
"""
    # The Mac framework bin/python is itself a spawn trampoline.
    if sys.platform == "darwin":
        from tests.test_macos_forkless_scope import interpreter
        python = interpreter()
    else:
        python = sys.executable
    with ProbeServer(store, seconds=15) as server:
        descriptor = server.child.fileno()
        proc = spawn_managed([python, "-c", code, str(source), str(descriptor), str(lease), str(program), str(profile), str(timeout)],
            pass_fds=(descriptor, lease), macos_forkless=sys.platform == "darwin",
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        server.child.close()
        out, err, _ = collect_output(proc, timeout=15, limit=8192, on_poll=server.poll,
                                    terminate=lambda: terminate_tree(proc))
        assert proc.returncode == 0, (out, err)
        assert out.strip() == "engine-complete"
        assert server.calls == 1 and server.cleanup_complete
        assert proc._gitgo_cleanup["complete"] is True


@pytest.mark.parametrize("change", ["shell", "outside", "bad-lease", "large", "unknown-field"])
def test_broker_denies_invalid_requests_before_any_probe(broker_lab, monkeypatch, change):
    import array
    store, lease, program, profile = broker_lab
    from backend.core.update import health
    monkeypatch.setattr(health, "execute_probe", lambda *_a, **_k: pytest.fail("invalid request executed"))
    request = {"schema_version": 1, "probe": "update-data-probe", "program": str(program),
               "profile": str(profile), "timeout": 1, "nonce": "a" * 32, "plan": None}
    other = None
    if change == "shell":
        request["probe"] = "shell"
    elif change == "outside":
        request["program"] = "/bin/sh"
    elif change == "unknown-field":
        request["environment"] = {"PATH": "/attacker"}
    elif change == "large":
        pass  # An oversized length prefix is rejected before reading a body.
    else:
        other = os.open(store.root / "unrelated.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with ProbeServer(store) as server:
            raw = struct.pack("!I", 65 * 1024) if change == "large" else _frame(request)
            server.child.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                array.array("i", [other if other is not None else lease]))])
            with pytest.raises(UpdateError):
                server.poll()
    finally:
        if other is not None:
            os.close(other)
