"""Closed serial checkpoint profile for qualification, never auto-activation."""
import hashlib
from dataclasses import asdict,dataclass
from backend.core.storage.host_records import require
from backend.core.storage.checkpoint_limits import CheckpointLimits
from backend.core.storage.native_windows_sqlite import SqliteFileLimits
from backend.core.storage.envelope import FileEnvelope
from backend.core.storage.runtime_manifest import canonical
from .cycle_ledger import schedule,COUNTS,READ_MODES
from .batch_sql import SCHEMA


@dataclass(frozen=True)
class OperationalProfile:
    version:int=1

    def __post_init__(self):
        require(type(self.version) is int and self.version==1,'closed serial checkpoint profile version')

    @property
    def operations(self):return tuple(mode for mode,_ in COUNTS)+READ_MODES

    @property
    def checkpoint_limits(self):
        # These finite bounds are explicit in the pinned profile. They do not
        # truncate inputs or history; an operation that cannot fit is refused.
        return CheckpointLimits(4096,16,16384,8192,4096,262144,8,262144,64,512,128)

    @property
    def file_limits(self):
        state=schedule()
        return (SqliteFileLimits(state.bound.main_bytes,state.wal_bytes,
                                state.bound.main_bytes,state.shm_bytes),
                SqliteFileLimits(8*1024**2,32*1024**2,8*1024**2,32768))

    def value(self):
        return dict(version=1,operations=self.operations,
                    limits=asdict(self.checkpoint_limits),preparations=1,members=8,generations=32,
                    cas_object_bytes=65536,input_encoding_bytes=16384,
                    state_attempts=COUNTS,wal_bound=asdict(schedule().bound),
                    schema=hashlib.sha256(canonical(SCHEMA)).hexdigest())

    @classmethod
    def from_value(cls,value):
        result=cls()
        require(type(value) is dict and canonical(value)==canonical(result.value()),
                'unsupported operational profile fields or bounds')
        return result

    def cas_file_class(self):return FileEnvelope('cas_generations',65536,32)
