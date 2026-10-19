"""Objects repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import uuid
from ..transactions import authoritative_transaction
from ..models import StorageBlocked, StorageReferenceMissing
from ..redaction import redact_for_persistence
from .ports import ObjectsPort
from ..support import _utc_now

def put_state_ref(self: ObjectsPort, namespace: str, key: str, value_ref: str) -> None:
    """Store a small authoritative reference, never a large inline body."""
    self._ensure_open()
    if not namespace or not key or not value_ref:
        raise ValueError("namespace, key and value_ref are required")
    logical = len(namespace) + len(key) + len(value_ref)
    self._reserve_authoritative_write(logical)
    with self._state_lock:
        self._state.execute(
            """INSERT INTO storage_kv(namespace, key, value_ref, updated_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(namespace, key) DO UPDATE SET
                   value_ref=excluded.value_ref,
                   updated_at=excluded.updated_at""",
            (namespace, key, value_ref, _utc_now()),
        )


def get_state_ref(self: ObjectsPort, namespace: str, key: str) -> str | None:
    self._ensure_open()
    with self._state_lock:
        row = self._state.execute(
            "SELECT value_ref FROM storage_kv WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()
    return str(row[0]) if row is not None else None


def _write_blob_file(
    self, data: bytes, *, media_type: str = "application/octet-stream"
) -> dict:
    """Write one immutable file without opening a SQLite transaction."""
    self._ensure_open()
    self._state.require_available()
    if len(data) > self.policy.max_cas_object_bytes:
        raise StorageBlocked("cas_object_too_large")
    digest = hashlib.sha256(data).hexdigest()
    target = self.paths.cas_dir / digest[:2] / digest[2:]
    with self._cas_lock:
        if not target.exists():
            health = self.check_health()
            if health.cas_bytes + len(data) > self.policy.cas_max_bytes:
                raise StorageBlocked("cas_capacity_exceeded")
            if health.total_bytes + len(data) > self.policy.total_max_bytes:
                raise StorageBlocked("project_storage_capacity_exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                with self._health_lock:
                    if self._cas_bytes_hint is not None:
                        self._cas_bytes_hint += len(data)
            finally:
                temporary.unlink(missing_ok=True)
    return {
        "digest": digest,
        "ref": f"sha256:{digest}",
        "byte_length": len(data),
        "media_type": media_type,
        "created_at": _utc_now(),
    }


def _encoded_object(value: object) -> bytes:
    return json.dumps(
        redact_for_persistence(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _register_object(connection: sqlite3.Connection, descriptor: dict) -> None:
    connection.execute(
        """INSERT INTO objects(digest, byte_length, media_type, created_at)
           VALUES(?, ?, ?, ?) ON CONFLICT(digest) DO NOTHING""",
        (
            descriptor["digest"], descriptor["byte_length"],
            descriptor["media_type"], descriptor["created_at"],
        ),
    )


def put_blob(self: ObjectsPort, data: bytes, *, media_type: str = "application/octet-stream") -> str:
    """Write an immutable plaintext CAS object once and return its SHA-256 ref."""
    descriptor = self._write_blob_file(data, media_type=media_type)
    with self._state_lock:
        known = self._state.execute(
            "SELECT 1 FROM objects WHERE digest=?", (descriptor["digest"],)
        ).fetchone()
        if known is None:
            self._reserve_authoritative_write(256)
            self._register_object(self._state, descriptor)
    return str(descriptor["ref"])


def read_blob(self: ObjectsPort, ref: str, *, repair: bool = True) -> bytes:
    self._ensure_open()
    if not ref.startswith("sha256:"):
        raise ValueError("unsupported object reference")
    digest = ref.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("invalid SHA-256 object reference")
    path = self.paths.cas_dir / digest[:2] / digest[2:]
    try:
        if repair:
            return path.read_bytes()
        # Checkpoint lifecycle validation cannot create a missing object or
        # accept corrupt bytes under the old name. Native generation pinning
        # is a separate future CAS protocol requirement.
        with path.open("rb") as handle:
            data = handle.read(self.policy.max_cas_object_bytes + 1)
        if len(data) > self.policy.max_cas_object_bytes:
            raise StorageBlocked("cas_object_too_large")
        if hashlib.sha256(data).hexdigest() != digest:
            raise StorageBlocked("cas_object_digest_mismatch")
        return data
    except FileNotFoundError:
        restored = self._restore_canonical_blob(digest) if repair else None
        if restored is not None:
            return restored
        reason = f"missing:{digest}"
        with self._health_lock:
            self._cas_reference_reasons[digest] = reason
        raise StorageReferenceMissing(ref, str(path)) from None


def _restore_canonical_blob(self: ObjectsPort, digest: str) -> bytes | None:
    """Restore only tiny canonical JSON values proven by their digest.

    Empty split checkpoint components were historically shared by digest
    and could be collected by the old shallow GC.  Their bytes are fully
    determined by the hash, so restoring them is lossless.  Unknown content
    is never guessed.
    """
    candidates = (
        self._encoded_object({}), self._encoded_object([]),
        self._encoded_object(None), self._encoded_object(""),
        self._encoded_object(False), self._encoded_object(True),
    )
    data = next(
        (candidate for candidate in candidates
         if hashlib.sha256(candidate).hexdigest() == digest),
        None,
    )
    if data is None:
        return None
    target = self.paths.cas_dir / digest[:2] / digest[2:]
    with self._cas_lock:
        if target.exists():
            return target.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.repair.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            with self._health_lock:
                if self._cas_bytes_hint is not None:
                    self._cas_bytes_hint += len(data)
                self._cas_reference_reasons.pop(digest, None)
        finally:
            temporary.unlink(missing_ok=True)
    return data


def put_tool_result(
    self,
    content: str,
    *,
    process_id: str = "",
    task_id: str = "",
    tool_name: str = "",
    media_type: str = "application/json",
) -> dict:
    """Persist one oversized tool result as a content-addressed object.

    The locator is stable across retries and duplicate results. Ownership is
    recorded separately so deduplication never erases task/process lineage.
    """
    self._ensure_open()
    encoded = str(content).encode("utf-8")
    descriptor = self._write_blob_file(encoded, media_type=media_type)
    locator = f"tool-result:{descriptor['ref']}"
    now = _utc_now()
    self._reserve_authoritative_write(1024)
    with self._state_lock:
        with authoritative_transaction(self._state):
            self._register_object(self._state, descriptor)
            self._state.execute(
                """INSERT INTO tool_result_objects(
                       locator, content_ref, media_type, byte_length,
                       char_length, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(locator) DO NOTHING""",
                (
                    locator, descriptor["ref"], media_type,
                    len(encoded), len(str(content)), now,
                ),
            )
            self._state.execute(
                """INSERT INTO tool_result_owners(
                       locator, process_id, task_id, tool_name, created_at
                   ) VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(locator, process_id, task_id, tool_name)
                   DO NOTHING""",
                (locator, process_id, task_id, tool_name, now),
            )
    return {
        "locator": locator,
        "digest": descriptor["digest"],
        "media_type": media_type,
        "byte_length": len(encoded),
        "total_chars": len(str(content)),
    }


def read_tool_result(
    self,
    locator: str,
    *,
    offset: int = 0,
    max_chars: int = 24_000,
    json_pointer: str = "",
    query: str = "",
) -> dict:
    """Read a bounded page from an addressable tool result.

    Paging is calculated against the final JSON transport size, preventing
    a page from recursively entering the generic spill path.
    """
    self._ensure_open()
    if not str(locator).startswith("tool-result:sha256:"):
        raise ValueError("locator must be tool-result:sha256:<digest>")
    with self._state_lock:
        row = self._state.execute(
            """SELECT content_ref, media_type, byte_length, char_length
               FROM tool_result_objects WHERE locator=?""",
            (str(locator),),
        ).fetchone()
    if row is None:
        raise KeyError(f"tool result not found: {locator}")
    content = self.read_blob(str(row["content_ref"])).decode("utf-8")
    selected_pointer = str(json_pointer or "").strip()
    if selected_pointer:
        value: object = json.loads(content)
        if selected_pointer != "/":
            if not selected_pointer.startswith("/"):
                raise ValueError("json_pointer must be empty, '/', or start with '/'")
            for raw_part in selected_pointer[1:].split("/"):
                part = raw_part.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    value = value[int(part)]
                elif isinstance(value, dict):
                    value = value[part]
                else:
                    raise ValueError("json_pointer traverses a scalar value")
        content = json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=2,
        )
    needle = str(query or "")
    query_match = None
    if needle:
        found = content.casefold().find(needle.casefold())
        if found < 0:
            return {
                "locator": locator,
                "media_type": str(row["media_type"]),
                "json_pointer": selected_pointer,
                "query": needle,
                "match": None,
                "content": "",
                "offset": 0,
                "next_offset": None,
                "total_chars": len(content),
                "truncated": False,
            }
        query_match = found
        offset = max(0, found - 500)
    start = max(0, int(offset or 0))
    if start > len(content):
        raise ValueError("offset is beyond the tool result")
    requested = max(1_000, min(int(max_chars or 24_000), 100_000))
    end = min(len(content), start + requested)
    while True:
        result = {
            "locator": locator,
            "media_type": str(row["media_type"]),
            "json_pointer": selected_pointer,
            "query": needle,
            "match": query_match,
            "content": content[start:end],
            "offset": start,
            "next_offset": end if end < len(content) else None,
            "total_chars": len(content),
            "truncated": end < len(content),
        }
        if len(json.dumps(result, ensure_ascii=False)) <= 28_000 or end - start <= 1_000:
            result["transport_page_limited"] = end < min(
                len(content), start + requested,
            )
            return result
        end = start + max(1_000, int((end - start) * 0.75))
