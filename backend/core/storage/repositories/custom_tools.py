"""Custom Tools repository; uses the injected writer/transaction boundary."""
from __future__ import annotations
import hashlib
import uuid
from ..transactions import authoritative_transaction
from ..models import StorageBlocked
from .ports import CustomToolsPort
from ..support import _utc_now

def save_custom_tool(
    self, spec: dict, source: str, *, replace: bool = False,
) -> dict:
    """Persist one project-scoped authored tool as an immutable version.

    Persistence and authority are intentionally separate: this catalog
    records an asset, but a process must still explicitly mount it through
    ``author_tool`` and pass its capability gate before it can execute it.
    """
    self._ensure_open()
    name = str(spec.get("name") or "").strip()
    digest = str(spec.get("digest") or "").strip()
    source_digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
    if (not name or len(digest) != 64 or spec.get("execution_mode") not in {
        "authored_pure_python", "authored_privileged_python", "authored_host_python",
    }):
        raise ValueError("invalid authored tool specification")
    if source_digest != str(spec.get("source_sha256") or ""):
        raise ValueError("authored tool source digest does not match its specification")

    # A reusable project asset can outlive the turn that created it.  Apply
    # the same content scanner used at the Git publication boundary before
    # immutable CAS storage, and never return matched material in errors.
    from backend.core.authorship import scan_privacy
    alerts = [
        item for item in scan_privacy(
            f".gitgo/custom-tools/{name}.py", str(source), level=2,
        )
        if str(item.get("level") or "") == "error"
    ]
    if alerts:
        rules = sorted({str(item.get("rule") or "privacy") for item in alerts})
        raise StorageBlocked(
            "CUSTOM_TOOL_PRIVACY_BLOCKED:" + ",".join(rules)
        )

    persistent_spec = {
        key: value for key, value in dict(spec).items()
        if key not in {"_source", "source"}
    }
    source_object = self._write_blob_file(
        str(source).encode("utf-8"), media_type="text/x-python",
    )
    spec_object = self._write_blob_file(
        self._encoded_object(persistent_spec), media_type="application/json",
    )
    now = _utc_now()
    self._reserve_authoritative_write(4096)
    with self._state_lock:
        with authoritative_transaction(self._state):
            row = self._state.execute(
                "SELECT tool_id, state, current_version FROM custom_tools WHERE name=?",
                (name,),
            ).fetchone()
            if row is not None and not replace:
                raise ValueError("CUSTOM_TOOL_EXISTS: use replace to create a new version")
            if row is None and replace:
                raise ValueError("CUSTOM_TOOL_NOT_FOUND: use register to create it")
            for descriptor in (source_object, spec_object):
                self._register_object(self._state, descriptor)
            if row is None:
                tool_id = str(uuid.uuid4())
                version = 1
                self._state.execute(
                    """INSERT INTO custom_tools(
                           tool_id, name, state, current_version, created_at, updated_at
                       ) VALUES(?, ?, 'active', ?, ?, ?)""",
                    (tool_id, name, version, now, now),
                )
            else:
                tool_id = str(row["tool_id"])
                existing = self._state.execute(
                    """SELECT version FROM custom_tool_versions
                        WHERE tool_id=? AND digest=?""",
                    (tool_id, digest),
                ).fetchone()
                if existing is not None:
                    version = int(existing["version"])
                else:
                    version = int(row["current_version"] or 0) + 1
                self._state.execute(
                    """UPDATE custom_tools SET state='active', current_version=?, updated_at=?
                        WHERE tool_id=?""",
                    (version, now, tool_id),
                )
            self._state.execute(
                """INSERT INTO custom_tool_versions(
                       tool_id, version, spec_ref, source_ref, digest, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tool_id, digest) DO NOTHING""",
                (
                    tool_id, version, spec_object["ref"], source_object["ref"],
                    digest, now,
                ),
            )
    return {
        "saved": True, "tool_id": tool_id, "name": name,
        "version": version, "digest": digest, "state": "active",
    }


