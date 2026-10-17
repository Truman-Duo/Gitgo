"""Real native storage worker lifecycle and after-effect fault acceptance."""
import ctypes
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import weakref

import pytest

from backend.core.storage.native_windows_process import (
    StorageProcessError, StorageProcessIdentity, StorageWorkerCommand, WindowsProcessLibrary,
)


def close(owner, *, terminate=True):
    deadline = time.monotonic() + 15
    while not owner.retire(terminate=terminate):
        assert time.monotonic() < deadline, "Native worker did not retire"
        time.sleep(.01)


@pytest.fixture
def native():
    if sys.platform != "win32":
        pytest.skip("Windows native storage workers")
    selected = os.environ.get("GITGO_STORAGE_NATIVE_TEST_LIBRARY")
    if not selected:
        pytest.skip("Build native tests with scripts/build_storage_native.py")
    path = Path(selected).with_name("gitgo-storage-process.dll").resolve(strict=True)
    library = WindowsProcessLibrary(ctypes.CDLL(str(path)))
    yield library
    # Preserve failure visibility while always retiring our disposable workers.
    retained = library.retained_processes
    for owner in list(library._owners.values()):
        close(owner)
    assert retained == 0


def command(root, source):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint32
    kernel.GetWindowsDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetWindowsDirectoryW.restype = ctypes.c_uint32
    image, windows = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(32768)
    assert 0 < kernel.GetModuleFileNameW(None, image, len(image)) < len(image)
    assert 0 < kernel.GetWindowsDirectoryW(windows, len(windows)) < len(windows)
    # Disposable test runtime. Production supplies independently pinned code.
    return StorageWorkerCommand((image.value, "-I", "-S", "-B", "-c", source), str(root),
        (("PATH", str(Path(image.value).parent)), ("SYSTEMROOT", windows.value), ("WINDIR", windows.value)))


def report(owner):
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        output.extend(owner.read())
        errors.extend(owner.read(stderr=True))
        assert len(output) <= 4096 and len(errors) <= 4096
        if b"\n" in output:
            line, rest = output.split(b"\n", 1)
            assert not rest
            return json.loads(line)
        assert owner.status().active, errors.decode("utf-8", "replace")
        time.sleep(.01)
    raise AssertionError("No bounded worker report")


def test_native_creation_is_suspended_until_exact_identity_resume(native, tmp_path_factory):
    owner = native.reserve()
    try:
        owner.create(command(tmp_path_factory, "import pathlib,sys; pathlib.Path('created').touch(); sys.stdin.buffer.read(1)"))
        identity, attempted = owner.identity()
        assert not attempted and owner.status().active == 1
        time.sleep(.05)
        assert not (tmp_path_factory / "created").exists()
        owner.resume(identity)
        deadline = time.monotonic() + 10
        while not (tmp_path_factory / "created").exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert owner.identity() == (identity, True)
        owner.release()
        close(owner, terminate=False)
    finally:
        close(owner)


def test_wrong_native_identity_never_resumes_worker(native, tmp_path_factory):
    owner = native.reserve()
    try:
        owner.create(command(tmp_path_factory, "open('must-not-exist','w').close()"))
        identity, _ = owner.identity()
        with pytest.raises(StorageProcessError):
            owner.resume(replace(identity, creation_time=identity.creation_time + 1))
        assert not (tmp_path_factory / "must-not-exist").exists()
        assert owner.identity() == (identity, False)
    finally:
        close(owner)


def test_closed_environment_refuses_parent_injection(native, tmp_path_factory, monkeypatch):
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "HOSTILE_PARENT_VALUE", "__PYVENV_LAUNCHER__"):
        monkeypatch.setenv(key, str(tmp_path_factory / "hostile"))
    owner = native.reserve()
    try:
        owner.create(command(tmp_path_factory, "import os,json,sys; print(json.dumps({'keys':sorted(os.environ),'isolated':sys.flags.isolated,'site':sys.flags.no_site}),flush=True); sys.stdin.buffer.read(1)"))
        owner.resume(owner.identity()[0])
        value = report(owner)
        assert value == {"keys": ["PATH", "SYSTEMROOT", "WINDIR"], "isolated": 1, "site": 1}
        owner.release()
        close(owner, terminate=False)
    finally:
        close(owner)


@pytest.mark.parametrize("mode", ["memory", "process"])
def test_actual_job_enforces_worker_limits(native, tmp_path_factory, mode):
    source = """import json,os,subprocess,sys
denied=False
try:
    if MODE == 'memory':
        data=bytearray(512*1024*1024)
    else:
        subprocess.run([sys.executable,'-I','-S','-B','-c',"open('forbidden-child','w').close()"],check=True)
except (MemoryError,OSError):
    denied=True
print(json.dumps({'denied':denied,'pid':os.getpid()}),flush=True)
sys.stdin.buffer.read(1)
""".replace("MODE", repr(mode))
    owner = native.reserve()
    try:
        owner.create(command(tmp_path_factory, source))
        identity, _ = owner.identity()
        owner.resume(identity)
        value = report(owner)
        assert value == {"denied": True, "pid": identity.pid}
        assert not (tmp_path_factory / "forbidden-child").exists()
        owner.release()
        close(owner, terminate=False)
    finally:
        close(owner)


