"""Finite WAL schedule arithmetic, not a reservation or a qualified backend.

A backend may use this bound only for its closed SQLite operation set: fixed
page size and page ceiling, no dirty-page spill/flush, no backup, no savepoints,
and one dirty-page-list WAL commit per transaction. FULL-sync sector padding is
included. Native I/O ceilings remain required even when those conditions hold.

All transactions since the last confirmed reset count, including failed or
uncertain attempts. Remaining cleanup for every admitted debt must be charged
together. A successful business commit does not reclaim either allowance.
"""
from __future__ import annotations

from dataclasses import dataclass

from .envelope import FileEnvelope, MAX_ACCOUNTED_BYTES, StorageEnvelopeInvalid


def _integer(value, maximum=MAX_ACCOUNTED_BYTES, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        raise StorageEnvelopeInvalid("invalid WAL capacity integer")
    return value


@dataclass(frozen=True)
class WalFrameBound:
    """Conditional frame bound; it does not install or attest SQLite settings."""
    page_size: int
    max_pages: int
    max_sector_size: int

    def __post_init__(self):
        _integer(self.page_size, 65536)
        if self.page_size < 512 or self.page_size & (self.page_size-1):
            raise StorageEnvelopeInvalid("invalid SQLite page size")
        # SQLite reserves 0xffffffff and limits the maximum page count below it.
        _integer(self.max_pages, 0xfffffffe)
        _integer(self.max_sector_size, 65536)
        if self.max_sector_size & (self.max_sector_size-1):
            raise StorageEnvelopeInvalid("invalid SQLite sector ceiling")
        _integer(self.main_bytes)
        _integer(self.transaction_bytes)

    @property
    def main_bytes(self):
        return self.page_size*self.max_pages

    @property
    def frame_bytes(self):
        return self.page_size+24

    @property
    def padding_frames(self):
        # The end of the dirty-page list can be one byte past a sector boundary.
        return (self.max_sector_size-1+self.frame_bytes-1)//self.frame_bytes

    @property
    def transaction_frames(self):
        return self.max_pages+self.padding_frames

    @property
    def transaction_bytes(self):
        return self.transaction_frames*self.frame_bytes


@dataclass(frozen=True)
class WalDebt:
    """Maximum still-owed transaction attempts, including all cleanup stages.

    The operation catalogue, durable owner and recovery protocol must establish
    these counts. A caller's claimed count is not evidence of a complete debt.
    """
    operation: str
    copies: int
    business_attempts: int
    cleanup_attempts: int

    def __post_init__(self):
        import re
        if type(self.operation) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.operation) is None:
            raise StorageEnvelopeInvalid("invalid WAL debt operation")
        _integer(self.copies, 65536)
        _integer(self.business_attempts, zero=True)
        _integer(self.cleanup_attempts)
        _integer(self.attempts)

    @property
    def attempts(self):
        return self.copies*(self.business_attempts+self.cleanup_attempts)


@dataclass(frozen=True)
class WalSchedule:
    """One complete unreset WAL epoch, including its already retained bytes.

    The schedule has no advance/refund/reset API. Persisted ownership is the
    authority for attempts and settlement. In particular a busy checkpoint or
    uncertain close must not construct a fresh zero-retained epoch.
    """
    bound: WalFrameBound
    retained_wal_bytes: int
    debts: tuple[WalDebt, ...]
    housekeeping_attempts: int

    def __post_init__(self):
        if type(self.bound) is not WalFrameBound:
            raise StorageEnvelopeInvalid("WAL frame bound required")
        _integer(self.retained_wal_bytes, zero=True)
        if self.retained_wal_bytes and self.retained_wal_bytes < 32:
            raise StorageEnvelopeInvalid("incomplete retained WAL header")
        if type(self.debts) is not tuple or not 1 <= len(self.debts) <= 64:
            raise StorageEnvelopeInvalid("complete immutable WAL debt schedule required")
        if not all(type(debt) is WalDebt for debt in self.debts):
            raise StorageEnvelopeInvalid("invalid WAL debt")
        names = [debt.operation for debt in self.debts]
        if names != sorted(set(names)):
            raise StorageEnvelopeInvalid("WAL debts must be unique and sorted")
        _integer(self.housekeeping_attempts)
        _integer(self.attempts)
        _integer(self.wal_bytes)
        # WAL frame numbers are 32-bit. Refuse arithmetic that would wrap them.
        _integer(self.max_frames, 0xfffffffe)
        _integer(self.shm_bytes)

    @property
    def attempts(self):
        return self.housekeeping_attempts+sum(debt.attempts for debt in self.debts)

    @property
    def wal_bytes(self):
        return max(32,self.retained_wal_bytes)+self.attempts*self.bound.transaction_bytes

    @property
    def max_frames(self):
        # A retained uncommitted/partial tail is charged too, rounded upwards.
        retained = max(0,self.retained_wal_bytes-32)
        frames = (retained+self.bound.frame_bytes-1)//self.bound.frame_bytes
        return frames+self.attempts*self.bound.transaction_frames

    @property
    def shm_bytes(self):
        # WAL-index block 0 has room for 4062 frames; later blocks hold 4096.
        return 32768*(1+(max(0,self.max_frames-4062)+4095)//4096)

    def file_classes(self, prefix: str):
        """Only the main/WAL/SHM classes; the backend must add all other roles.

        This intentionally cannot build a complete StorageEnvelope by itself.
        Journal, control, preparation, CAS, temporary and retained-copy classes
        still need their own qualified limits and aggregate reservation.
        """
        return tuple(FileEnvelope(prefix+'_'+name,size,1) for name,size in (
            ('main',self.bound.main_bytes),('shm',self.shm_bytes),('wal',self.wal_bytes)))
