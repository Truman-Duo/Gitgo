"""Mount/rebuild effective tools at committed capability boundaries."""
from .protocol_tools.registry import build_internal_tools
from .tool_surface import _select_authorized_tools
from .prompt_admission import _refresh_compiled_prompt


class ToolSurfaceRuntime:
    def __init__(self, process, dispatcher, provider, workspace, frontend):
        self.process, self.dispatcher, self.provider = process, dispatcher, provider
        self.workspace, self.frontend = workspace, frontend
        self.available = dispatcher._executors if dispatcher else {}
        self.tools = {}
        self.known_tools = {}
        self.ctx = None

    def rebuild(self):
        internal = build_internal_tools(self.process, self.available, self.workspace,
            self.provider, self.dispatcher, self.frontend)
        self.known_tools = {**self.available, **self.process.dynamic_tools, **internal}
        self.tools, unavailable = _select_authorized_tools(self.process, self.known_tools)
        if self.ctx is not None:
            self.ctx.artifacts['tool_catalog'] = self.tools
        return unavailable

    def discovery(self, active_names, *, excluded=()):
        from .tool_surface import admission_snapshot
        known = set(self.known_tools).difference(excluded)
        return admission_snapshot(self.process, active_names, known)

    def refresh_prompt(self):
        _refresh_compiled_prompt(self.process.session, self.process, self.tools, self.workspace)

    def refresh_after(self, results, task_kind_changed, guard):
        if not task_kind_changed and not any(r.tool_name in {
            'declare_task_contract', 'request_self_execute', 'define_tool', 'author_tool',
            'delegate_task', 'delegate_task_dag', 'delegate_task_bundle', 'promote_agent_changes'
        } and not r.is_error for r in results):
            return []
        previous = set(self.tools)
        unavailable = self.rebuild()
        if unavailable:
            return unavailable
        guard.reset_tool_errors(set(self.tools).difference(previous))
        self.refresh_prompt()
        return []
