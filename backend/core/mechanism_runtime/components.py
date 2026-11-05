"""Registered building blocks, independent of user-selected mode names."""
from .contracts import Element, Port
from dataclasses import replace
from .runner import NodeResult

BOUNDARIES = ("before_tool", "after_tool", "completion", "before_context", "after_compaction", "restored", "discussion_candidate")


def _event(ctx, params, inputs):
    if ctx.boundary not in params["boundaries"]:
        return NodeResult()
    event = inputs["event"]
    effect = event.get("effect", (event.get("receipt") or {}).get("effect"))
    if params["effects"] and effect is not None and effect not in params["effects"]:
        return NodeResult()
    return NodeResult({"event": inputs["event"], "go": True})


def _judge(ctx, params, inputs):
    judgment = ctx.call("agent.judge", {"criterion": params["criterion"], "boundary": ctx.boundary,
        "event": inputs["event"], "prior_state": ctx.state})
    return NodeResult({"matches": judgment["matches"], "evidence": judgment}, {"last_judgment": judgment})


def _context(ctx, params, inputs):
    projected = ctx.call("context.project", {"content": params["content"], "include_event": params["include_event"], "event": inputs.get("event", {})})
    return NodeResult({"projected": True}, {"projection": projected})


def _checkpoint(ctx, params, inputs):
    checkpoint = ctx.call("interaction.checkpoint", {**params, "event": inputs["event"]})
    return NodeResult({"checkpoint": checkpoint})


def _compare(ctx, params, inputs):
    result = ctx.call("interaction.compare", inputs["event"])
    return NodeResult({"comparison": result})


def _return(ctx, params, inputs):
    result = ctx.call("completion.return", {**params, "evidence": inputs.get("evidence", {})})
    return NodeResult({"transition": result})


def _review(ctx, params, inputs):
    result = ctx.call("agent.review", {**params, "event": inputs["event"]})
    return NodeResult({"changes_required": result["verdict"] == "changes_required", "evidence": result})


def _inbox(ctx, params, inputs):
    return NodeResult({"candidate": ctx.call("inbox.add", {"event": inputs["event"]})})


def _material(ctx, params, inputs):
    return NodeResult({"attachment": ctx.call("question.attach", {**params, **inputs})})


def _any(ctx, params, inputs):
    return NodeResult({"go": True})


def _evidence_versions(registry, element):
    """Keep saved v1 interfaces, while new graphs use distinct proof ports.

    The original broad record interface stays callable for pinned definitions;
    a nominal port change is an explicit v2, never a same-version mutation.
    """
    registry.register(element)
    def ports(values):
        return tuple(Port(p.name, 'evidence' if p.name == 'evidence' else p.type_id, p.required) for p in values)
    registry.register(replace(element, version=2, inputs=ports(element.inputs), outputs=ports(element.outputs),
        description=element.description + ' V2 distinguishes judgment/review evidence from lifecycle event data; bind the original event to event inputs.'))


