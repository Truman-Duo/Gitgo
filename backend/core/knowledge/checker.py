"""Project-scoped declaration changes via the existing real permission card."""
from __future__ import annotations
from pathlib import Path
from .models import Lesson
from .review import content_version,can_consume

TOOL="configure_lesson_checker"


def prepare_change(storage, workspace, args):
    found=storage.get_lesson_record(str(args.get("lesson_id") or ""))
    if found is None:raise ValueError("LESSON_NOT_FOUND")
    lesson=Lesson.from_dict(found[1])
    if lesson.evidence and lesson.evidence.get("scope")=="user_shared":raise ValueError("Use a local reviewed lesson before configuring a project check")
    version=content_version(lesson)
    if args.get("expected_version")!=version:raise ValueError("LESSON_CONTENT_CHANGED")
    if not can_consume(lesson):raise ValueError("LESSON_REVIEW_REQUIRED")
    action=args.get("action")
    if action not in {"enable","disable"}:raise ValueError("action must be enable or disable")
    check=None
    if action=="enable":
        from .applicability import assess_lesson
        if assess_lesson(lesson,workspace).get("state")!="current":raise ValueError("LESSON_EVIDENCE_RECHECK_REQUIRED")
        literal=args.get("violation_literal")
        if not isinstance(literal,str) or not 1<=len(literal)<=256 or not literal.strip():raise ValueError("Use a nonempty violation_literal of at most 256 characters")
        paths=args.get("paths")
        if not isinstance(paths,list) or not 1<=len(paths)<=12:raise ValueError("1..12 exact project-relative paths are required")
        normalized=[]
        root=Path(workspace).resolve()
        for name in paths:
            if not isinstance(name,str) or not name or Path(name).is_absolute():raise ValueError("Invalid checker path")
            path=(root/name).resolve();relative=path.relative_to(root).as_posix()
            if relative==".":raise ValueError("A file path is required")
            normalized.append(relative)
        import re
        check={"authority":"registered","mode":"violation_pattern","pattern":re.escape(literal),
               "literal":literal,"paths":sorted(set(normalized))}
    return {"lesson_id":lesson.id,"expected_version":version,"action":action,
            "project_id":storage.paths.project_id,"check":check,
            "rule":lesson.rule[:2000]}


def request_change(process, storage, workspace, args):
    change=prepare_change(storage,workspace,args)
    from backend.core.loop.agent_tool import AgentTool,ApprovalMode,ToolEffect
    from backend.core.loop.permission_broker import create_permission_request
    tool=AgentTool(name=TOOL,description="Change the exact project's declared lesson check after real user approval.",
        parameters={"type":"object","properties":{}},execute=lambda _:None,
        effect=ToolEffect.PROCESS,approval=ApprovalMode.ASK,approval_per_invocation=True,
        resources=["capability://project-lesson-checkers"],idempotent=True)
    result=create_permission_request(process,{"purpose":(
        f"{change['action']} the project file check for {change['lesson_id']}; rule: {change['rule']}; "
        f"definition: {change['check']}. This checks literal occurrence, not semantic correctness"),
        "tool_name":TOOL,"arguments":change}, {TOOL:tool},str(workspace))
    process.session.host_ledger.append({"event":"lesson_checker_change_requested",
        "decision_id":result["decision_id"],"task_id":process.active_task_id or process.process_id,
        "change":change})
    # This descriptor is produced by this service, not accepted as a model
    # permission flag. The existing broker owns the real card/grant/receipt.
    descriptor=process.pending_decision["permission_request"]
    descriptor["lesson_checker_change"]=change
    approved=next(o for o in process.pending_decision["options"] if o.get("action")=="allow_once")
    approved["immediate_effect"]="Apply this exact project check configuration once."
    approved["downstream_effect"]=("This check remains active for the specified files until disabled or its evidence becomes invalid."
        if change["action"]=="enable" else "The specified check stops restricting future writes; other checks remain unchanged.")
    approved["risks"]=("The literal is matched anywhere in the complete candidate, including comments or quoted strings; it is not a semantic correctness check."
        if change["action"]=="enable" else "This check will no longer prevent its known violation.")
    approved["reversibility"]="Request a separately approved configuration change or revocation."
    process.pending_decision["why_user_must_decide"]="Enabling a check changes which project writes Gitgo may refuse. Approve this exact definition and scope, or leave the configuration unchanged."
    return {"status":"awaiting_user",**process.pending_decision}


def apply_decision(process, pending, action, storage):
    request=pending.get("permission_request") or {}
    change=request.get("lesson_checker_change")
    if request.get("tool_name")!=TOOL or not isinstance(change,dict):raise ValueError("Invalid checker decision")
    from backend.core.loop.permission_broker import consume_exact_grant,arguments_digest
    owned=next((e for e in process.session.host_ledger if e.get("event")=="lesson_checker_change_requested"
                and e.get("decision_id")==pending.get("decision_id")
                and e.get("task_id")== (process.active_task_id or process.process_id)),None)
    if not owned or owned.get("change")!=change:raise ValueError("CHECKER_REQUEST_NOT_BOUND")
    if request.get("arguments_digest")!=arguments_digest(change):raise ValueError("CHECKER_DECISION_CHANGED")
    if action!="allow_once":return {"configured":False,"status":"declined","lesson_id":change["lesson_id"]}
    grant=consume_exact_grant(process,TOOL,change)
    if not grant:raise ValueError("CHECKER_APPROVAL_REQUIRED")
    if change.get("project_id")!=storage.paths.project_id:raise ValueError("CHECKER_PROJECT_CHANGED")
    result=storage.configure_lesson_checker(change,grant_id=grant["grant_id"])
    return {"configured":result["applied"],"status":result["status"],"lesson":result["lesson"],
            "check_active":bool(result["lesson"].get("check")),"requested_action":change["action"],"project_id":storage.paths.project_id}
