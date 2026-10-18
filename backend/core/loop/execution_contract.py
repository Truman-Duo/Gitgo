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
    file_action: str = ""

    def __post_init__(self):
        if not isinstance(self.kind, ExecutionType) or type(self.version) is not int or self.version != 1:
            raise ValueError("Unknown execution contract type/version")
        if self.file_action not in {"", "read_existing_file"}:
            raise ValueError("Unknown file action")
        if self.file_action and self.kind != ExecutionType.NATIVE_PROCESS:
            raise ValueError("File reads require the native helper")
        if self.kind in {ExecutionType.DATA_BROKER, ExecutionType.EXTERNAL_SERVICE}:
            if not isinstance(self.broker, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{2,95}", self.broker):
                raise ValueError("A named Host broker is required")
        elif self.broker:
            raise ValueError("Internal/native execution cannot name a broker")

    def to_dict(self):
        return {"version": self.version, "kind": self.kind.value, "broker": self.broker, "file_action": self.file_action}


def data_broker(name: str) -> ExecutionContract:
    return ExecutionContract(ExecutionType.DATA_BROKER, name)


HOST_COMPUTE = ExecutionContract(ExecutionType.HOST_COMPUTE)
NATIVE_PROCESS = ExecutionContract(ExecutionType.NATIVE_PROCESS)
NATIVE_FILE_READ = ExecutionContract(ExecutionType.NATIVE_PROCESS, file_action="read_existing_file")


def native_timeout(tool, args):
    import math
    value = float(tool.timeout)
    if tool.timeout_argument:
        requested = float(args.get(tool.timeout_argument) or tool.timeout_default)
        if not math.isfinite(requested):
            raise ValueError("Native timeout must be finite")
        value = min(value, max(1.0, requested) + 5)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Native timeout must be positive and finite")
    return value


@dataclass(frozen=True)
class NativeInvocation:
    """One in-memory Host snapshot, not a grant or a durable launch permit.

    This only describes the existing native boundary. In particular, a read
    handler's object binding does not make arbitrary workspace code read-only.
    No deserializer accepts this object from tool JSON.
    """
    handler: str
    arguments: bytes
    timeout: float
    policy: object
    owner: tuple[str, str]
    registration: str

    @staticmethod
    def encode(args):
        import json
        return json.dumps(args, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode('utf8')

    @classmethod
    def compile(cls, handler, args, timeout, *, owner=('', ''), registration=''):
        import math
        from pathlib import Path
        from backend.core.sandbox import SandboxPolicy
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid native invocation timeout")
        workspace = args.get('_workspace') or args.get('workspace_path')
        if not workspace:
            raise ValueError("Host execution workspace is required")
        policy = SandboxPolicy(Path(str(workspace)), cpu_seconds=max(1, min(int(timeout), 1800)))
        return cls(handler, cls.encode(args), timeout, policy, tuple(owner), registration)

    def check(self, handler, args, timeout):
        if handler != self.handler or self.encode(args) != self.arguments or not 0 < timeout <= self.timeout:
            raise ValueError("Native invocation changed after Host admission")

    @property
    def digest(self):
        import hashlib
        from dataclasses import asdict
        policy = asdict(self.policy)
        policy['workspace'] = str(policy['workspace'])
        return hashlib.sha256(self.encode(dict(handler=self.handler,
            arguments=self.arguments.decode('utf8'), timeout=self.timeout,
            policy=policy, owner=self.owner, registration=self.registration))).hexdigest()
