"""Real native installer lifetimes and unknown-result reconciliation.

Only the fixture entry substitutes a temporary account. The opt-in disposable
CI test exercises the production canonical account, command and poll entry.
"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import site
import subprocess
import sys
import tempfile
import time

import pytest

from backend.core.storage.envelope import FileEnvelope, StorageEnvelope
from backend.core.storage.host_records import HostAccount, HostInput, HostLayout, metadata_classes
from backend.core.storage.models import StorageBlocked
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_process import WindowsProcessLibrary
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_installation import PinnedRuntime
from backend.core.storage.runtime_launch import RuntimeInstallation
from backend.core.storage.runtime_manifest import with_runtime_copies
from backend.core.storage.runtime_worker import RuntimeInstallRequest
from scripts.build_storage_runtime import build
from tests.test_storage_runtime_control import reopen, runtime_case
from tests.test_storage_windows_tree import native, private_directory


@pytest.fixture(scope="module")
def packaged_input():
    selected = os.environ.get("GITGO_STORAGE_NATIVE_TEST_LIBRARY")
    if sys.platform != "win32" or not selected:
        pytest.skip("Explicit native Windows runtime build required")
    binaries = Path(selected).resolve(strict=True).parent
    scopes = WindowsScopeLibrary(ctypes.CDLL(str(binaries/"gitgo-storage-scope.dll")))
    scope = scopes.open_scope()
    try:
        sid, _ = scope.account()
    finally:
        scope.close()
    repository = Path(__file__).resolve().parents[1]
    packages = next(Path(path) for path in site.getsitepackages() if (Path(path)/"httpx").is_dir())
    with tempfile.TemporaryDirectory(prefix="gitgo_runtime_build_") as temporary:
        output = Path(temporary).resolve()/"input"
        private_directory(output, sid)
        # Fixture profile and build digest are not production installation
        # approval, nor qualification of the real State/Obs capacity profile.
        yield build(repository, Path(sys.base_prefix), packages, binaries, output, "a"*64)
    assert scopes.retained_scopes == 0


@contextmanager
def launch_case(native, temporary, manifest, *, canonical=False):
    scopes, trees, binaries = native
    journals = WindowsJournalLibrary(ctypes.CDLL(str(binaries/"gitgo-storage-journal.dll")))
    processes = WindowsProcessLibrary(ctypes.CDLL(str(binaries/"gitgo-storage-process.dll")))
    scope = scopes.open_scope()
    try:
        sid, profile = scope.account()
    finally:
        scope.close()
    if not canonical:
        profile = temporary/"profile"
        private_directory(profile, sid)
    account = HostAccount(sid, str(profile))
    source = PinnedRuntime(trees, manifest.to_bytes(), manifest.sha256, sid).pin()
    envelope = StorageEnvelope(1, 4096, 2, 256*1024*1024, metadata_classes(2), (FileEnvelope("state", 65536, 2),))
    layout = HostLayout(with_runtime_copies(envelope, manifest.content), manifest.content)
    registry = WindowsHostRegistry(scopes, trees, journals, layout)
    host = registry.owner()
    try:
        if canonical:
            assert not Path(account.root).exists(), "Never replace an existing canonical Host"
            host.bootstrap(source)
        else:
            host._attempted = True
            parent, identity = host._parent(account, create=True)
            host._bootstrap_at(account, parent, identity, HostInput.from_manifest(manifest))
        control = RuntimeControl(host)
        launch = RuntimeInstallation(control, control.reserve(source))
        yield registry, host, account, source, processes, launch
    finally:
        deadline = time.monotonic()+15
        for process in list(processes._owners.values()):
            while not process.retire():
                assert time.monotonic()<deadline
                time.sleep(.01)
        for owner in list(registry._owners.values()):
            assert owner.close()
        source.close()
        assert not (journals.retained_journals or processes.retained_processes)


FIXTURE_WORKER = '''import ctypes,os,sys
from pathlib import Path
from backend.core.storage.host_records import HostAccount,HostLayout,HOST_LEAF
from backend.core.storage.envelope import StorageEnvelope
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.native_windows_tree import WindowsTreeLibrary,RuntimeInstaller
from backend.core.storage.native_windows_journal import WindowsJournalLibrary
from backend.core.storage.native_windows_process import WindowsProcessLibrary
from backend.core.storage.native_windows_host import WindowsHostRegistry
from backend.core.storage.runtime_manifest import RuntimeManifest
from backend.core.storage.runtime_installation import PinnedRuntime
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_worker import RuntimeInstallRequest,materialize
root=Path(sys.executable).parent
def lib(name,kind):return kind(ctypes.CDLL(str(root/('gitgo-storage-'+name+'.dll')),winmode=0x100|0x800))
scopes=lib('scope',WindowsScopeLibrary);trees=lib('tree',WindowsTreeLibrary)
journals=lib('journal',WindowsJournalLibrary);processes=lib('process',WindowsProcessLibrary)
probe=scopes.open_scope();sid,_=probe.account()
account=HostAccount(sid,FIXTURE_PROFILE)
input_root=probe.root(str(root))
raw=probe.read(probe.open('runtime-manifest.json',input_root),65536)
manifest=RuntimeManifest.from_bytes(raw,APPROVED_INPUT)
host_root=probe.root(account.root)
envelope=StorageEnvelope.from_bytes(probe.read(probe.open('envelope.json',host_root),8192))
registry=WindowsHostRegistry(scopes,trees,journals,HostLayout(envelope,manifest.content))
host=registry.owner();host._attempted=True
parent,parent_id=host._parent(account,create=False)
host._verify(account,parent_id,host._scope.open(HOST_LEAF,parent,directory=True))
probe.close()
source=host._new(PinnedRuntime,trees,raw,APPROVED_INPUT,sid);source.pin()
count=0
original=RuntimeControl._update
def update(self,*args):
    global count
    result=original(self,*args);count+=1
    if count==FAULT_AFTER_UPDATE:os._exit(0)
    return result
RuntimeControl._update=update
if FAULT_AFTER_UPDATE==4:
    begin=RuntimeInstaller.begin
    def lost_begin(self,*args):
        begin(self,*args);os._exit(0)
    RuntimeInstaller.begin=lost_begin
if DIAGNOSTIC_ONLY:
    os.write(1,b'{"phase":"READY","success":true}\\n');os._exit(0)
result=materialize(host,source,processes,RuntimeInstallRequest.from_bytes(REQUEST_BYTES))
os.write(1,b'MATERIALIZED diagnostic only\\n')
assert sys.stdin.buffer.read(1)==b'R'
os._exit(0)
'''


def fixture_entry(monkeypatch, case, *, fault=0, diagnostic=False):
    registry, host, account, source, processes, launch = case
    manifest = source.manifest
    original = processes._calls["start"]
    observed = []
    def start(cookie, executable, command, directory, environment):
        request = RuntimeInstallRequest(launch._anchor.binding, launch._ticket.plan.slot,
            launch._ticket.plan.generation, manifest.sha256, launch._ticket.plan.sha256, launch._event["launch"])
        assert command.value == subprocess.list2cmdline((executable, "-I", "-S", "-B", "-m",
            "backend.core.storage.runtime_worker", request.to_bytes().decode("ascii")))
        code = FIXTURE_WORKER.replace("FIXTURE_PROFILE", repr(account.profile)).replace("APPROVED_INPUT", repr(manifest.sha256))
        code = code.replace("REQUEST_BYTES", repr(request.to_bytes())).replace("FAULT_AFTER_UPDATE", repr(fault))
        code = code.replace("DIAGNOSTIC_ONLY", repr(diagnostic))
        actual = ctypes.create_unicode_buffer(subprocess.list2cmdline((executable,"-I","-S","-B","-c",code)))
        observed.append(request)
        return original(cookie, executable, actual, directory, environment)
    monkeypatch.setitem(processes._calls, "start", start)
    def observer():
        # Retain before attach, including exceptions, as the public method does.
        from backend.core.storage.host_records import HOST_LEAF
        launch._observer = registry.owner()
        owner = launch._observer
        owner._attempted = True
        parent, identity = owner._parent(account, create=False)
        owner._verify(account, identity, owner._scope.open(HOST_LEAF, parent, directory=True))
        return owner
    monkeypatch.setattr(launch, "_new_observer", observer)
    return observed


def finish(launch, *, cancel=False):
    deadline = time.monotonic()+45
    while True:
        result = launch.poll(cancel=cancel)
        if result is not None:
            return result
        assert time.monotonic()<deadline
        time.sleep(.01)


@pytest.mark.parametrize("fault,diagnostic,expected", [
    (0,False,"READY"), (1,False,"RETIRING"), (2,False,"RETIRING"),
    (3,False,"READY"), (4,False,"RETIRING"), (0,True,"RETIRING"),
])
def test_real_worker_committed_facts_and_native_retirement_determine_result(
        native, tmp_path_factory, packaged_input, monkeypatch, fault, diagnostic, expected):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, host, account, source, processes, launch = case
        observed = fixture_entry(monkeypatch, case, fault=fault, diagnostic=diagnostic)
        launch.start(source, processes)
        assert not source.ready and not host._ready
        result = finish(launch)
        assert result.phase == expected and len(observed) == 1
        assert not registry._owners and not processes.retained_processes
        assert finish(launch) is result
        observer = reopen(registry, account)
        value = observer._read_slot(63).head.state.value()
        entry = value["entries"][launch._ticket.plan.slot]
        assert entry["phase"] == expected and entry["worker"]["phase"] == "RETIRED"
        assert value["active"] is None  # Readiness is not activation.
        assert observer.close()
        if expected == "READY":
            assert result.manifest.content == packaged_input.content
            assert result.manifest.sha256 != packaged_input.sha256
        else:
            assert result.manifest is None


def test_lost_completion_acknowledgement_confirms_exact_durability_without_relaunch(
        native, tmp_path_factory, packaged_input, monkeypatch):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, _, _, source, processes, launch = case
        observed = fixture_entry(monkeypatch, case)
        launch.start(source, processes)
        original = registry.journals._calls["update"]
        def lost(*args):
            assert original(*args) == 0
            raise RuntimeError("lost terminal acknowledgement")
        with monkeypatch.context() as patch:
            patch.setitem(registry.journals._calls, "update", lost)
            with pytest.raises(RuntimeError, match="lost terminal"): finish(launch)
        assert launch._result is None and launch._terminal_attempted
        assert registry._owners and registry.journals.retained_journals == 1
        assert finish(launch).phase == "READY" and len(observed) == 1
        assert not registry._owners and not registry.journals.retained_journals


def test_unknown_parent_close_retains_committed_worker_debt(
        native, tmp_path_factory, packaged_input, monkeypatch):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, host, account, source, processes, launch = case
        fixture_entry(monkeypatch, case)
        launch.start(source, processes)
        original = host.close
        with monkeypatch.context() as patch:
            patch.setattr(host, "close", lambda: False)
            deadline = time.monotonic()+45
            while not launch._native_retired:
                assert launch.poll() is None
                assert time.monotonic()<deadline
                time.sleep(.01)
            assert not launch._parent_closed and launch._observer is None
            from backend.core.storage import runtime_records
            entry = runtime_records.FORMAT.read((Path(account.root)/"slot-0063.json").read_bytes()).head.state.value()["entries"][0]
            assert entry["phase"] == "MATERIALIZED" and entry["worker"]["phase"] == "ACCEPTED"
        assert finish(launch).phase == "READY" and not registry._owners


def test_failed_creation_keeps_one_intent_until_native_owner_is_closed(native, tmp_path_factory, monkeypatch):
    with runtime_case(native, tmp_path_factory) as (registry, host, account, source):
        processes = WindowsProcessLibrary(ctypes.CDLL(str(native[2]/"gitgo-storage-process.dll")))
        control = RuntimeControl(host)
        launch = RuntimeInstallation(control, control.reserve(source))
        called = []
        def failed(*args):
            called.append(1)
            raise RuntimeError("unknown create return")
        monkeypatch.setitem(processes._calls, "start", failed)
        with pytest.raises(RuntimeError, match="unknown create"): launch.start(source, processes)
        with pytest.raises(StorageBlocked): launch.start(source, processes)
        assert launch._retire_original(cancel=True)
        observer = reopen(registry, account)
        result = launch._complete(observer)
        assert result.phase == "RETIRING" and len(called) == 1
        assert observer._read_slot(63).head.state.value()["entries"][0]["worker"]["phase"] == "RETIRED"
        assert observer.close() and not processes.retained_processes


@pytest.mark.parametrize("operation", ["start", "resume"])
def test_lost_actual_native_launch_return_never_replays_worker(
        native, tmp_path_factory, packaged_input, monkeypatch, operation):
    with launch_case(native, tmp_path_factory, packaged_input) as case:
        registry, _, account, source, processes, launch = case
        observed = fixture_entry(monkeypatch, case)
        original = processes._calls[operation]
        def lost(*args):
            assert original(*args) == 0
            raise RuntimeError("lost actual native launch")
        with monkeypatch.context() as patch:
            patch.setitem(processes._calls, operation, lost)
            with pytest.raises(RuntimeError, match="lost actual"): launch.start(source, processes)
        with pytest.raises(StorageBlocked): launch.start(source, processes)
        result = finish(launch, cancel=True)
        assert result.phase in {"READY", "RETIRING"} and len(observed) == 1
        observer = reopen(registry, account)
        entry = observer._read_slot(63).head.state.value()["entries"][0]
        assert entry["worker"]["phase"] == "RETIRED"
        if operation == "start":
            assert result.phase == "RETIRING" and entry["worker"]["pid"] == 0
        assert observer.close() and not registry._owners


def test_disposable_ci_public_canonical_runtime_installation(native, tmp_path_factory, packaged_input):
    if os.environ.get("GITGO_STORAGE_CANONICAL_TEST") != "1":
        pytest.skip("Canonical account mutation belongs only to disposable CI acceptance")
    assert os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
    with launch_case(native, tmp_path_factory, packaged_input, canonical=True) as case:
        registry, _, account, source, processes, launch = case
        launch.start(source, processes)
        result = finish(launch)
        assert result.phase == "READY" and result.manifest.content == packaged_input.content
        assert Path(result.manifest.root).parent == Path(account.root)/"runtimes"
        assert not registry._owners and not processes.retained_processes
        observer = registry.owner()
        observer.attach()
        assert observer._read_slot(63).head.state.value()["entries"][0]["phase"] == "READY"
        assert observer.close()