def list_custom_tools(self: CustomToolsPort, *, include_archived: bool = False) -> dict:
    """Return bounded catalog metadata without exposing stored source."""
    self._ensure_open()
    where = "" if include_archived else "WHERE t.state='active'"
    with self._state_lock:
        rows = self._state.execute(
            f"""SELECT t.tool_id, t.name, t.state, t.current_version,
                        t.created_at, t.updated_at, v.spec_ref, v.digest,
                        (SELECT COUNT(*) FROM custom_tool_versions x
                          WHERE x.tool_id=t.tool_id) AS version_count
                   FROM custom_tools t
                   JOIN custom_tool_versions v ON v.tool_id=t.tool_id
                        AND v.version=t.current_version
                   {where}
                  ORDER BY t.name COLLATE NOCASE
                  LIMIT 500"""
        ).fetchall()
    tools = []
    for row in rows:
        spec = self._read_json_blob(str(row["spec_ref"]))
        spec = spec if isinstance(spec, dict) else {}
        tools.append({
            "tool_id": str(row["tool_id"]), "name": str(row["name"]),
            "description": str(spec.get("description") or ""),
            "state": str(row["state"]),
            "version": int(row["current_version"]),
            "version_count": int(row["version_count"]),
            "digest": str(row["digest"]),
            "updated_at": str(row["updated_at"]),
            "authority": (
                "user_approved_privileged"
                if spec.get("execution_mode") == "authored_privileged_python"
                else "pure_transform_only"
            ),
            "effect": str(spec.get("effect") or "read"),
            "resources": list(spec.get("resources") or []),
        })
    return {"tools": tools, "count": len(tools)}


def load_custom_tool(self: CustomToolsPort, name: str, *, version: int | None = None) -> dict:
    """Load a saved version for an explicit task-local mount."""
    self._ensure_open()
    with self._state_lock:
        tool = self._state.execute(
            "SELECT tool_id, state, current_version FROM custom_tools WHERE name=?",
            (str(name),),
        ).fetchone()
        if tool is None:
            raise KeyError(f"CUSTOM_TOOL_NOT_FOUND:{name}")
        if str(tool["state"]) != "active":
            raise ValueError(f"CUSTOM_TOOL_ARCHIVED:{name}")
        selected = int(version or tool["current_version"])
        row = self._state.execute(
            """SELECT spec_ref, source_ref, digest FROM custom_tool_versions
                WHERE tool_id=? AND version=?""",
            (str(tool["tool_id"]), selected),
        ).fetchone()
    if row is None:
        raise KeyError(f"CUSTOM_TOOL_VERSION_NOT_FOUND:{name}@{selected}")
    spec = self._read_json_blob(str(row["spec_ref"]))
    if not isinstance(spec, dict):
        raise RuntimeError("CUSTOM_TOOL_SPEC_INVALID")
    source = self.read_blob(str(row["source_ref"])).decode("utf-8")
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != spec.get("source_sha256"):
        raise RuntimeError("CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED")
    return {
        "tool_id": str(tool["tool_id"]), "name": str(name),
        "version": selected, "digest": str(row["digest"]),
        "source_ref": str(row["source_ref"]), "spec": spec,
        "source": source,
    }


def set_custom_tool_archived(self: CustomToolsPort, name: str, archived: bool = True) -> dict:
    """Archive or restore an asset; immutable versions remain recoverable."""
    self._ensure_open()
    state = "archived" if archived else "active"
    now = _utc_now()
    self._reserve_authoritative_write(512)
    with self._state_lock:
        cursor = self._state.execute(
            "UPDATE custom_tools SET state=?, updated_at=? WHERE name=?",
            (state, now, str(name)),
        )
    if cursor.rowcount != 1:
        raise KeyError(f"CUSTOM_TOOL_NOT_FOUND:{name}")
    return {"name": str(name), "state": state, "archived": bool(archived)}
