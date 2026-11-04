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
        self.prepared = {}

    def _bind(self, args):
        """Bind mechanical identities; the Agent still chooses candidates/text.

        This is preparation for the existing disposition operations, not a
        second inbox or adoption authority. Explicit legacy assertions remain
        assertions and are never silently replaced with current values.
        """
        context = self.app.capability_runtime.context()
        sources = self.app.sources(args['source_refs']) if 'source_refs' in args else context['sources']
        selected = args.get('candidates', []) if args['operation'] == 'inbox_merge' else [{'id': args.get('candidate_id')}]
        ids = [item['id'] for item in selected]
        if not ids or any(not name for name in ids) or len(set(ids)) != len(ids):
            raise ValueError('MECHANISM_CANDIDATE_SELECTION_REQUIRED')
        instances = self.app.repo.list_instances(self.app.task, limit=100)
        matches = [value for value in instances if (not args.get('instance_id') or value['id'] == args['instance_id'])
            and all(name in value.get('inbox', {}) for name in ids)]
        if len(matches) != 1:
            raise ValueError('MECHANISM_CANDIDATE_SCOPE_REQUIRED: select candidate ids from one current-task instance; specify instance_id if ambiguous')
        instance = matches[0]
        items = [deepcopy(instance['inbox'][name]) for name in ids]
        bound = {**deepcopy(args), 'instance_id': instance['id'], 'source_refs': list(sources)}
        for requested, item in zip(selected, items):
            revision = requested.get('revision') if args['operation'] == 'inbox_merge' else args.get('candidate_revision')
            if revision is not None and revision != item['revision']:
                raise ValueError('MECHANISM_CANDIDATE_REVISION_CONFLICT')
            if item['status'] != 'candidate':
                raise ValueError('MECHANISM_CANDIDATE_ALREADY_DISPOSED')
        if args['operation'] == 'inbox_merge':
            bound['candidates'] = [{'id': item['id'], 'revision': item['revision']} for item in items]
        else:
            bound['candidate_revision'] = items[0]['revision']
        if args['operation'] == 'inbox_adopt':
            self._adoption_quote(args, sources)
            if 'contract_revision' in args and args['contract_revision'] != context['contract_revision']:
                raise ValueError('MECHANISM_ADOPTION_CONTRACT_CHANGED')
            bound['contract_revision'] = context['contract_revision']
        # Counter/journal changes and other candidates do not change this
        # proposal. A changed selected candidate, instance generation, user
        # instruction or contract does invalidate it before any write.
        from .capability_runtime import instance_basis
        return {'context': context, 'bound': bound, 'sources': sources,
            'basis': instance_basis(instance), 'items': items}

    def prepare(self, args):
        key = digest(args)
        current = self._bind(args)
        previous = self.prepared.get(key)
        if previous is not None and previous != current:
            self.prepared.pop(key, None)
            raise ValueError('MECHANISM_CANDIDATE_PREPARED_STATE_CHANGED: inspect the changed candidate before proposing again')
        self.prepared[key] = current
        while len(self.prepared) > 32:
            del self.prepared[next(iter(self.prepared))]
        return {'kind': 'harness_candidate_disposition',
            'request': {key: deepcopy(value) for key, value in current['bound'].items()
                if key not in {'source_refs', 'candidate_revision', 'contract_revision'}},
            'current_candidates': [{key: deepcopy(item.get(key)) for key in ('id', 'revision', 'status', 'text')}
                for item in current['items']],
            'source_evidence': {ref: {'kind': value.get('kind'), 'digest': digest(value)}
                for ref, value in current['sources'].items()},
            'requires_actual_user_adoption': args['operation'] == 'inbox_adopt', 'authority_granted': False}

    def commit(self, args):
        from .application import configuration_allowed
        from backend.core.loop.mechanism_lifecycle import fence
        if not configuration_allowed(self.app.process):
            raise ValueError('MECHANISM_CONFIGURATION_CAPABILITY_REQUIRED')
        previous = self.prepared.get(digest(args))
        if previous is None:
            raise ValueError('MECHANISM_CANDIDATE_PREPARATION_REQUIRED')
        with fence(self.app.process):
            if self._bind(args) != previous:
                self.prepared.pop(digest(args), None)
                raise ValueError('MECHANISM_CANDIDATE_PREPARED_STATE_CHANGED')
            return self.control(previous['bound'])

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

    @staticmethod
    def _adoption_quote(args, sources):
        quote = args.get("quote")
        actual = [source.get("content") if source.get("kind") == "user_message" else
                  source.get("event", {}).get("answer") if source.get("kind") == "user_decision" else "" for source in sources.values()]
        if not isinstance(quote, str) or not quote.strip() or not any(quote in str(content or "") for content in actual):
            raise ValueError("MECHANISM_ADOPTION_ACTUAL_USER_QUOTE_REQUIRED")
        return quote

    def adopt(self, args, sources, instance, item):
        from backend.core.loop.task_contract import get_task_contract
        quote = self._adoption_quote(args, sources)
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
