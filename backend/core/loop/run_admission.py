"""Composition root for one admitted Agent run; controllers retain their duties."""
from dataclasses import dataclass
import time
from .capability_negotiation import apply_provider_runtime, record_provider_changed
from .context_window import ContextWindow
from .engineering_workflow import EngineeringWorkflow
from .event_bus import EventBus
from .execution_context import ExecutionContext
from .governance_runtime import GovernanceRuntime
from .lifecycle import transition
from .loop_guard import LoopGuard
from .models import ProcessStatus
from .provider_route_runtime import ProviderRouteRuntime
from .runtime_outcomes import _error_result, _status_result
from .tool_surface_runtime import ToolSurfaceRuntime
from .transcript import TaskTranscriptBuilder
from .turn_observations import TurnObservations


@dataclass(frozen=True)
class RunServices:
    surface: ToolSurfaceRuntime
    context: ExecutionContext
    guard: LoopGuard
    route: ProviderRouteRuntime
    governance: GovernanceRuntime
    observations: TurnObservations
    started_wall: float


@dataclass(frozen=True)
class RunAdmission:
    services: RunServices | None = None
    result: dict | None = None


def _start_process(process):
    if process.session is None:
        transition(process, ProcessStatus.FAILED)
        return _error_result(process, 'NO_SESSION', 'Agent runtime has no session')
    from .completion_control import execute_control
    control = execute_control(process)
    if control is not None:
        process.result = control
        return control
    if process.cancel_requested:
        transition(process, ProcessStatus.CANCELLED)
        return _status_result(process, process.session)
    if process.status == ProcessStatus.WAITING:
        transition(process, ProcessStatus.RUNNING)
    if process.status != ProcessStatus.RUNNING:
        return _status_result(process, process.session)
    return None


def _bind_provider(process, provider):
    session = process.session
    client = provider.bind_session(session) if hasattr(provider, 'bind_session') else provider
    route = str(getattr(client, 'route_key', '') or '')
    changed = session.bind_provider_route(route)
    apply_provider_runtime(process, provider_id=getattr(client, 'provider_id', process.provider_id),
        route_key=route, model_id=getattr(client, '_model', process.model_id), protocol=getattr(client, 'protocol', 'unknown'),
        capabilities=getattr(client, 'capabilities', None), hosted_search_transport=getattr(client, 'hosted_search_transport', 'unavailable'))
    return route, changed


def _create_tool_context(process, surface, observations, workspace):
    bus = EventBus(on_error=observations.failed)
    ctx = ExecutionContext(process=process, session=process.session, workspace_path=workspace,
        event_bus=bus, cancellation=process.cancellation_event, storage=process.bound_storage)
    surface.ctx = ctx
    ctx.artifacts['tool_catalog'] = surface.tools
    frontend = observations.publish if observations.frontend is not None else None
    ctx.artifacts['engineering_workflow'] = EngineeringWorkflow(process, frontend)
    transcript = TaskTranscriptBuilder(task_id=process.process_id)
    observations.wire(bus, transcript)
    process._transcript_builder = transcript
    return ctx


def admit_agent_run(process, provider, instruction, dispatcher, workspace, frontend):
    terminal = _start_process(process)
    if terminal is not None:
        return RunAdmission(result=terminal)
    session = process.session
    provider_route, changed = _bind_provider(process, provider)
    process._turn_started_monotonic = time.monotonic()
    started = time.time()
    observations = TurnObservations(process, frontend)
    routed_frontend = observations.publish if frontend is not None else None
    surface = ToolSurfaceRuntime(process, dispatcher, provider, workspace, routed_frontend)
    try:
        from .dynamic_mount import restore_pending_definitions
        from .recovery import resolve_clean_recovery
        restore_pending_definitions(process, surface.available)
        resolve_clean_recovery(process)
    except (ValueError, KeyError, PermissionError, OSError) as exc:
        transition(process, ProcessStatus.FAILED)
        return RunAdmission(result=_error_result(process, 'COMPOSITE_RECOVERY_BLOCKED',
            'The saved invocation cannot safely resume; committed effects are retained. ' + str(exc)))
    unavailable = surface.rebuild()
    if unavailable:
        transition(process, ProcessStatus.FAILED)
        return RunAdmission(result=_error_result(process, 'CAPABILITY_TOOL_UNAVAILABLE',
            'Capability references unavailable tools: ' + ', '.join(unavailable)))
    ctx = _create_tool_context(process, surface, observations, workspace)
    surface.refresh_prompt()
    session.model_context_limit = max(1024, int(getattr(provider, 'context_window', 128000)))
    route = ProviderRouteRuntime(process, session, provider, provider_route,
        ContextWindow(session.model_context_limit), surface.refresh_prompt)
    if changed:
        record_provider_changed(session, model_id=process.model_id,
            protocol=str(process.runtime_preferences.get('provider_protocol') or 'unknown'), provider_route=provider_route)
        observations.publish({'event': 'provider_route_changed', 'provider_route': provider_route,
                              'continuation_policy': 'public_history_only'})
    if instruction:
        session.append_user(instruction, message_type='conversation')
    elif not session.messages:
        transition(process, ProcessStatus.FAILED)
        return RunAdmission(result=_error_result(process, 'EMPTY_INSTRUCTION', 'Agent task has no instruction'))
    governance = GovernanceRuntime(process, session, workspace, observations.publish)
    ctx.tool_authorizer = governance.authorize
    return RunAdmission(services=RunServices(surface, ctx, LoopGuard(), route, governance, observations, started))
