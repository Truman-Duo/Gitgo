"""Disposable test parent; never a production storage entry or path override."""
from pathlib import Path
import ctypes
import json
import os
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.native_windows_tree import WindowsTreeLibrary
from backend.core.storage.runtime_manifest import RuntimeManifest
from tests.test_storage_runtime_launch import launch_case, fixture_entry


def main():
    assert len(sys.argv) == 6
    temporary, manifest_file, digest, binary, mode = sys.argv[1:]
    assert mode in {"complete", "partial", "unknown_identity", "alive_parent"}
    manifest = RuntimeManifest.from_bytes(Path(manifest_file).read_bytes(), digest)
    binaries = Path(binary).parent
    scopes = WindowsScopeLibrary(ctypes.CDLL(str(binary)))
    trees = WindowsTreeLibrary(ctypes.CDLL(str(binaries/"gitgo-storage-tree.dll")))
    with pytest.MonkeyPatch.context() as patch:
        with launch_case((scopes, trees, binaries), Path(temporary), manifest) as case:
            _, _, account, source, processes, launch = case
            fixture_entry(patch, case, fault=2 if mode == "partial" else 0)
            if mode == "unknown_identity":
                original = processes._calls["start"]
                def lost(*args):
                    assert original(*args) == 0
                    raise RuntimeError("lost created identity")
                patch.setitem(processes._calls, "start", lost)
                with pytest.raises(RuntimeError, match="lost created identity"):
                    launch.start(source, processes)
            else:
                launch.start(source, processes)
                deadline = time.monotonic()+45
                output, errors = bytearray(), bytearray()
                while launch._worker.status().active:
                    output.extend(launch._worker.read())
                    errors.extend(launch._worker.read(stderr=True))
                    assert len(output) <= 4096 and len(errors) <= 8192
                    assert time.monotonic() < deadline, errors.decode("utf8", "replace")
                    time.sleep(.01)
                if mode == "alive_parent":
                    while not launch._retire_original(cancel=False):
                        assert time.monotonic() < deadline
                        time.sleep(.01)
            print(json.dumps(dict(profile=account.profile, sid=account.sid,
                generation=launch._ticket.plan.generation, event=launch._event)), flush=True)
            if mode == "alive_parent":
                assert sys.stdin.read(1) == "R"
            # Intentionally bypass every Python finally/close. The production
            # recovery must independently observe actual native process death.
            os._exit(89)


if __name__ == "__main__":
    main()
