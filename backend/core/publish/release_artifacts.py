"""Bounded release content composed from the project's existing CAS objects."""
from __future__ import annotations

import hashlib
import io
import json
import re
import time

from .state import PublishError, encoded
from .release_engine import ReleaseCancelled

NAMESPACE = "publish.release.temporary-artifacts"
CHUNK_BYTES = 4 * 1024 * 1024
MAX_RELEASE_BYTES = 2 * 1024**3


class ReleaseArtifacts:
    def __init__(self, storage, owner, *, cancelled=lambda: False):
        if not isinstance(owner, str) or not owner or "/" in owner or len(owner) > 100:
            raise ValueError("Exact release operation owner required")
        self.storage, self.owner, self.cancelled = storage, owner, cancelled
        self.pins = set()
        self.last_cancel_check = 0.

    def _check(self, force=False):
        now = time.monotonic()
        if force or now - self.last_cancel_check >= .2:
            self.last_cancel_check = now
            if self.cancelled(): raise ReleaseCancelled("artifact_io")

    def _pin(self, reference):
        key = self.owner + "/" + reference.removeprefix("sha256:")
        self.storage.put_state_ref(NAMESPACE, key, reference)
        self.pins.add((key, reference))

    def ingest(self, chunks, *, size, sha256=None):
        if type(size) is not int or not 0 <= size <= MAX_RELEASE_BYTES:
            raise PublishError("PUBLISH_RELEASE_ARTIFACT_LIMIT")
        step = min(CHUNK_BYTES, self.storage.policy.max_cas_object_bytes)
        pending, total, content_hash, parts = bytearray(), 0, hashlib.sha256(), []
        def flush(raw):
            reference = self.storage.put_blob(raw)
            self._pin(reference)
            parts.append({"blob_ref": reference, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        for chunk in chunks:
            self._check()
            if not isinstance(chunk, bytes):
                raise PublishError("PUBLISH_RELEASE_ARTIFACT_INVALID")
            if len(chunk) > 65536:
                raise PublishError("PUBLISH_RELEASE_ARTIFACT_CHUNK_LIMIT")
            total += len(chunk)
            if total > size:
                raise PublishError("PUBLISH_RELEASE_ARTIFACT_LIMIT")
            content_hash.update(chunk)
            pending.extend(chunk)
            while len(pending) >= step:
                flush(bytes(pending[:step])); del pending[:step]
        self._check(True)
        if pending: flush(bytes(pending))
        actual = content_hash.hexdigest()
        if total != size or sha256 is not None and actual != sha256:
            raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
        value = {"schema": 1, "size": size, "sha256": actual, "chunks": parts}
        reference = self.storage.put_blob(encoded(value), media_type="application/vnd.gitgo.release-artifact+json")
        self._pin(reference)
        return {"blob_ref": reference, "size": size, "sha256": actual,
                "references": [reference, *(part["blob_ref"] for part in parts)]}

    def manifest(self, descriptor):
        raw = self.storage.read_blob(descriptor["blob_ref"], repair=False)
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema") != 1 or value.get("size") != descriptor["size"]
                or value.get("sha256") != descriptor["sha256"] or not isinstance(value.get("chunks"), list)
                or type(value.get("size")) is not int or not 0 <= value["size"] <= MAX_RELEASE_BYTES
                or not isinstance(value.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", value["sha256"])
                or len(value["chunks"]) > 65536):
            raise PublishError("PUBLISH_RELEASE_ARTIFACT_INVALID")
        for part in value["chunks"]:
            if (not isinstance(part, dict) or type(part.get("size")) is not int
                    or not 0 < part["size"] <= CHUNK_BYTES or not isinstance(part.get("sha256"), str)
                    or not re.fullmatch("[0-9a-f]{64}", part["sha256"])
                    or part.get("blob_ref") != "sha256:" + part["sha256"]):
                raise PublishError("PUBLISH_RELEASE_ARTIFACT_INVALID")
        if sum(part["size"] for part in value["chunks"]) != value["size"]:
            raise PublishError("PUBLISH_RELEASE_ARTIFACT_INVALID")
        return value

    def references(self, descriptor):
        return [{"blob_ref": descriptor["blob_ref"]},
                *({"blob_ref": part["blob_ref"]} for part in self.manifest(descriptor)["chunks"])]

    def bytes(self, descriptor, maximum):
        if descriptor["size"] > maximum:
            raise PublishError("PUBLISH_RELEASE_METADATA_LIMIT")
        return b"".join(self.chunks(descriptor))

    def unpack(self, descriptor):
        """Reuse the updater's directory/type/path guards, writing only CAS."""
        from backend.core.update.archive import _extract_zip, _validate, ExtractionLimits
        limits = ExtractionLimits(entries=128, total_bytes=MAX_RELEASE_BYTES, metadata_bytes=1024**2)
        started = time.monotonic()
        def check():
            self._check()
            if time.monotonic() - started > limits.seconds:
                raise PublishError("PUBLISH_RELEASE_ARTIFACT_TIMEOUT")
        def collect(entries, opener, _destination, limits, checkpoint, *, wrapped):
            _validate(entries, limits, checkpoint, wrapped=wrapped)
            outputs, names = [], set()
            for entry in entries:
                if entry.kind == "directory": continue
                name = entry.name.rsplit("/", 1)[-1]
                if name.casefold() in names:
                    raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
                names.add(name.casefold())
                with opener(entry.source) as source:
                    def chunks():
                        while raw := source.read(65536):
                            checkpoint()
                            yield raw
                    value = self.ingest(chunks(), size=entry.size)
                outputs.append({"name": name, "archive_path": entry.name, **value})
            return outputs
        with self.reader(descriptor) as source:
            return _extract_zip(source, None, limits, check, writer=collect)

    def chunks(self, descriptor):
        value = self.manifest(descriptor)
        content_hash = hashlib.sha256()
        total = 0
        for part in value["chunks"]:
            self._check()
            if part["blob_ref"] != "sha256:" + part["sha256"] or not 0 < part["size"] <= CHUNK_BYTES:
                raise PublishError("PUBLISH_RELEASE_ARTIFACT_INVALID")
            raw = self.storage.read_blob(part["blob_ref"], repair=False)
            if len(raw) != part["size"]:
                raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
            content_hash.update(raw); total += len(raw)
            for offset in range(0, len(raw), 64 * 1024):
                self._check()
                yield raw[offset:offset + 64 * 1024]
        if total != value["size"] or content_hash.hexdigest() != value["sha256"]:
            raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")

    def verify(self, descriptor):
        for _ in self.chunks(descriptor): pass

    def close(self):
        for key, reference in self.pins:
            self.storage.release_state_ref(NAMESPACE, key, expected_ref=reference)
        self.pins.clear()

    @staticmethod
    def release_pins(storage, owner):
        after = ""
        while True:
            rows = storage.list_state_refs(NAMESPACE, prefix=owner + "/", after=after, limit=100)
            for row in rows:
                storage.release_state_ref(NAMESPACE, row["key"], expected_ref=row["value_ref"])
            if len(rows) < 100: return
            after = rows[-1]["key"]

    def reader(self, descriptor):
        return _ArtifactReader(self, self.manifest(descriptor))


class ArtifactContent:
    """Fresh checked stream per guard/upload; Content-Length remains exact."""
    def __init__(self, store, descriptor):
        self.store, self.descriptor = store, descriptor
    def __len__(self): return self.descriptor["size"]
    def __iter__(self): return self.store.chunks(self.descriptor)


class _ArtifactReader(io.RawIOBase):
    def __init__(self, store, manifest):
        self.store, self.manifest, self.position = store, manifest, 0
        self.cached = (-1, b"")
        self.offsets = [0]
        for part in manifest["chunks"]: self.offsets.append(self.offsets[-1] + part["size"])
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        value = offset if whence == 0 else self.position + offset if whence == 1 else self.manifest["size"] + offset
        if whence not in (0, 1, 2) or value < 0: raise ValueError("Invalid artifact seek")
        self.position = value
        return value
    def read(self, size=-1):
        import bisect
        self.store._check()
        size = max(0, self.manifest["size"] - self.position) if size < 0 else size
        output = bytearray()
        while size and self.position < self.manifest["size"]:
            index = bisect.bisect_right(self.offsets, self.position) - 1
            if self.cached[0] != index:
                part = self.manifest["chunks"][index]
                raw = self.store.storage.read_blob(part["blob_ref"], repair=False)
                if len(raw) != part["size"] or hashlib.sha256(raw).hexdigest() != part["sha256"]:
                    raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
                self.cached = index, raw
            offset = self.position - self.offsets[index]
            part = self.cached[1][offset:offset + size]
            output.extend(part); self.position += len(part); size -= len(part)
        return bytes(output)

    def readinto(self, buffer):
        raw = self.read(len(buffer))
        buffer[:len(raw)] = raw
        return len(raw)