def test_busy_retirement_seals_but_keeps_owner_and_capacity(native, tmp_path_factory):
    owner = native.reserve()
    try:
        owner.create(command(tmp_path_factory, "import sys; sys.stdin.buffer.read(1)"))
        owner.resume(owner.identity()[0])
        assert not owner.retire(terminate=False)
        assert native.retained_processes == 1
        assert owner.status().active == 1 and owner.status().sealed
        with pytest.raises(StorageProcessError):
            owner.release()
    finally:
        close(owner)


def test_lost_retirement_result_never_terminates_reused_slot(native, tmp_path_factory):
    first = native.reserve()
    assert native._calls["retire"](first._cookie, 0) == 0
    second = native.reserve()
    try:
        second.create(command(tmp_path_factory, "import sys; sys.stdin.buffer.read(1)"))
        identity = second.identity()
        assert first.retire()
        assert second.identity() == identity and second.status().active == 1
    finally:
        close(first)
        close(second)


def test_python_gc_cannot_release_a_live_native_owner(native, tmp_path_factory):
    owner = native.reserve()
    owner.create(command(tmp_path_factory, "import sys; sys.stdin.buffer.read(1)"))
    reference = weakref.ref(owner)
    del owner
    gc.collect()
    try:
        assert reference() is not None and native.retained_processes == 1
        assert reference().status().active == 1
    finally:
        close(reference())


def test_bounded_native_pool_retains_all_admitted_owners(native):
    owners = []
    try:
        owners.extend(native.reserve() for _ in range(8))
        with pytest.raises(StorageProcessError):
            native.reserve()
        assert native.retained_processes == 8
    finally:
        for owner in owners:
            close(owner)


def test_native_after_effect_faults_are_not_replayed(native):
    executable = Path(os.environ["GITGO_STORAGE_NATIVE_TEST_LIBRARY"]).with_name("storage-process-faults.exe")
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native process faults passed" in result.stdout and "quarantined=1" in result.stdout


@pytest.mark.parametrize("pid,created", [(True, 1), (0, 1), (2**32, 1), (1, 0), (1, -1), (1, 2**64), (1, True)])
def test_process_identity_cannot_be_coerced_or_wrapped(pid, created):
    with pytest.raises(ValueError):
        StorageProcessIdentity(pid, created)


@pytest.mark.parametrize("argv,directory,environment", [
    (("python.exe",), r"C:\runtime", (("PATH", r"C:\runtime"), ("SYSTEMROOT", r"C:\Windows"), ("WINDIR", r"C:\Windows"))),
    (("C:\\runtime\\python.exe\0evil",), r"C:\runtime", (("PATH", r"C:\runtime"), ("SYSTEMROOT", r"C:\Windows"), ("WINDIR", r"C:\Windows"))),
    ((r"C:\runtime\python.exe",), "relative", (("PATH", r"C:\runtime"), ("SYSTEMROOT", r"C:\Windows"), ("WINDIR", r"C:\Windows"))),
    ((r"C:\runtime\python.exe",), r"C:\runtime", ()),
    ((r"C:\runtime\python.exe",), r"C:\runtime", (("PATH", r"C:\runtime;C:\hostile"), ("SYSTEMROOT", r"C:\Windows"), ("WINDIR", r"C:\Windows"))),
    ((r"C:\runtime\python.exe",), r"C:\runtime", (("PATH", r"C:\runtime"), ("SYSTEMROOT", r"C:\Windows"), ("WINDIR", r"C:\other"))),
])
def test_implicit_or_injected_launch_inputs_are_refused(argv, directory, environment):
    with pytest.raises(ValueError):
        StorageWorkerCommand(argv, directory, environment)


def test_native_current_context_ignores_environment_and_owns_no_extra_handle(native,tmp_path_factory,monkeypatch):
    for name in ("SYSTEMROOT","WINDIR","PATH"):
        monkeypatch.setenv(name,str(tmp_path_factory/"untrusted"))
    owner=native.reserve()
    try:
        context=owner.current()
        assert context.identity.pid==os.getpid() and context.identity.creation_time>0
        assert Path(context.image).is_file() and Path(context.system_root).is_dir()
        assert str(tmp_path_factory/"untrusted") not in {context.image,context.system_root}
        assert context==owner.current() and owner.status().handles==0
    finally:
        close(owner)


def test_lost_current_context_seals_work_without_leaking_new_handles(native,monkeypatch):
    owner=native.reserve()
    original=native._calls["current"]
    def lost(*args):
        assert original(*args)==0
        raise RuntimeError("lost native current context")
    try:
        with monkeypatch.context() as context:
            context.setitem(native._calls,"current",lost)
            with pytest.raises(RuntimeError,match="lost native current context"):
                owner.current()
        with pytest.raises(StorageProcessError): owner.current()
        assert owner.status().handles==0
    finally:
        close(owner)
