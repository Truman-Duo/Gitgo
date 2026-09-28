"""Content-addressed context objects with mutable latest refs and session memo."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from backend.core.loop.agent_tool import AgentTool, ToolEffect
from backend.core.unicode_safety import normalize_unicode_text, normalize_unicode_value


_REF_RE = re.compile(r"^context:([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.\/-]+)(?:@(.+))?$")
_TRANSPORT_SAFE_JSON_CHARS = 28_000


def _fit_transport_page(
    content: str,
    *,
    offset: int,
    max_chars: int,
    build_result,
) -> dict:
    """Fit a content page below generic ToolPipeline externalization.

    The outer pipeline JSON-encodes tool results. Quotes, backslashes and
    newlines can therefore make a nominal 24K page exceed its 32K limit and
    recursively turn every artifact_read into another artifact. Size the final
    serialized object, not the raw source slice.
    """
    end = min(len(content), offset + max_chars)
    while True:
        result = build_result(end)
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        if len(encoded) <= _TRANSPORT_SAFE_JSON_CHARS or end - offset <= 1000:
            result["transport_page_limited"] = end < min(
                len(content), offset + max_chars
            )
            return result
        span = max(1000, int((end - offset) * 0.75))
        end = offset + span


def _json_default(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    return str(value)


@dataclass(frozen=True)
class ResolvedContextObject:
    ref: str
    logical_name: str
    digest: str
    media_type: str
    content: str
    metadata: dict


class ContextObjectStore:
    DIRECTORY = ".gitgo/context_objects"
    _refs_lock = threading.RLock()

    def __init__(self, workspace_path: str | Path):
        self.workspace = Path(workspace_path).resolve()
        self.root = self.workspace / self.DIRECTORY
        self.blobs = self.root / "blobs"
        self.refs_dir = self.root / "refs"
        self.refs_path = self.root / "refs.json"

    def put(
        self, logical_name: str, value: Any, *, media_type: str = "application/json",
        metadata: dict | None = None,
    ) -> dict:
        name = self._validate_name(logical_name)
        object_metadata = normalize_unicode_value(dict(metadata or {}))
        if media_type == "application/json":
            content = json.dumps(
                normalize_unicode_value(value),
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                default=_json_default,
            )
        else:
            content = normalize_unicode_text(str(value))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.blobs.mkdir(parents=True, exist_ok=True)
        blob_path = self.blobs / f"{digest}.json"
        if not blob_path.exists():
            self._atomic_write(blob_path, {
                "digest": digest,
                "media_type": media_type,
                "content": content,
                "metadata": object_metadata,
            })
        # One atomic file per logical ref avoids lost read-modify-write updates
        # even when Native Host, daemon watcher, and another harness overlap.
        with self._refs_lock:
            ref_path = self.refs_dir / f"{name}.json"
            ref_value = {"digest": digest, "media_type": media_type}
            if object_metadata.get("producer"):
                ref_value["producer"] = str(object_metadata["producer"])
            if object_metadata.get("depends_on"):
                ref_value["depends_on"] = dict(object_metadata["depends_on"])
            try:
                previous = json.loads(ref_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                previous = None
            if previous != ref_value:
                self._atomic_write(ref_path, ref_value)
        return {
            "latest": f"context:{name}@latest",
            "pinned": f"context:{name}@sha256:{digest}",
            "digest": digest,
            "producer": str(object_metadata.get("producer") or name),
            "depends_on": dict(object_metadata.get("depends_on") or {}),
        }

    def resolve(self, ref: str) -> ResolvedContextObject:
        match = _REF_RE.match(str(ref))
        if not match:
            raise ValueError("context ref must be context:<namespace>/<name>@latest|sha256:<digest>")
        name, selector = match.groups()
        name = self._validate_name(name)
        selector = selector or "latest"
        if selector == "latest":
            digest = str(self._load_refs().get(name, {}).get("digest", ""))
            if not digest:
                raise KeyError(f"context ref not found: {ref}")
        elif selector.startswith("sha256:"):
            digest = selector.split(":", 1)[1]
        else:
            raise ValueError(f"unsupported context selector: {selector}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid context digest")
        blob_path = self.blobs / f"{digest}.json"
        try:
            payload = json.loads(blob_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(f"context blob not found: {digest}") from exc
        content = str(payload.get("content", ""))
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            raise ValueError(f"context blob integrity failure: {digest}")
        return ResolvedContextObject(
            ref=f"context:{name}@sha256:{digest}", logical_name=name,
            digest=digest, media_type=str(payload.get("media_type", "text/plain")),
            content=content, metadata=dict(payload.get("metadata", {})),
        )

    def search(self, query: str, *, namespace: str = "", limit: int = 20) -> list[dict]:
        needle = query.casefold().strip()
        if not needle:
            raise ValueError("query is required")
        results = []
        for name, info in sorted(self._load_refs().items()):
            if namespace and not name.startswith(namespace.rstrip("/") + "/"):
                continue
            try:
                item = self.resolve(f"context:{name}@sha256:{info['digest']}")
            except (KeyError, ValueError, OSError):
                continue
            haystack = f"{name}\n{item.content}".casefold()
            if needle not in haystack:
                continue
            offset = haystack.find(needle)
            results.append({
                "ref": f"context:{name}@latest",
                "digest": item.digest,
                "preview": item.content[max(0, offset - 120):offset + 360],
            })
            if len(results) >= max(1, min(limit, 100)):
                break
        return results

    def materialize(
        self, session, ref: str, *, max_chars: int = 24000, offset: int = 0,
    ) -> dict:
        with session._context_memo_lock:
            return self._materialize_locked(
                session, ref, max_chars=max_chars, offset=offset,
            )

    def _materialize_locked(
        self, session, ref: str, *, max_chars: int, offset: int,
    ) -> dict:
        item = self.resolve(ref)
        current = self._load_refs().get(item.logical_name) or {}
        current_digest = str(current.get("digest") or "")
        version_status = {
            "state": "current" if current_digest == item.digest else "historical" if current_digest else "unknown",
            "current_ref": f"context:{item.logical_name}@latest",
            "current_digest": current_digest,
            "note": "Version identity only; a changed version is not proof that a defect is fixed.",
        }
        max_chars = max(1000, min(int(max_chars), 100000))
        offset = max(0, int(offset))
        key = (
            f"{session.context_epoch}:{item.logical_name}:{item.digest}:"
            f"{offset}:{max_chars}"
        )
        if key in session.context_memo:
            previous = session.context_memo[key]
            return {
                "ref": ref, "resolved_ref": item.ref, "digest": item.digest,
                "memo_hit": True,
                "version_status": version_status,
                "content": (
                    f"Already materialized in context epoch {session.context_epoch} "
                    f"at turn {previous['turn']}; content is unchanged."
                ),
            }
        earlier = [
            value for value in session.context_memo.values()
            if value.get("logical_name") == item.logical_name
            and value.get("epoch") == session.context_epoch
        ]
        content = item.content
        mode = "full"
        base_digest = ""
        if earlier:
            latest = earlier[-1]
            try:
                base = self.resolve(
                    f"context:{item.logical_name}@sha256:{latest['digest']}"
                )
                diff = "\n".join(difflib.unified_diff(
                    base.content.splitlines(), item.content.splitlines(),
                    fromfile=base.digest, tofile=item.digest, lineterm="",
                ))
                if diff and len(diff) < len(content):
                    content = diff
                    mode = "diff"
                    base_digest = base.digest
            except (KeyError, ValueError, OSError):
                pass
        total_chars = len(content)
        if offset > total_chars:
            raise ValueError("context offset is beyond the materialized object")
        def build_result(end: int) -> dict:
            page = content[offset:end]
            truncated = end < total_chars
            if truncated:
                page += f"\n[context object continues at offset {end}]"
            return {
                "ref": ref, "resolved_ref": item.ref, "digest": item.digest,
                "base_digest": base_digest, "mode": mode, "memo_hit": False,
                "version_status": version_status,
                "media_type": item.media_type, "content": page,
                "offset": offset, "next_offset": end if truncated else None,
                "total_chars": total_chars, "truncated": truncated,
            }

        result = _fit_transport_page(
            content, offset=offset, max_chars=max_chars,
            build_result=build_result,
        )
        session.context_memo[key] = {
            "logical_name": item.logical_name, "digest": item.digest,
            "epoch": session.context_epoch, "turn": len(session.messages),
        }
        return result

    def _load_refs(self) -> dict:
        refs = {}
        if self.refs_path.exists():
            try:
                refs.update(json.loads(self.refs_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        if self.refs_dir.exists():
            for path in self.refs_dir.rglob("*.json"):
                try:
                    name = path.relative_to(self.refs_dir).with_suffix("").as_posix()
                    refs[name] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        return refs

    @staticmethod
    def _validate_name(name: str) -> str:
        clean = str(name).strip().strip("/")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.\/-]+", clean):
            raise ValueError(f"invalid context logical name: {name}")
        if ".." in Path(clean).parts:
            raise ValueError("context logical name cannot traverse")
        return clean

    @staticmethod
    def _atomic_write(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def seed_context_objects(workspace_path: str, context: dict) -> dict:
    """Publish the current governance layers and return latest+pinned refs."""
    if not workspace_path:
        return dict(context)
    store = ContextObjectStore(workspace_path)
    contract = store.put("task/contract", context.get("task_contract", {}), metadata={
        "producer": "task_contract_admission",
    })
    lessons = store.put("knowledge/project-lessons", context.get("lessons", []), metadata={
        "producer": "LessonManager",
    })
    governance_dependencies = {
        "task_contract": contract["digest"],
        "project_lessons": lessons["digest"],
    }
    policy_source = (context.get("evidence_sources") or {}).get("policy_snapshot") or {}
    if policy_source.get("digest"):
        governance_dependencies["policy_snapshot"] = str(policy_source["digest"])
    governance = store.put("governance/active", {
        "brief": context.get("brief", ""),
        "signals": context.get("signals", []),
        "evidence_candidates": context.get("evidence_candidates", []),
    }, metadata={
        "authority": "privileged", "producer": "GovernanceProjection",
        "depends_on": governance_dependencies,
    })
    refs = {
        "governance_active": governance,
        "project_lessons": lessons,
        "task_contract": contract,
    }
    return {**context, "context_refs": refs}


def build_context_tools(process, workspace_path: str) -> dict[str, AgentTool]:
    if not workspace_path or process.session is None:
        return {}
    store = ContextObjectStore(workspace_path)
    runtime_storage = getattr(getattr(process, "_manager", None), "storage", None)

    def context_open(args: dict) -> dict:
        return store.materialize(
            process.session, str(args.get("ref", "")),
            max_chars=int(args.get("max_chars", 24000) or 24000),
            offset=int(args.get("offset", 0) or 0),
        )

    def context_search(args: dict) -> dict:
        return {"results": store.search(
            str(args.get("query", "")), namespace=str(args.get("namespace", "")),
            limit=int(args.get("limit", 20) or 20),
        )}

    def dependency_query(args: dict) -> dict:
        from backend.core.dependency_graph import load_dependency_graph
        graph = load_dependency_graph(Path(workspace_path))
        path = str(args.get("path", ""))
        return {"path": path, "dependents": graph.get_dependents(
            path, min_confidence=float(args.get("min_confidence", 0.0) or 0.0),
            include_dismissed=bool(args.get("include_dismissed", False)),
        )}

    def artifact_read(args: dict) -> dict:
        raw_path = Path(str(args.get("path", "")))
        target = (Path(workspace_path) / raw_path).resolve()
        target.relative_to(Path(workspace_path).resolve())
        if not target.is_file():
            raise ValueError("artifact path is not a workspace file")
        content = target.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        expected = str(args.get("sha256", ""))
        if expected and expected != digest:
            raise ValueError("artifact digest mismatch")
        offset = max(0, int(args.get("offset", 0) or 0))
        max_chars = max(1000, min(int(args.get("max_chars", 24000) or 24000), 100000))
        if offset > len(content):
            raise ValueError("artifact offset is beyond the file")
        relative = target.relative_to(Path(workspace_path).resolve()).as_posix()

        def build_result(end: int) -> dict:
            return {
                "path": relative, "sha256": digest,
                "content": content[offset:end], "offset": offset,
                "next_offset": end if end < len(content) else None,
                "total_chars": len(content),
            }

        return _fit_transport_page(
            content, offset=offset, max_chars=max_chars,
            build_result=build_result,
        )

    tools = {
        "context_open": AgentTool(
            name="context_open", description="Resolve and materialize a latest or pinned context ref.",
            parameters={"type": "object", "properties": {
                            "ref": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 0},
                            "max_chars": {"type": "integer", "minimum": 1000}},
                        "required": ["ref"]},
            execute=context_open, effect=ToolEffect.READ, idempotent=True,
        ),
        "context_search": AgentTool(
            name="context_search", description="Search addressable project context objects.",
            parameters={"type": "object", "properties": {
                "query": {"type": "string"}, "namespace": {"type": "string"},
                "limit": {"type": "integer"}}, "required": ["query"]},
            execute=context_search, effect=ToolEffect.READ, idempotent=True,
        ),
        "dependency_query": AgentTool(
            name="dependency_query", description="Query evidence-ranked dependents for a workspace file.",
            parameters={"type": "object", "properties": {
                "path": {"type": "string"}, "min_confidence": {"type": "number"},
                "include_dismissed": {"type": "boolean"}}, "required": ["path"]},
            execute=dependency_query, effect=ToolEffect.READ, idempotent=True,
        ),
        "artifact_read": AgentTool(
            name="artifact_read", description="Read a workspace artifact, optionally pinned by SHA-256.",
            parameters={"type": "object", "properties": {
                "path": {"type": "string"}, "sha256": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000}},
                "required": ["path"]},
            execute=artifact_read, effect=ToolEffect.READ, idempotent=True,
        ),
    }
    if runtime_storage is not None:
        def tool_result_open(args: dict) -> dict:
            try:
                return runtime_storage.read_tool_result(
                    str(args.get("locator", "")),
                    offset=int(args.get("offset", 0) or 0),
                    max_chars=int(args.get("max_chars", 24000) or 24000),
                    json_pointer=str(args.get("json_pointer", "") or ""),
                    query=str(args.get("query", "") or ""),
                )
            except (KeyError, ValueError, PermissionError, OSError) as exc:
                from backend.core.errors import error_payload
                return error_payload(
                    "TOOL_RESULT_LOCATOR_INVALID", message=str(exc),
                    details={"locator": str(args.get("locator", ""))[:160]},
                    next_actions=[{"action": "reuse_current_locator"},
                                  {"action": "rerun_source_tool"}],
                )

        tools["tool_result_open"] = AgentTool(
            name="tool_result_open",
            description=(
                "Open or search a content-addressed oversized tool result. Use the "
                "locator and next_offset returned by TOOL_RESULT_SPILL; JSON results "
                "may also be narrowed with an RFC 6901 JSON Pointer."
            ),
            parameters={"type": "object", "properties": {
                "locator": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000},
                "json_pointer": {"type": "string"},
                "query": {"type": "string"},
            }, "required": ["locator"]},
            execute=tool_result_open,
            effect=ToolEffect.READ,
            idempotent=True,
        )
    return tools
