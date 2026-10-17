"""Explicit CAS root-discovery result; partial knowledge cannot authorize GC."""
from __future__ import annotations

from dataclasses import dataclass

from .models import StorageBlocked


class CasInventoryIncomplete(StorageBlocked):
    code = 'STORAGE_CAS_INVENTORY_INCOMPLETE'


@dataclass(frozen=True)
class CasReferenceInventory:
    direct: frozenset[str]
    owned: frozenset[str]
    nested: frozenset[str]
    incomplete: frozenset[str]

    @property
    def complete(self) -> bool:
        return not self.incomplete

    def require_complete(self) -> frozenset[str]:
        if not self.complete:
            raise CasInventoryIncomplete(CasInventoryIncomplete.code)
        return self.direct | self.owned | self.nested
