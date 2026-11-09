"""Context-reference capture and derived values over immutable sheet inputs.

Version applicability delegates to the existing evidence graph; it never
certifies upstream completeness, semantic truth, or a natural-language answer.
"""
from __future__ import annotations
from copy import deepcopy
import json
from decimal import Decimal

from backend.core.loop.evidence_applicability import EvidenceApplicability
from .data import aggregate, aggregate_value, parse_json, snapshot_json, text_digest
from backend.core.storage.redaction import redact_for_persistence
from .sheets import RevisionConflict, _identifier, _text


class SourceUnavailable(ValueError):
    pass


class InvalidData(ValueError):
    pass


SOURCE_KINDS = {"context_source", "provided_data", "tool_result_source"}
MAX_RESULT_DEPTH = 16

class SourceWorkbench:
    def __init__(self, sheets, contexts, current_context=lambda: {}):
        self.sheets = sheets
        self.contexts = contexts
        self.current_context = current_context

    def _prepare(self, sheet_id, expected_revision):
        self.sheets._revision_key(sheet_id, expected_revision)
        document, revision, head = self.sheets._load(sheet_id, expected_revision)
        if revision != head:
            raise RevisionConflict(head)
        return document

    @staticmethod
    def _append(document, cell):
        _identifier(cell["id"])
        if any(c["id"] == cell["id"] for c in document["cells"]):
            raise ValueError("cell_id already exists; use a distinct id to preserve prior material")
        document["cells"].append(cell)

    def import_context(self, sheet_id, expected_revision, *, cell_id, source_ref):
        document = self._prepare(sheet_id, expected_revision)
        try:
            item = self.contexts.resolve(source_ref)
            info = self.contexts.reference_info(item.ref)
        except (OSError, KeyError, ValueError) as exc:
            raise SourceUnavailable("The context reference could not be resolved with its integrity intact") from exc
        if item.media_type != "application/json":
            raise InvalidData("This operation imports application/json context records only")
        try:
            snapshot, redacted = snapshot_json(item.content)
        except ValueError as exc:
            raise InvalidData(str(exc)) from exc
        meta = {"kind": "context_source", "ref": item.ref,
                "logical_name": item.logical_name, "origin_digest": item.digest,
                "snapshot_sha256": text_digest(snapshot), "redacted": redacted,
                "dependency_metadata_state": info["dependency_metadata_state"],
                "producer": info["producer"], "depends_on": info["depends_on"],
                "scope": {"workspace_path": str(self.contexts.workspace)},
                "coverage": "Imported context record only; its existence and identity do not certify upstream truth or completeness."}
        if redact_for_persistence(meta) != meta:
            raise InvalidData("Reference metadata requires privacy filtering that would change the source binding")
        self._append(document, {"id": cell_id, "cell_type": "raw",
                               "metadata": {"gitgo": meta}, "source": snapshot})
        committed = self.sheets.commit(sheet_id, document, expected_revision)
        return {**committed, "cell_id": cell_id, "source_ref": item.ref,
                "snapshot_sha256": meta["snapshot_sha256"], "redacted": redacted,
                "applicability": self._assess_source(meta)}

    def add_data(self, sheet_id, expected_revision, *, cell_id, json_text, description):
        document = self._prepare(sheet_id, expected_revision)
        try:
            snapshot, redacted = snapshot_json(json_text)
        except ValueError as exc:
            raise InvalidData(str(exc)) from exc
        meta = {"kind": "provided_data", "snapshot_sha256": text_digest(snapshot),
                "description": _text(description, "data description", 2000), "redacted": redacted,
                "origin": "model_assembled; may transcribe user data, but fidelity is not independently established"}
        self._append(document, {"id": cell_id, "cell_type": "raw",
                              "metadata": {"gitgo": meta}, "source": snapshot})
        committed = self.sheets.commit(sheet_id, document, expected_revision)
        return {**committed, "cell_id": cell_id, "applicability": self._assess_source(meta)}

    @staticmethod
    def _read_result(storage, locator, process_id):
        chunks, offset = [], 0
        while True:
            page = storage.read_tool_result(locator, offset=offset, max_chars=24000,
                                            owner_process_id=process_id)
            if page["media_type"] != "application/json" or page["total_chars"] > 120000:
                raise ValueError("Imported tool result must be bounded JSON, not an oversized or textual result")
            chunks.append(page["content"])
            following = page["next_offset"]
            if following is None:
                break
            if following <= offset:
                raise ValueError("tool result pagination did not advance")
            offset = following
        text = "".join(chunks)
        if locator != "tool-result:sha256:" + text_digest(text):
            raise ValueError("tool result integrity failed")
        return text

    def import_result(self, sheet_id, expected_revision, *, cell_id, locator):
        document = self._prepare(sheet_id, expected_revision)
        try:
            raw = self._read_result(self.sheets.storage, locator, self.sheets.process_id)
        except (KeyError, ValueError, OSError) as exc:
            raise SourceUnavailable("The registered JSON tool result is unavailable in this process or failed its bounds/integrity") from exc
        try:
            snapshot, redacted = snapshot_json(raw)
        except ValueError as exc:
            raise InvalidData(str(exc)) from exc
        meta = {"kind": "tool_result_source", "ref": locator,
                "owner_process_id": self.sheets.process_id,
                "snapshot_sha256": text_digest(snapshot), "origin_digest": text_digest(raw),
                "redacted": redacted, "scope": {"workspace_path": str(self.contexts.workspace)},
                "origin": "registered immutable tool output; not evidence of present external state"}
        self._append(document, {"id": cell_id, "cell_type": "raw",
                              "metadata": {"gitgo": meta}, "source": snapshot})
        committed = self.sheets.commit(sheet_id, document, expected_revision)
        return {**committed, "cell_id": cell_id, "source_ref": locator, "applicability": self._assess_source(meta)}

    def _assess_source(self, meta, *, context=None, reference_cache=None):
        if meta.get("kind") == "provided_data":
            return {"state": "needs_recheck", "reason": "model_assembled_data",
                    "scope_note": "Exact calculation is conditional on this supplied data; transcription, classification and completeness were not independently checked."}
        if meta.get("kind") == "tool_result_source":
            storage = self.sheets.storage if self.sheets is not None else None
            if storage is None:
                from backend.core.storage import get_storage
                storage = get_storage(self.contexts.workspace)
            try:
                raw = self._read_result(storage, meta["ref"], meta["owner_process_id"])
            except (KeyError, ValueError, OSError):
                return {"state": "needs_recheck", "reason": "tool_result_unavailable"}
            return {"state": "current" if text_digest(raw) == meta["origin_digest"] else "needs_recheck",
                    "reason": "stored_tool_result_identity",
                    "scope_note": "Immutable recorded output identity only; not re-execution, current external state or semantic verification."}
        if meta.get("dependency_metadata_state") != "matching_ref":
            return {"state": "needs_recheck", "reason": "historical_dependency_metadata_unavailable"}
        try:
            cache = reference_cache if reference_cache is not None else {}
            if meta["ref"] not in cache:
                cache[meta["ref"]] = self.contexts.reference_info(meta["ref"])
            info = cache[meta["ref"]]
        except (OSError, KeyError, ValueError):
            return {"state": "needs_recheck", "reason": "source_unavailable"}
        context = deepcopy(self.current_context() or {}) if context is None else deepcopy(context)
        # The bound context-store workspace is Runtime supplied. Imported
        # metadata does not choose the current project or elevate its scope.
        context["workspace_path"] = str(self.contexts.workspace)
        key = "context:" + meta["logical_name"]
        current_sources = dict(context.get("evidence_sources") or {})
        current_sources[key] = {"digest": info["current_digest"],
                                "depends_on": info["depends_on"]}
        context["evidence_sources"] = current_sources
        evidence = {"scope": meta["scope"],
                    "depends_on": {**meta.get("depends_on", {}), key: meta["origin_digest"]}}
        assessment = EvidenceApplicability.assess(evidence, context)
        if assessment["state"] == "current" and (
                info["dependency_metadata_state"] != "matching_ref" or
                info["depends_on"] != meta["depends_on"] or info["producer"] != meta["producer"]):
            assessment = {"state": "needs_recheck", "reason": "source_metadata_changed"}
        return {**assessment, "scope_note": "Version/dependency applicability only; not semantic verification or proof of source freshness in the external world."}

    def _pinned_cells(self, ref, documents):
        if not isinstance(ref, dict) or set(ref) != {"sheet_id", "revision", "cell_id"}:
            raise InvalidData("a result reference requires sheet_id, revision and cell_id")
        _identifier(ref["cell_id"])
        sheet_id, revision = ref["sheet_id"], ref["revision"]
        key = (sheet_id, revision)
        self.sheets._revision_key(*key)
        if key not in documents:
            document, _, _ = self.sheets._load(*key)
            documents[key] = {cell["id"]: cell for cell in document["cells"]}
        return documents[key]

    def _lineage(self, cell, cells, documents):
        dependencies = []
        while cell["metadata"]["gitgo"]["kind"] == "computed_value":
            if len(dependencies) >= MAX_RESULT_DEPTH:
                raise InvalidData("calculation dependency depth exceeds 16")
            meta = cell["metadata"]["gitgo"]
            ref = meta.get("input_result_ref")
            if ref:
                if ref in dependencies:
                    raise InvalidData("calculation dependency cycle")
                dependencies.append(ref)
                cells = self._pinned_cells(ref, documents)
            else:
                # Older computations identify an observed input in this document.
                dependencies.append({"sheet_id": meta["input_sheet_id"],
                                     "revision": meta["input_revision"], "cell_id": meta["input_cell_id"]})
            cell = cells.get(meta["input_cell_id"])
            if cell is None or text_digest(cell["source"]) != meta["input_sha256"]:
                raise SourceUnavailable("calculation_input_not_present")
            if not ref and cell["metadata"]["gitgo"]["kind"] not in SOURCE_KINDS:
                raise SourceUnavailable("calculation_input_not_present")
        source = cell["metadata"]["gitgo"]
        if source["kind"] not in SOURCE_KINDS:
            raise InvalidData("calculation inputs must be observed data or computed values, not model notes")
        return source, dependencies

    def read_result(self, ref, *, context=None):
        """Resolve a pinned computed cell within the Runtime-bound owner scope."""
        documents = {}
        cells = self._pinned_cells(ref, documents)
        cell = cells.get(ref["cell_id"])
        if cell is None or cell["metadata"]["gitgo"]["kind"] != "computed_value":
            raise SourceUnavailable("result reference does not select a computed value")
        source, dependencies = self._lineage(cell, cells, documents)
        return {**json.loads(cell["source"]), "result_ref": dict(ref),
                "dependencies": deepcopy(dependencies), "source": deepcopy(source),
                "applicability": self._assess_source(source, context=context)}

    def assessments(self, document):
        cells = {c["id"]: c for c in document["cells"]}
        context = deepcopy(self.current_context() or {})
        reference_cache, documents, result = {}, {}, {}
        for cell_id, cell in cells.items():
            if cell["metadata"]["gitgo"]["kind"] not in SOURCE_KINDS | {"computed_value"}:
                continue
            try:
                source, _ = self._lineage(cell, cells, documents)
            except (KeyError, ValueError, OSError):
                result[cell_id] = {"state": "needs_recheck", "reason": "calculation_input_not_present"}
            else:
                result[cell_id] = self._assess_source(source, context=context, reference_cache=reference_cache)
        return result

    def check(self, sheet_id, revision=None):
        document, selected, head = self.sheets._load(sheet_id, revision)
        return {"sheet_id": sheet_id, "revision": selected, "current_revision": head,
                "cell_applicability": self.assessments(document),
                "meaning": "Status is recomputed from current reference/dependency state; the historical document is not modified."}

    def project_open(self, opened):
        # Assess all inputs, even when only one page of cells was requested.
        return {**opened, "cell_applicability": self.check(opened["sheet_id"], opened["revision"])["cell_applicability"]}

    def aggregate(self, sheet_id, expected_revision, *, cell_id, selector, reducer,
                  source_cell_id=None, source_result=None, field="", null_policy="reject"):
        document = self._prepare(sheet_id, expected_revision)
        if bool(source_cell_id) == bool(source_result):
            raise InvalidData("choose exactly one of source_cell_id and source_result")
        ref = source_result or {"sheet_id": sheet_id, "revision": expected_revision, "cell_id": source_cell_id}
        documents = {(sheet_id, expected_revision): {c["id"]: c for c in document["cells"]}}
        cells = self._pinned_cells(ref, documents)
        source = cells.get(ref["cell_id"])
        if source is None:
            raise SourceUnavailable("calculation input is missing")
        source_meta, dependencies = self._lineage(source, cells, documents)
        if len(dependencies) >= MAX_RESULT_DEPTH:
            raise InvalidData("calculation dependency depth exceeds 16")
        kind = source["metadata"]["gitgo"]["kind"]
        if source_result and kind != "computed_value":
            raise InvalidData("source_result must select a computed value")
        query = {"selector": selector, "reducer": reducer, "field": field, "null_policy": null_policy}
        try:
            if kind == "computed_value":
                previous = json.loads(source["source"])["result"]
                if previous["value_type"] == "scalar_set_json":
                    values = parse_json(previous["value"])
                else:
                    # Existing numeric reducers accept a collection. Treat one
                    # derived number as a one-row input without float conversion.
                    values = [Decimal(previous["value"])]
                result = aggregate_value(values, **query)
            else:
                result = aggregate(source["source"], **query)
        except ValueError as exc:
            raise InvalidData(str(exc)) from exc
        payload = {"algorithm": "gitgo.json-aggregate.v1", "query": query, "result": result}
        meta = {"kind": "computed_value", "input_cell_id": ref["cell_id"],
                "input_sha256": text_digest(source["source"]),
                "input_sheet_id": ref["sheet_id"], "input_revision": ref["revision"],
                "source_ref": source_meta.get("ref"), "origin": kind, "algorithm": payload["algorithm"]}
        if kind == "computed_value":
            meta["input_result_ref"] = dict(ref)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        meta["payload_sha256"] = text_digest(encoded)
        self._append(document, {"id": cell_id, "cell_type": "raw", "metadata": {"gitgo": meta},
                               "source": encoded})
        committed = self.sheets.commit(sheet_id, document, expected_revision)
        return {**committed, "cell_id": cell_id, **payload,
                "result_ref": {"sheet_id": sheet_id, "revision": committed["revision"], "cell_id": cell_id},
                "input": {**ref, "sha256": meta["input_sha256"],
                          "source_ref": meta["source_ref"], "redacted": source_meta["redacted"]},
                "applicability": self._assess_source(source_meta)}
