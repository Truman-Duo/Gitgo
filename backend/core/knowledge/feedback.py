"""Feedback identity and request exposure facts; never model understanding."""
from __future__ import annotations
import hashlib,json
from backend.core.tools.file_operations import FILE_MUTATION_TOOLS


def identity(candidate, context, diagnostics):
    body={"operation":candidate.operation,"path":str(candidate.path),
          "before":candidate.before_sha256,"after":candidate.after_sha256,
          "owner":{k:context.get(k,"") for k in ("project_name","task_id","process_id","session_id")},
          "lessons":[{k:d.get(k) for k in ("lesson_id","lesson_version","code","applicability")} for d in diagnostics]}
    return "lf_"+hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def exposed_ids(process):
    task=process.active_task_id or process.process_id
    return list(dict.fromkeys(e["feedback_id"] for e in process.session.host_ledger
        if e.get("event")=="lesson_feedback_provider_input" and e.get("task_id")==task
        and e.get("process_id")==process.process_id and e.get("feedback_id")))


def observe_provider_input(process, messages):
    """Observe the actual projected tool text, excluding quarantine/compaction.

    A request input observation is not proof of network receipt or comprehension.
    No IDs are accepted from assistant/user text or model-supplied receipts.
    """
    def strings(value):
        if isinstance(value,str):yield value
        elif isinstance(value,list):
            for v in value:yield from strings(v)
        elif isinstance(value,dict):
            for v in value.values():yield from strings(v)
    task=process.active_task_id or process.process_id
    known=set(exposed_ids(process))
    candidates=[]
    for message in process.session.messages:
        if message.get("role")!="tool" or message.get("provider_visible") is False:continue
        data=message.get("data")
        receipt=message.get("receipt")
        if not isinstance(data,dict) or not isinstance(receipt,dict):continue
        if (receipt.get("tool_name") != message.get("_tool_name")
                or receipt.get("task_id") != task or receipt.get("process_id") != process.process_id):continue
        if message.get("_tool_name") not in FILE_MUTATION_TOOLS and receipt.get("file_service_result") is not True:continue
        feedback=data.get("lesson_feedback")
        if not isinstance(feedback,dict):continue
        identifier=feedback.get("feedback_id")
        if (not identifier or identifier in known or receipt.get("lesson_feedback_id")!=identifier
                or (receipt.get("effect")!="workspace_write" and receipt.get("file_service_result") is not True)):continue
        candidates.append((message,identifier))
    if not candidates:return
    texts=list(strings(messages))
    for message,identifier in candidates:
        content=message.get("content")
        if not content or not any(content==t or content in t for t in texts):continue
        process.session.host_ledger.append({"event":"lesson_feedback_provider_input",
            "feedback_id":identifier,"task_id":task,"process_id":process.process_id,
            "context_epoch":process.session.context_epoch,
            "tool_call_id":message.get("tool_call_id",""),
            "input_sha256":hashlib.sha256(content.encode()).hexdigest(),
            "observation":"included_in_projected_provider_input","provider_received":"unknown",
            "understood":"unknown","adopted":"unknown"})
        known.add(identifier)
