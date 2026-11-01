"""Project/task-bound candidates; accepted obligations use task_maintenance."""
from __future__ import annotations

from copy import deepcopy
import json

from .contracts import digest


def discussion_event(payload):
    event = payload.get('event')
    if not isinstance(event, dict) or any(not isinstance(event.get(key), str) or not event[key]
        for key in ('discussion_ref', 'sidecar_id')):
        raise ValueError('MECHANISM_DISCUSSION_EVENT_REQUIRED: bind the original lifecycle event, not judgment evidence')
    return event


class Inbox:
    def __init__(self, application):
        self.app = application

    def discussion(self, reference, sidecar_id=None):
        from backend.core.loop.discussion_adoption import NAMESPACE
        record = json.loads(self.app.repo.storage.read_blob(reference))
        if record.get("project_id") != self.app.repo.project_id or not record.get("read_only") or record.get("status") != "completed":
            raise ValueError("MECHANISM_DISCUSSION_SOURCE_INVALID")
        if sidecar_id is not None and record["sidecar_id"] != sidecar_id:
            raise ValueError("MECHANISM_DISCUSSION_SOURCE_CHANGED")
        if self.app.repo.storage.get_state_ref(NAMESPACE, f"revision/{record['sidecar_id']}/{record['revision']}") != reference:
            raise ValueError("MECHANISM_DISCUSSION_SOURCE_UNREGISTERED")
        target = record.get("targets", {}).get(self.app.process.process_id)
        if not target or target.get("task_id") != self.app.task:
            raise ValueError("MECHANISM_DISCUSSION_TARGET_STALE")
        return record

    def add(self, ctx, payload):
        if ctx.boundary != "discussion_candidate" or ctx.task_id != self.app.task:
            raise ValueError("MECHANISM_INBOX_BOUNDARY_INVALID")
        event = discussion_event(payload)
        record = self.discussion(event["discussion_ref"], event["sidecar_id"])
        identifier = "candidate-" + digest([record["sidecar_id"], record["answer"]])[:24]
        item = {"id": identifier, "revision": 1, "status": "candidate", "text": record["answer"],
            "question": record["question"], "source_refs": [event["discussion_ref"]], "edited_source_refs": [],
            "sidecar_id": record["sidecar_id"], "task_id": ctx.task_id,
            "source_contract_revision": record["targets"][self.app.process.process_id].get("contract_revision", 0),
            "authority_granted": False}
        def save(instance):
            items = instance.setdefault("inbox", {})
            old = items.get(identifier)
            if old:
                if event["discussion_ref"] not in old["source_refs"]:
                    old["source_refs"].append(event["discussion_ref"])
                return old
            if len(items) >= 128:
                raise ValueError("MECHANISM_INBOX_CAPACITY")
            items[identifier] = deepcopy(item)
            return item
        return self.app.repo.mutate(ctx.task_id, ctx.instance_id, save)

    def _candidate(self, instance_id, candidate_id):
        instance, _ = self.app.repo.instance(self.app.task, instance_id)
        item = (instance or {}).get("inbox", {}).get(candidate_id)
        if not item:
            raise ValueError("MECHANISM_CANDIDATE_NOT_FOUND")
        return instance, item

    def control(self, args):
        operation = args["operation"]
        sources = self.app.sources(args.get("source_refs"))
        instance_id = args["instance_id"]
        if operation == "inbox_merge":
            return self.merge(args, sources)
        instance, item = self._candidate(instance_id, args["candidate_id"])
        if operation == "inbox_adopt":
            return self.adopt(args, sources, instance, item)
        if operation not in {"inbox_revise", "inbox_discard"}:
            raise ValueError("MECHANISM_INBOX_OPERATION_INVALID")
        def change(value):
            candidate = value["inbox"][item["id"]]
            if candidate["revision"] != args["candidate_revision"]:
                raise ValueError("MECHANISM_CANDIDATE_REVISION_CONFLICT")
            if candidate["status"] != "candidate":
                raise ValueError("MECHANISM_CANDIDATE_ALREADY_DISPOSED")
            if operation == "inbox_revise":
                text = args.get("text", "")
                if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                    raise ValueError("MECHANISM_CANDIDATE_TEXT_REQUIRED")
                candidate["text"] = text
                candidate["edited_source_refs"] = list(dict.fromkeys(candidate["edited_source_refs"] + list(sources)))
            else:
                candidate["status"] = "discarded"
                candidate["disposition_sources"] = deepcopy(sources)
            candidate["revision"] += 1
            return candidate
        return {"candidate": self.app.repo.mutate(self.app.task, instance_id, change), "authority_granted": False}

    def merge(self, args, sources):
        items = args.get("candidates")
        text = args.get("text")
        if (not isinstance(items, list) or not 2 <= len(items) <= 8 or not isinstance(text, str) or not text.strip()
                or len(text) > 16000 or len({item["id"] for item in items}) != len(items)):
            raise ValueError("MECHANISM_CANDIDATE_MERGE_INVALID")
        merged_id = "candidate-" + digest({"items": items, "text": text, "sources": sources})[:24]
        def merge(value):
            inbox = value.get("inbox", {})
            if merged_id in inbox:
                return inbox[merged_id]
            originals = []
            for selected in items:
                item = inbox.get(selected["id"])
                if not item or item["revision"] != selected["revision"] or item["status"] != "candidate":
                    raise ValueError("MECHANISM_CANDIDATE_MERGE_STALE")
                originals.append(item)
            if len(inbox) >= 128:
                raise ValueError("MECHANISM_INBOX_CAPACITY")
            merged = {"id": merged_id, "revision": 1, "status": "candidate", "text": text, "task_id": self.app.task,
                "source_refs": list(dict.fromkeys(ref for item in originals for ref in item["source_refs"])),
                "edited_source_refs": list(sources), "merged_from": deepcopy(items),
                "source_contract_revision": min(item["source_contract_revision"] for item in originals), "authority_granted": False}
            for item in originals:
                item.update(status="merged", replaced_by=merged_id, revision=item["revision"] + 1)
            inbox[merged_id] = merged
            return merged
        return {"candidate": self.app.repo.mutate(self.app.task, args["instance_id"], merge), "authority_granted": False}

    def adopt(self, args, sources, instance, item):
        from backend.core.loop.task_contract import get_task_contract
        quote = args.get("quote")
        actual = [source.get("content") if source.get("kind") == "user_message" else
                  source.get("event", {}).get("answer") if source.get("kind") == "user_decision" else "" for source in sources.values()]
        if not isinstance(quote, str) or not quote.strip() or not any(quote in str(content or "") for content in actual):
            raise ValueError("MECHANISM_ADOPTION_ACTUAL_USER_QUOTE_REQUIRED")
        key = digest({"candidate": item["id"], "revision": args["candidate_revision"], "sources": sources, "quote": quote})
        change_id = "mechanism-inbox-" + key[:24]
        current = get_task_contract(self.app.process)
        def preparing(value):
            candidate = value["inbox"][item["id"]]
            if candidate.get("adoption_key") == key and candidate["status"] in {"pending_adoption", "adopted"}:
                return candidate
            if candidate["revision"] != args["candidate_revision"] or candidate["status"] != "candidate":
                raise ValueError("MECHANISM_CANDIDATE_ADOPTION_STALE")
            candidate.update(status="pending_adoption", adoption_key=key, change_id=change_id,
                adoption_sources=deepcopy(sources), adoption_quote=quote, revision=candidate["revision"] + 1,
                adoption_request_revision=args["candidate_revision"],
                adoption_contract_revision=args.get("contract_revision", current.get("revision", 0)))
            return candidate
        candidate = self.app.repo.mutate(self.app.task, instance["id"], preparing)
        return self._finish_adoption(instance["id"], candidate)

    def _finish_adoption(self, instance_id, candidate):
        from backend.core.loop.task_maintenance import add_change
        store = getattr(getattr(self.app.process, "_manager", None), "session_store", None)
        if store is None:
            raise ValueError("MECHANISM_ADOPTION_CHECKPOINT_UNAVAILABLE")
        change = add_change(self.app.process, candidate["text"], identifier=candidate["change_id"],
            source_ref="mechanism-inbox:" + candidate["id"], expected_task_id=self.app.task,
            expected_contract_revision=candidate["adoption_contract_revision"], store=store,
            source_contract_revision=candidate.get("source_contract_revision"), user_adopted=True)
        def adopted(value):
            item = value["inbox"][candidate["id"]]
            if item.get("adoption_key") != candidate["adoption_key"]:
                raise ValueError("MECHANISM_ADOPTION_IDENTITY_CHANGED")
            item["status"] = "adopted"
            item.pop("adoption_error", None)
            return item
        adopted_item = self.app.repo.mutate(self.app.task, instance_id, adopted)
        return {"candidate": adopted_item, "change": change, "next_action": "reconcile_scope_plan_and_validation_with_declare_task_contract",
                "authority_granted": False}

    def reconcile(self):
        from backend.core.loop.mechanism_lifecycle import fence
        from backend.core.loop.task_maintenance import state_for
        # Cross-store handoff uses a stable add_change identity. Reconcile the
        # existing accepted command; never repeat arbitrary component effects.
        with fence(self.app.process):
            for instance in self.app.repo.list_instances(self.app.task, limit=100):
                for candidate in instance.get("inbox", {}).values():
                    change = state_for(self.app.process).get("changes", {}).get(candidate.get("change_id"))
                    if candidate["status"] == "adopted" and change and change["status"] in {"adopted", "withdrawn"}:
                        def reconciled(value):
                            item = value["inbox"][candidate["id"]]
                            item["status"] = "processed" if change["status"] == "adopted" else "withdrawn"
                            item["processing_scope"] = "scope_plan_validation_reconciled_not_task_completed"
                            item["reconciliation"] = {key: change.get(key) for key in ("contract_revision", "plan_revision", "resolution")}
                        self.app.repo.mutate(self.app.task, instance["id"], reconciled)
                        continue
                    if candidate["status"] != "pending_adoption":
                        continue
                    try:
                        self._finish_adoption(instance["id"], candidate)
                    except ValueError as error:
                        def failed(value):
                            value["inbox"][candidate["id"]]["adoption_error"] = str(error)
                        self.app.repo.mutate(self.app.task, instance["id"], failed)

    def attach_material(self, ctx, payload):
        if ctx.boundary != "discussion_candidate" or ctx.task_id != self.app.task:
            raise ValueError("MECHANISM_MATERIAL_BOUNDARY_INVALID")
        event = discussion_event(payload)
        self.discussion(event["discussion_ref"], event["sidecar_id"])
        from backend.core.loop.question_broker import decision_evidence, attach_material
        cards = decision_evidence(self.app.process)["items"]
        valid = {item["decision_id"] for item in cards if item["status"] not in {"cancelled", "superseded", "withdrawn"}}
        target = payload.get("decision_id")
        if not target:
            # Supporting citations may include answered historical questions.
            # An implicit destination is a cited *awaiting* question, never
            # inferred from there merely being one card in the task. History
            # remains addressable by an explicit exact decision_id.
            awaiting = {item['decision_id'] for item in cards if item['status'] == 'awaiting_user'}
            referenced = set(payload.get("evidence", {}).get("evidence_refs", [])) & awaiting
            target = next(iter(referenced)) if len(referenced) == 1 else None
        if target not in valid:
            return {"attached": False, "reason": "question_target_missing_or_stale"}
        store = getattr(getattr(self.app.process, "_manager", None), "session_store", None)
        material = attach_material(self.app.process, target, event["discussion_ref"], store=store)
        return {"attached": True, "material": material}
