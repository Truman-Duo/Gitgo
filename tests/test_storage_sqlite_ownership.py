"""Lost acknowledgements cannot escape the Python-to-native ownership edge."""
import ctypes
from types import SimpleNamespace

import pytest

from backend.core.storage import native_windows_sqlite as n


class Effects:
    def __init__(self,monkeypatch,mode):
        self.mode=mode
        self.leases=self.opens=self.closes=self.releases=self.destroys=self.unknown=0
        self.library=object.__new__(n.WindowsSqliteLibrary)
        self.library._owners={}
        self.library._extension=self.library._sqlite=SimpleNamespace(_handle=1)
        self.library._calls=dict(create=self.create,lease=self.lease,stats=self.stats,
            seal=lambda cookie:0,destroy=self.destroy)
        effect=self
        class Database:
            def __new__(cls): return object.__new__(cls)
            def __init__(self,*args,**kwargs):
                effect.opens+=1
                if effect.mode=='open':raise RuntimeError('lost initialization result')
            def execute(self,sql):
                self.sql=sql;return self
            def fetchone(self):
                if self.sql=='PRAGMA page_size':return (4096,)
                if self.sql=='PRAGMA page_count':return (0,)
                return (256,)
            def close(self):
                effect.closes+=1;effect.opens-=1
                if effect.mode=='close':raise RuntimeError('lost close result')
        monkeypatch.setattr(n.sqlite3,'Connection',Database)
    def create(self,*args):
        ctypes.cast(args[-1],ctypes.POINTER(ctypes.c_uint64))[0]=257
        if self.mode=='register':raise RuntimeError('lost registration result')
        return 0
    def lease(self,cookie,acquire):
        self.leases+=1 if acquire else -1
        if not acquire:self.releases+=1
        if self.mode==('acquire' if acquire else 'release'):raise RuntimeError('lost lease result')
        return 0
    def stats(self,cookie,out,count):
        for i in range(count):out[i]=0
        out[1],out[2],out[3]=self.opens,self.unknown,self.leases
        return 0
    def destroy(self,cookie):
        self.destroys+=1
        if self.mode=='unregister':raise RuntimeError('lost unregistration result')
        return 0


@pytest.fixture(autouse=True)
def retained_fixture_only():
    assert not n._LIVE
    yield
    # Fake callbacks own no OS resources. Production has no such forgetting API.
    for owner in list(n._LIVE.values()):owner._library._owners.clear()
    n._LIVE.clear()


@pytest.mark.parametrize('mode',['acquire','open'])
def test_connection_intent_precedes_unknown_native_effect(monkeypatch,tmp_path_factory,mode):
    effect=Effects(monkeypatch,mode)
    owner=effect.library.open(tmp_path_factory/'state.sqlite3',n.SqliteFileLimits(1048576,65536,1048576,32768))
    with pytest.raises(RuntimeError):owner.connect()
    assert len(owner._slots)==1 and effect.leases==1
    if mode=='open':assert owner._slots[0]._database is not None
    for _ in range(2):
        with pytest.raises(n.StorageSqliteError,match='retained_connection_debt'):owner.close()
    assert effect.closes==effect.releases==effect.destroys==0
    assert effect.library.retained_owners==1


@pytest.mark.parametrize('mode',['close','release'])
def test_unknown_connection_retirement_never_repeats_effect(monkeypatch,tmp_path_factory,mode):
    effect=Effects(monkeypatch,mode)
    owner=effect.library.open(tmp_path_factory/'state.sqlite3',n.SqliteFileLimits(1048576,65536,1048576,32768))
    owner.connect()
    for _ in range(3):
        with pytest.raises(n.StorageSqliteError,match='retained_connection_debt'):owner.close()
    assert effect.closes==1 and effect.releases==(mode=='release')
    assert effect.destroys==0 and effect.library.retained_owners==1


def test_registration_cookie_remains_owned_when_python_result_is_lost(monkeypatch,tmp_path_factory):
    effect=Effects(monkeypatch,'register')
    with pytest.raises(RuntimeError):
        effect.library.open(tmp_path_factory/'state.sqlite3',n.SqliteFileLimits(1048576,65536,1048576,32768))
    assert len(n._LIVE)==effect.library.retained_owners==1
    owner=next(iter(n._LIVE.values()))
    assert owner._cookie.value==257 and owner._sealed
    owner.close()
    assert not n._LIVE and effect.destroys==1


def test_unknown_unregistration_never_repeats_callback(monkeypatch,tmp_path_factory):
    effect=Effects(monkeypatch,'unregister')
    owner=effect.library.open(tmp_path_factory/'state.sqlite3',n.SqliteFileLimits(1048576,65536,1048576,32768))
    with pytest.raises(RuntimeError):owner.close()
    for _ in range(3):
        with pytest.raises(n.StorageSqliteError,match='unregistration_unknown'):owner.close()
    assert effect.destroys==1 and effect.library.retained_owners==1


def test_deferred_native_files_prevent_owner_retirement(monkeypatch,tmp_path_factory):
    effect=Effects(monkeypatch,'normal')
    owner=effect.library.open(tmp_path_factory/'state.sqlite3',n.SqliteFileLimits(1048576,65536,1048576,32768))
    owner.connect();effect.unknown=1
    with pytest.raises(n.StorageSqliteError,match='retained_connection_debt'):owner.close()
    assert effect.closes==effect.releases==1 and effect.destroys==0


def test_known_close_is_repeatable_without_refunding_another_connection(monkeypatch,tmp_path_factory):
    effect=Effects(monkeypatch,'normal')
    owner=effect.library.open(tmp_path_factory/'state.sqlite3',n.SqliteFileLimits(1048576,65536,1048576,32768))
    first=owner.connect();second=owner.connect()
    first.close();first.close()
    assert effect.closes==effect.releases==1 and effect.leases==1
    assert second.database is not None
    owner.close();owner.close()
    assert effect.closes==effect.releases==2 and effect.destroys==1 and not n._LIVE


@pytest.mark.parametrize('values',[(True,4096,4096,32768),(4096,0,4096,32768),
    (4096,4096,4096,32769),(4096,4096,1<<41,32768)])
def test_limits_require_explicit_bounded_integers(values):
    with pytest.raises(ValueError):n.SqliteFileLimits(*values)
