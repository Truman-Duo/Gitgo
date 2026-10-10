"""Host-declared execution authority, separate from business effects.

This declaration is a registration contract, not the effective OS manifest.
Only trusted Host builders create it; tool arguments/JSON cannot supply it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class ExecutionType(str, Enum):
    HOST_COMPUTE = "host_compute"
    DATA_BROKER = "data_broker"
    NATIVE_PROCESS = "native_process"
    EXTERNAL_SERVICE = "external_service"


@dataclass(frozen=True)
class ExecutionContract:
    kind: ExecutionType
    broker: str = ""
    version: int = 1

    def __post_init__(self):
        if not isinstance(self.kind, ExecutionType) or type(self.version) is not int or self.version != 1:
            raise ValueError("Unknown execution contract type/version")
        if self.kind in {ExecutionType.DATA_BROKER, ExecutionType.EXTERNAL_SERVICE}:
            if not isinstance(self.broker, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{2,95}", self.broker):
                raise ValueError("A named Host broker is required")
        elif self.broker:
            raise ValueError("Internal/native execution cannot name a broker")

    def to_dict(self):
        return {"version": self.version, "kind": self.kind.value, "broker": self.broker}


def data_broker(name: str) -> ExecutionContract:
    return ExecutionContract(ExecutionType.DATA_BROKER, name)


HOST_COMPUTE = ExecutionContract(ExecutionType.HOST_COMPUTE)
NATIVE_PROCESS = ExecutionContract(ExecutionType.NATIVE_PROCESS)
