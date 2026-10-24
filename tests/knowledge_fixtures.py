"""Declared protocol/approval fixtures, never live user-confirmation evidence."""
import json


def fields(refs):
    return {"information_kind": "knowledge", "source_signal_ids": refs,
        "qualification": {"knowledge_type": "project_constraint",
            "conditions": "The explicitly configured fixture project only",
            "validation": "Fixture validation result; not a production truth claim",
            "novelty": "A private fixture constraint not already installed in this isolated store",
            "limitations": "No other project or dependency release is covered"}}


def qualified_response(messages, candidates):
    refs = []
    for line in messages[0]["content"].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("signal_id"):
            refs.append(value["signal_id"])
    result = []
    for item in candidates:
        if isinstance(item, dict) and isinstance(item.get("trigger", ""), str):
            result.append({**fields(refs), **item})
        else:
            result.append(item)
    return json.dumps(result, ensure_ascii=False)


def review_fixture(lesson):
    from backend.core.knowledge.review import mark_reviewed
    return mark_reviewed(lesson, kind="explicit_verification", reference="declared-test-fixture")
