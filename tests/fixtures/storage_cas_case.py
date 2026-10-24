"""Actual CAS objects in a disposable directory and finite native worker pool."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.core.sandbox_read import ObjectIdentity
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage import native_windows_cas as n


def denied(action, error=n.StorageCasError):
    try:
        action()
    except error:
        return
    raise AssertionError("Operation unexpectedly admitted")


def main():
    binaries, root, case = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    load = lambda name: ctypes.CDLL(str(binaries / ("gitgo-storage-" + name + ".dll")), winmode=0x100|0x800)
    scopes = WindowsScopeLibrary(load("scope"))
    writers = n.WindowsCasWriterLibrary(load("cas_writer"))
    generations = n.WindowsCasGenerationLibrary(load("cas_generation"))
    scope = scopes.open_scope()
    parent = scope.root(str(root))
    identity = scope.identity(parent)[0]
    objects = []

    def writer():
        value = writers.reserve(); objects.append(value); return value

    def generation(purpose):
        value = generations.reserve(purpose); objects.append(value); return value

    def intent(data):
        return n.CasFileIntent(hashlib.sha256(data).hexdigest(), str(uuid.uuid4()), len(data))

    def fixture(data, *, partial=None):
        planned = intent(data)
        (root/planned.name).write_bytes(data if partial is None else partial)
        handle = scope.open(planned.name, parent)
        record = n.CasFileRecord(planned, scope.identity(handle)[0])
        scope.close_handle(handle)
        return record

    try:
        if case.startswith("roundtrip_"):
            size = int(case.split("_")[1]); data = bytes(i % 251 for i in range(size))
            planned = intent(data); created = writer(); record = created.create(parent, identity, planned)
            assert record.intent is planned and record.identity.volume == identity.volume
            # Production must persist this exact birth ID before materialize.
            assert created.materialize(data) == record
            denied(lambda: created.materialize(data))
            created.close(); created.close()
            reader = generation(n.CasPurpose.READ)
            assert reader.open(parent, identity, record) and reader.read() == data
            denied(reader.delete); reader.close()
            deletion = generation(n.CasPurpose.RETIRE_COMMITTED)
            assert deletion.open(parent, identity, record)
            denied(deletion.delete)
            assert deletion.read() == data
            deletion.delete(); assert deletion.observe_absence()
            denied(deletion.delete); deletion.close()
            observer = generation(n.CasPurpose.READ)
            assert not observer.open(parent, identity, record) and observer.observe_absence()
        elif case == "collision":
            record = fixture(b"original"); created = writer()
            denied(lambda: created.create(parent, identity, record.intent))
            denied(lambda: created.materialize(b"original"))
            created.close(); assert (root/record.intent.name).read_bytes() == b"original"
        elif case == "wrong_parent":
            other = ObjectIdentity("win32", identity.volume, identity.file_id ^ 1)
            planned = intent(b"original"); created = writer()
            denied(lambda: created.create(parent, other, planned))
            created.close(); assert not (root/planned.name).exists()
        elif case == "wrong_identity":
            record = fixture(b"original")
            wrong = n.CasFileRecord(record.intent, ObjectIdentity("win32", record.identity.volume, record.identity.file_id ^ 1))
            deletion = generation(n.CasPurpose.RETIRE_COMMITTED)
            denied(lambda: deletion.open(parent, identity, wrong)); denied(deletion.delete)
            deletion.close(); assert (root/record.intent.name).read_bytes() == b"original"
        elif case in {"partial_0", "partial_3", "partial_wrong"}:
            data = b"complete immutable planned content"
            partial = b"wrong" if case == "partial_wrong" else data[:int(case.split("_")[1])]
            record = fixture(data, partial=partial)
            for purpose in (n.CasPurpose.READ, n.CasPurpose.RETIRE_COMMITTED):
                strict = generation(purpose)
                denied(lambda: strict.open(parent, identity, record)); strict.close()
            deletion = generation(n.CasPurpose.RETIRE_UNPUBLISHED)
            assert deletion.open(parent, identity, record)
            denied(lambda: deletion.read(), ValueError)
            denied(lambda: deletion.read(planned_bytes=partial), ValueError)
            denied(deletion.delete)
            if case == "partial_wrong":
                denied(lambda: deletion.read(planned_bytes=data)); denied(deletion.delete)
                deletion.close(); assert (root/record.intent.name).read_bytes() == partial
            else:
                assert deletion.read(planned_bytes=data) == partial
                deletion.delete(); assert deletion.observe_absence()
        elif case == "wrong_content":
            record = fixture(b"correct")
            (root/record.intent.name).write_bytes(b"changed")
            deletion = generation(n.CasPurpose.RETIRE_COMMITTED)
            assert deletion.open(parent, identity, record)
            denied(deletion.read); denied(deletion.delete)
            deletion.close(); assert (root/record.intent.name).read_bytes() == b"changed"
        elif case == "pinned_reader":
            data = b"original"; record = fixture(data); reader = generation(n.CasPurpose.READ)
            assert reader.open(parent, identity, record)
            with ThreadPoolExecutor(max_workers=1) as pool:
                for action in (reader.read, reader.close):
                    pool.submit(denied, action).result()
            assert reader.read() == data
            deletion = generation(n.CasPurpose.RETIRE_COMMITTED)
            denied(lambda: deletion.open(parent, identity, record)); deletion.close()
            path = root/record.intent.name
            for action in (lambda: path.write_bytes(b"changed"), lambda: path.rename(root/"moved"), path.unlink):
                denied(action, PermissionError)
        elif case == "lifetime":
            for _ in range(8): writer().close()
            denied(writers.reserve)
            for _ in range(32): generation(n.CasPurpose.READ).close()
            denied(lambda: generations.reserve(n.CasPurpose.READ))
            assert not writers.retained_owners and not generations.retained_owners
        else:
            raise AssertionError(case)
    finally:
        for value in reversed(objects): value.close()
        scope.close()
    assert not writers.retained_owners and not generations.retained_owners
    print(case, "passed")


if __name__ == "__main__": main()
