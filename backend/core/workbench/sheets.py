"""Versioned scratch documents in the existing project CAS and state repository.

Only this service constructs notebook metadata. Model notes are never execution
receipts, approved lessons, or arbitrary executable notebook cells.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import uuid

from backend.core.storage.redaction import redact_for_persistence

_NAMESPACE = "workbench.v1"
_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
_MAX_BYTES = 240_000


class RevisionConflict(ValueError):
    def __init__(self, current):
        super().__init__("The sheet changed; open its current revision before revising.")
        self.current = current


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty text of at most {maximum} characters")
    return value


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid sheet or cell id")
    return value


def _validate(document):
    # Unlike validate(), isvalid does not repair duplicate/missing cell IDs.
    from nbformat.validator import isvalid
    if not isvalid(document, version=4, version_minor=5):
        raise ValueError("Invalid notebook document; no repair was applied")
    from .data import verify_snapshots
    verify_snapshots(document)


def _notes(notes):
    if not isinstance(notes, list) or len(notes) > 100:
        raise ValueError("notes must contain at most 100 cells")
    cells = []
    for note in notes:
        if not isinstance(note, dict) or set(note) != {"id", "text"}:
            raise ValueError("each note requires only id and text; authority metadata is not accepted")
        cells.append({"id": _identifier(note["id"]), "cell_type": "markdown",
                      "metadata": {"gitgo": {"kind": "model_note"}},
                      "source": _text(note["text"], "note text", 24_000)})
    return cells


class SheetStore:
    def __init__(self, storage, *, session_id: str, process_id: str, owner_scope: str = "process"):
        if storage is None or not session_id or not process_id:
            raise ValueError("A bound storage runtime, session and process are required")
        self.storage = storage
        self.process_id = process_id
        # Runtime owns scope. Main processes are replaced on every user turn,
        # while their saved AgentSession continues. Children keep process scope.
        # Retain the old process key format for existing explicit process scopes;
        # never scan/adopt another owner's historical material as a fallback.
        if owner_scope == "main_session":
            identity = json.dumps({"version": 2, "session_id": session_id, "scope": "main_session"}, sort_keys=True)
        elif owner_scope == "process":
            identity = session_id + "\0" + process_id
        else:
            raise ValueError("unsupported server-owned sheet scope")
        self.owner = hashlib.sha256(identity.encode()).hexdigest()

    def _head(self, sheet_id):
        return f"{self.owner}/head/{_identifier(sheet_id)}"

    def _revision_key(self, sheet_id, revision):
        if not isinstance(revision, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", revision):
            raise ValueError("a pinned sheet revision is required")
        return f"{self.owner}/revision/{_identifier(sheet_id)}/{revision}"

    def _load(self, sheet_id, revision=None):
        head = self.storage.get_state_ref(_NAMESPACE, self._head(sheet_id))
        selected = revision or head
        if not selected or self.storage.get_state_ref(
                _NAMESPACE, self._revision_key(sheet_id, selected)) != selected:
            raise KeyError("sheet revision not found in this session/process")
        raw = self.storage.read_blob(selected)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != selected:
            raise ValueError("sheet revision integrity failure")
        document = json.loads(raw)
        _validate(document)
        return document, selected, head

    def _save(self, sheet_id, document, expected):
        if len(document["cells"]) > 100:
            raise ValueError("a sheet may have at most 100 cells")
        ids = [cell["id"] for cell in document["cells"]]
        if len(ids) != len(set(ids)):
            raise ValueError("cell ids must be unique")
        # Match the existing persistence privacy boundary. Revisions identify
        # the stored/redacted document, never claim a hash of original secrets.
        document = redact_for_persistence(document)
        _validate(document)
        raw = json.dumps(document, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > _MAX_BYTES:
            raise ValueError("sheet exceeds the 240000-byte storage limit")
        ref = self.storage.put_blob(raw, media_type="application/x-ipynb+json")
        if not self.storage.commit_state_revision(
                _NAMESPACE, self._head(sheet_id), ref, expected_ref=expected,
                revision_key=self._revision_key(sheet_id, ref)):
            raise RevisionConflict(self.storage.get_state_ref(_NAMESPACE, self._head(sheet_id)))
        return self._summary(document, ref)

    @staticmethod
    def _summary(document, revision):
        meta = document["metadata"]["gitgo"]
        return {"sheet_id": meta["sheet_id"], "title": meta["title"],
                "revision": revision, "sequence": meta["sequence"],
                "parent_revision": meta["parent_revision"],
                "transition": deepcopy(meta["transition"]),
                "cell_count": len(document["cells"]),
                "authority": "working_material_only"}

    def create(self, title, notes, *, continuation=None):
        sheet_id = uuid.uuid4().hex
        cells = _notes(notes)
        transition = None
        if continuation is not None:
            self._revision_key(continuation["sheet_id"], continuation["revision"])
            origin, revision, _ = self._load(continuation["sheet_id"], continuation["revision"])
            selected = continuation["cell_ids"]
            if not isinstance(selected, list) or not all(isinstance(c, str) for c in selected):
                raise ValueError("cell_ids must be an explicit list")
            if len(selected) != len(set(selected)):
                raise ValueError("continuation cell ids must be unique")
            by_id = {c["id"]: c for c in origin["cells"]}
            if any(c not in by_id for c in selected):
                raise ValueError("continuation references a missing cell")
            cells = [deepcopy(by_id[c]) for c in selected] + cells
            transition = {"sheet_id": continuation["sheet_id"], "revision": revision,
                          "cell_ids": selected,
                          "reason": _text(continuation["reason"], "transition reason", 2000)}
        document = {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
                    "metadata": {"gitgo": {"schema": 1, "sheet_id": sheet_id,
                        "title": _text(title, "title", 240), "sequence": 1,
                        "parent_revision": None, "transition": transition}}}
        return self._save(sheet_id, document, None)

    def open(self, sheet_id, revision=None, *, offset=0, limit=20):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("offset must be nonnegative and limit must be 1..20")
        document, selected, head = self._load(sheet_id, revision)
        cells = document["cells"][offset:offset + limit]
        return {**self._summary(document, selected), "current_revision": head,
                "version_state": "current" if selected == head else "historical",
                "document_format": "nbformat-4.5", "cells": cells,
                "next_offset": offset + len(cells) if offset + len(cells) < len(document["cells"]) else None}

    def list(self, *, after="", limit=20):
        if after:
            _identifier(after)
        prefix = f"{self.owner}/head/"
        rows = self.storage.list_state_refs(_NAMESPACE, prefix=prefix,
            after=prefix + after if after else "", limit=limit)
        result = []
        for row in rows:
            document, revision, _ = self._load(row["key"][len(prefix):], row["value_ref"])
            result.append(self._summary(document, revision))
        return {"sheets": result, "next_after": result[-1]["sheet_id"] if len(result) == limit else None}

    def reference_snapshot(self, *, limit=5):
        prefix = f"{self.owner}/head/"
        rows = self.storage.list_state_refs(_NAMESPACE, prefix=prefix, limit=limit)
        return {"sheets": [{"sheet_id": r["key"][len(prefix):], "revision": r["value_ref"]} for r in rows],
                "next_after": rows[-1]["key"][len(prefix):] if len(rows) == limit else None}

    def revise(self, sheet_id, expected_revision, notes, *, title=None):
        self._revision_key(sheet_id, expected_revision)
        document, selected, head = self._load(sheet_id, expected_revision)
        if selected != head:
            raise RevisionConflict(head)
        # Revision input is deliberately notes-only. Existing observed or derived
        # cells cannot be forged by attaching model-provided metadata.
        document["cells"] = _notes(notes) + [c for c in document["cells"]
            if c["metadata"]["gitgo"]["kind"] != "model_note"]
        return self.commit(sheet_id, document, selected, title=title)

    def commit(self, sheet_id, document, expected_revision, *, title=None):
        meta = document["metadata"]["gitgo"]
        meta["sequence"] += 1
        meta["parent_revision"] = expected_revision
        if title is not None:
            meta["title"] = _text(title, "title", 240)
        return self._save(sheet_id, document, expected_revision)
