"""Closed installed-runtime description; a digest is never installation approval.

The trusted installer supplies the expected manifest digest. Content
compatibility, actual installed identity and retained native pins are separate
requirements. This module creates no Host registry or runtime activation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

from backend.core.sandbox_read import ObjectIdentity
from .envelope import FileEnvelope, StorageEnvelope, StorageEnvelopeInvalid
from .models import StorageBlocked

MAX_MANIFEST_BYTES = 65536
MAX_TREE_NODES = 256
MAX_COPY_BYTES = 64 * 1024 * 1024
MANIFEST_NAME = "runtime-manifest.json"


class RuntimeManifestInvalid(StorageBlocked):
    code = "STORAGE_RUNTIME_MANIFEST"


def require(condition, reason):
    if not condition:
        raise RuntimeManifestInvalid(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            "an exact SHA-256 digest is required")


def charge(length):
    require(type(length) is int and 0 <= length <= MAX_COPY_BYTES, "bounded runtime file length required")
    return 1 << max(12, (max(1, length)-1).bit_length())


def windows_component(value):
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{kind}{number}" for kind in ("COM", "LPT") for number in "123456789¹²³")
    require(type(value) is str and 0 < len(value) <= 255 and value not in {".", ".."}
            and not value.endswith((".", " ")) and not any(c in value for c in ':*?"<>|/\\')
            and not any(ord(c) < 32 for c in value) and value.split(".")[0].upper() not in reserved,
            "canonical Windows component required")


def local_path(value):
    require(type(value) is str and 3 < len(value) <= 512 and "\0" not in value,
            "bounded installed runtime root required")
    path = PureWindowsPath(value)
    require(path.is_absolute() and len(path.drive) == 2 and path.drive[1] == ":" and str(path) == value,
            "canonical local Windows runtime root required")
    for component in path.parts[1:]:
        windows_component(component)


@dataclass(frozen=True)
class RuntimeMember:
    path: str
    length: int
    sha256: str

    def __post_init__(self):
        require(type(self.path) is str and 0 < len(self.path) <= 256 and "\\" not in self.path,
                "bounded relative runtime member required")
        path = PurePosixPath(self.path)
        require(not path.is_absolute() and path.as_posix() == self.path and 1 <= len(path.parts) <= 8,
                "canonical runtime member path required")
        for part in path.parts:
            require(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", part) is not None, "invalid runtime member name")
            windows_component(part)
        require(path.parts[0].lower() != MANIFEST_NAME, "manifest path is reserved")
        charge(self.length)
        digest(self.sha256)


@dataclass(frozen=True)
class RuntimeContent:
    python_abi: str
    storage_profile: str
    files: tuple[RuntimeMember, ...]

    def __post_init__(self):
        require(type(self.python_abi) is str and re.fullmatch(r"cpython-[0-9]{2,3}-win_amd64", self.python_abi),
                "qualified Python ABI descriptor required")
        digest(self.storage_profile)
        require(type(self.files) is tuple and 2 <= len(self.files) < MAX_TREE_NODES,
                "bounded immutable runtime member list required")
        require(all(type(item) is RuntimeMember for item in self.files), "exact runtime member records required")
        names = [item.path for item in self.files]
        require(names == sorted(set(names)), "runtime members must be unique and sorted")
        require({"python.exe", "app.zip"} <= set(names), "complete storage worker entry required")
        seen = {}
        for name in [*self.directories, *names]:
            require(name.lower() not in seen, "runtime file/directory case alias or type conflict")
            seen[name.lower()] = name
        require(2+len(self.directories)+len(self.files) <= MAX_TREE_NODES, "runtime installation node ceiling")
        require(sum(charge(item.length) for item in self.files)+MAX_MANIFEST_BYTES <= MAX_COPY_BYTES,
                "runtime copy data ceiling")
        require(len(canonical(self.value())) <= MAX_MANIFEST_BYTES-2048, "runtime descriptor size ceiling")

    @property
    def directories(self):
        return tuple(sorted({str(parent) for item in self.files for parent in PurePosixPath(item.path).parents
                             if str(parent) != "."}, key=lambda name: (name.count("/"), name)))

    def value(self):
        return dict(version=1, python_abi=self.python_abi, storage_profile=self.storage_profile,
                    files=[asdict(item) for item in self.files])

    @property
    def contract(self):
        return hashlib.sha256(canonical(self.value())).hexdigest()

    @classmethod
    def from_value(cls, value):
        require(type(value) is dict and set(value) == {"version", "python_abi", "storage_profile", "files"},
                "closed runtime content fields required")
        require(type(value["version"]) is int and value["version"] == 1, "unknown runtime content version")
        require(type(value["files"]) is list and 2 <= len(value["files"]) < MAX_TREE_NODES,
                "bounded runtime member array required")
        for row in value["files"]:
            require(type(row) is dict and set(row) == {"path", "length", "sha256"}, "closed runtime file fields required")
        return cls(value["python_abi"], value["storage_profile"], tuple(RuntimeMember(**row) for row in value["files"]))


@dataclass(frozen=True)
class RuntimeManifest:
    root: str
    root_identity: ObjectIdentity
    content: RuntimeContent

    def __post_init__(self):
        local_path(self.root)
        identity = self.root_identity
        require(type(identity) is ObjectIdentity and identity.platform == "win32"
                and type(identity.volume) is int and 0 <= identity.volume < 2**64
                and type(identity.file_id) is int and 0 <= identity.file_id < 2**128,
                "exact native runtime identity required")
        require(type(self.content) is RuntimeContent, "closed runtime content required")
        require(len(self.to_bytes()) <= MAX_MANIFEST_BYTES, "bounded runtime manifest required")

    def to_bytes(self):
        return canonical(dict(version=1, root=self.root, root_identity=asdict(self.root_identity), content=self.content.value()))

    @property
    def sha256(self):
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, raw, expected_digest):
        require(type(raw) is bytes and 0 < len(raw) <= MAX_MANIFEST_BYTES, "bounded runtime manifest bytes required")
        digest(expected_digest)
        require(hashlib.sha256(raw).hexdigest() == expected_digest, "approved manifest digest mismatch")
        def pairs(items):
            result = {}
            for key, value in items:
                require(key not in result, "duplicate runtime manifest field")
                result[key] = value
            return result
        def nonfinite(value):
            raise RuntimeManifestInvalid("nonfinite runtime manifest value")
        try:
            value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
        except (ValueError, RecursionError) as error:
            raise RuntimeManifestInvalid("invalid runtime manifest encoding") from error
        require(type(value) is dict and set(value) == {"version", "root", "root_identity", "content"},
                "closed runtime installation fields required")
        require(type(value["version"]) is int and value["version"] == 1, "unknown runtime installation version")
        identity = value["root_identity"]
        require(type(identity) is dict and set(identity) == {"platform", "volume", "file_id"}, "closed runtime identity fields required")
        result = cls(value["root"], ObjectIdentity(**identity), RuntimeContent.from_value(value["content"]))
        require(result.to_bytes() == raw, "noncanonical runtime manifest encoding")
        return result


def with_runtime_copies(envelope: StorageEnvelope, content: RuntimeContent) -> StorageEnvelope:
    """Charge one pinned input and two destination copies in the same envelope.

    This is descriptor arithmetic, not a reservation or OS volume quota. It
    deliberately leaves the Host limit and all project budgets unchanged.
    """
    if type(envelope) is not StorageEnvelope or envelope.allocation_unit != 4096 or type(content) is not RuntimeContent:
        raise StorageEnvelopeInvalid("qualified runtime envelope inputs required")
    if any(item.name.startswith(("runtime_", "input_")) for item in envelope.registry_files):
        raise StorageEnvelopeInvalid("runtime copies already accounted")
    classes = runtime_copy_classes(content)
    return StorageEnvelope(envelope.version, envelope.allocation_unit, envelope.slots, envelope.host_max_bytes,
                           tuple(sorted((*envelope.registry_files, *classes), key=lambda item: item.name)), envelope.files)


def runtime_copy_classes(content: RuntimeContent) -> tuple[FileEnvelope, ...]:
    require(type(content) is RuntimeContent, "closed runtime content required")
    buckets = {}
    for length in (*[item.length for item in content.files], MAX_MANIFEST_BYTES):
        size = charge(length)
        buckets[size] = buckets.get(size, 0)+1
    return tuple(FileEnvelope(f"{prefix}_{size:09}", size, count*copies)
                    for prefix, copies in (("input", 1), ("runtime", 2)) for size, count in sorted(buckets.items()))