def install(registry):
    for name, schema in {"record": {"type": "object"}, "evidence": {"type": "object"}, "boolean": {"type": "boolean"}, "trigger": {"const": True}}.items():
        registry.register_type(name, schema)
    registry.register(Element("control.any", 1, {"type": "object", "additionalProperties": False},
        tuple(Port(name, "trigger", False) for name in ("a", "b", "c", "d")), (Port("go", "trigger"),), BOUNDARIES, _any,
        description="Join alternate trigger branches. Runs once when at least one bound input is present; no loop or implicit retry."))
    registry.register(Element("inbox.collect", 1, {"type": "object", "additionalProperties": False},
        (Port("go", "trigger"), Port("event", "record")), (Port("candidate", "record"),), ("discussion_candidate",), _inbox,
        effect="control", capabilities=("inbox.add",),
        description="Keep a genuine completed read-only discussion as a task-bound candidate, without adopting it or creating execution obligations. Deduplicated by immutable source."))
    _evidence_versions(registry, Element("question.attach", 1, {"type": "object", "properties": {
        "decision_id": {"type": "string", "maxLength": 128, "default": ""}}, "additionalProperties": False},
        (Port("go", "trigger"), Port("event", "record"), Port("evidence", "record", False)),
        (Port("attachment", "record"),), ("discussion_candidate",), _material,
        effect="control", capabilities=("question.attach",),
        description="Attach this immutable discussion as supplementary material to a current-task question. Use an explicit real decision_id or exactly one valid id in Agent evidence_refs; never answer, approve or revive a superseded question."))
    registry.register(Element("runtime.event", 1,
        {"type": "object", "properties": {"boundaries": {"type": "array", "items": {"enum": list(BOUNDARIES)},
            "minItems": 1, "uniqueItems": True, "default": ["before_tool"]},
            "effects": {"type": "array", "items": {"enum": ["read", "external_read", "workspace_write", "process", "external"]},
                        "uniqueItems": True, "default": []}}, "additionalProperties": False},
        (Port("event", "record"),), (Port("event", "record", False), Port("go", "trigger", False)), BOUNDARIES, _event, entry=True,
        scopes=("task", "session", "project"), boundary_parameter="boundaries",
        description="Emit trusted lifecycle event data and a trigger at subscribed boundaries; optional exact effect-category filtering."))
    _evidence_versions(registry, Element("agent.condition", 1,
        {"type": "object", "properties": {"criterion": {"type": "string", "minLength": 1, "maxLength": 3000}},
         "required": ["criterion"], "additionalProperties": False},
        (Port("go", "trigger"), Port("event", "record")), (Port("matches", "boolean"), Port("evidence", "record")), BOUNDARIES, _judge,
        capabilities=("agent.judge",), effect="projection", timeout_seconds=120, scopes=("task", "session", "project"),
        description="An Agent judges the supplied event and evidence against a criterion. The boolean and evidence are fallible judgments, never authorization."))
    registry.register(Element("context.project", 1,
        {"type": "object", "properties": {"content": {"type": "string", "minLength": 1, "maxLength": 4000},
         "include_event": {"type": "boolean", "default": False}}, "required": ["content"], "additionalProperties": False},
        (Port("go", "trigger"), Port("event", "record", False)), (Port("projected", "trigger"),), BOUNDARIES, _context,
        effect="projection", capabilities=("context.project",), scopes=("task", "session", "project"),
        description="Upsert task-bound guidance through official model context assembly, without changing permissions or verified facts."))
    registry.register(Element("interaction.checkpoint", 1,
        {"type": "object", "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 2000},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            "hint": {"type": "string", "minLength": 1, "maxLength": 2000},
            "hint_label": {"type": "string", "default": "Give a hint", "minLength": 1},
            "skip_label": {"type": "string", "default": "Skip this checkpoint", "minLength": 1},
            "exit_label": {"type": "string", "default": "Disable this extension", "minLength": 1},
            "max_hints": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3}},
         "required": ["question", "reason", "hint"], "additionalProperties": False},
        (Port("go", "trigger"), Port("event", "record")), (Port("checkpoint", "record"),), ("before_tool",), _checkpoint,
        effect="control", capabilities=("interaction.checkpoint",), writes=("interaction.checkpoint",),
        description="Pause the exact proposed invocation with a genuine question card. Hint keeps waiting, skip releases this checkpoint only, and disable suspends this extension; normal tool permissions still apply."))
    comparison = Element("interaction.compare", 1, {"type": "object", "additionalProperties": False},
        (Port("event", "record"),), (Port("comparison", "record"),), ("after_tool",), _compare,
        effect="projection", capabilities=("interaction.compare",),
        description="Link a genuine checkpoint prediction with the same invocation's actual receipt, and project both for the main Agent to compare and explain.")
    registry.register(comparison)
    registry.register(replace(comparison, version=2, description=
        "Observe an after_tool event and compare only when this same instance has an answered checkpoint for the exact invocation unit. "
        "With no answered matching checkpoint, return available=false and emit no projection; unrelated operations do not create comparisons. "
        "A matched prediction and actual receipt are projected once, then that checkpoint becomes compared. "
        "No separate Agent condition or trigger port is needed to enforce this identity guard. This grants no permission."))
    _evidence_versions(registry, Element("completion.return", 1,
        {"type": "object", "properties": {"max_returns": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
            "instruction": {"type": "string", "minLength": 1, "maxLength": 3000},
            "question": {"type": "string", "minLength": 1, "maxLength": 2000},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000}},
         "required": ["instruction", "question", "reason"], "additionalProperties": False},
        (Port("go", "trigger"), Port("evidence", "record", False)), (Port("transition", "record"),), ("completion",), _return,
        effect="control", capabilities=("completion.return",), writes=("completion.transition",),
        description="Return a completion attempt to the existing execution path a bounded number of times, then ask the user; original mandatory completion checks remain."))
    _evidence_versions(registry, Element("agent.review", 1,
        {"type": "object", "properties": {"instructions": {"type": "string", "minLength": 1, "maxLength": 4000},
            "max_steps": {"type": "integer", "minimum": 2, "maximum": 30, "default": 12}},
         "required": ["instructions"], "additionalProperties": False},
        (Port("go", "trigger"), Port("event", "record")), (Port("changes_required", "boolean"), Port("evidence", "record")),
        ("completion",), _review, effect="control", capabilities=("agent.review",), timeout_seconds=300,
        description="Launch the existing owned read-only Reviewer lifecycle for this result and evidence, returning its structured verdict with reviewer identity and generation."))
    # Trusted extension registration; implementations use the same public SDK.
    from .extensions.decision_recovery import install as install_decision_recovery
    install_decision_recovery(registry)
    from .extensions.context_audit import install as install_context_audit
    install_context_audit(registry)
    from .extensions.receipt_audit import install as install_receipt_audit
    install_receipt_audit(registry)
    from .extensions.question_audit import install as install_question_audit
    install_question_audit(registry)
    from .extensions.material_audit import install as install_material_audit
    install_material_audit(registry)
