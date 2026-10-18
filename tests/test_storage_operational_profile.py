"""Explicit profile construction and selected-worker initialization."""
from contextlib import closing,contextmanager
from pathlib import Path
import ctypes,json,os,site,sqlite3,sys,tempfile,uuid
import pytest
from backend.core.storage.project_profile import ProjectProfile
from backend.core.storage.operational_profile import OperationalProfile
from backend.core.storage.project_schema import StorageRuntime,MIGRATIONS,GUARD,schema_digest
from backend.core.storage.batch_sql import SCHEMA
from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.models import StorageBlocked
from tests.test_storage_project_namespace import LIMITS,pristine
from tests.test_storage_project_workers import initialized_runtime,prepare,run
from tests.test_storage_windows_tree import native,private_directory
from scripts import build_storage_runtime as build
REPO=Path(__file__).resolve().parents[1]

def operational_profile():
    schemas=[]
    for role in ('state','observability'):
        with closing(sqlite3.connect(':memory:',isolation_level=None)) as db:
            StorageRuntime._apply_migrations(db,MIGRATIONS[role])
            for sql in (*GUARD,*(SCHEMA if role=='state' else ())):db.execute(sql)
            schemas.append(schema_digest(db))
    operational=OperationalProfile()
    return ProjectProfile(sqlite3.sqlite_version,operational.file_limits,tuple(schemas),operational)

def test_explicit_operational_profile_keeps_old_encoding_and_refuses_drift():
    old=ProjectProfile(sqlite3.sqlite_version,LIMITS,pristine())
    assert json.loads(old.to_bytes())['version']==1
    assert 'operational' not in json.loads(old.to_bytes())
    assert ProjectProfile.from_bytes(old.to_bytes())==old
    current=operational_profile()
    assert len(current.to_bytes())<=8192
    assert ProjectProfile.from_bytes(current.to_bytes())==current
    assert current.sha256!=old.sha256
    assert current.schemas[0]!=old.schemas[0] and current.schemas[1]==old.schemas[1]
    modified=json.loads(current.to_bytes());modified['operational']['generations']=33
    from backend.core.storage.runtime_manifest import canonical
    with pytest.raises(StorageBlocked):ProjectProfile.from_bytes(canonical(modified))

@contextmanager
def operational_runtime_input():
    profile=operational_profile()
    binaries=Path(os.environ['GITGO_STORAGE_NATIVE_TEST_LIBRARY']).resolve(strict=True).parent
    scope=WindowsScopeLibrary(ctypes.CDLL(str(binaries/'gitgo-storage-scope.dll'))).open_scope()
    sid,_=scope.account();scope.close()
    packages=next(Path(path) for path in site.getsitepackages() if (Path(path)/'httpx').is_dir())
    with tempfile.TemporaryDirectory(prefix='gitgo_operational_profile_') as temporary:
        root=Path(temporary)
        output=root/'input';private_directory(output,sid)
        descriptor=root/'profile.json';descriptor.write_bytes(profile.to_bytes())
        manifest=build.build(REPO,Path(sys.base_prefix),packages,binaries,output,profile.sha256,profile_file=descriptor)
        yield manifest,profile

@pytest.fixture(scope='module')
def operational_input():
    # Module fixtures run before function-scoped native fixtures. Keep source
    # regression independent of explicitly opted-in Windows native acceptance.
    if sys.platform!='win32' or not os.environ.get('GITGO_STORAGE_NATIVE_TEST_LIBRARY'):
        pytest.skip('Explicit Windows native storage build required')
    with operational_runtime_input() as result:yield result

def test_selected_runtime_initializes_and_independently_verifies_new_profile(native,tmp_path_factory,operational_input,monkeypatch):
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        project=str(uuid.uuid4())
        first=run(prepare(registry,account,project),account,processes,monkeypatch)
        assert first.phase=='MATERIALIZED'
        second=run(prepare(registry,account,project,first.slot),account,processes,monkeypatch)
        assert second.phase=='READY' and second.record.profile==operational_input[1].sha256


def test_unknown_sqlite_build_cannot_enable_operational_budget(monkeypatch):
    operational=OperationalProfile()
    monkeypatch.setattr(sqlite3,'sqlite_version','99.0.0')
    with pytest.raises(StorageBlocked,match='source qualification'):
        ProjectProfile('99.0.0',operational.file_limits,('a'*64,'b'*64),operational)
