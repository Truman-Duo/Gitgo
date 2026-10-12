"""Live policy snapshots and synchronous authorization, not intent inference."""
from .legacy_policy import _policy_pre_check
from .signal_bus import SignalBus
from .signals import normalize_governance_signals


class GovernanceRuntime:
    def __init__(self, process, session, workspace_path, emit):
        self.process, self.session, self.workspace_path, self.emit = process, session, workspace_path, emit
        self.version = -1
        self.bus = None
        self.refresh('admission')
        if self.bus is not None:
            result = self.bus.dispatch(self.signals, process, context='pre_dispatch')
            for suggestion in result.suggestions[:3]:
                session.append_user(f'[治理建议] {suggestion}')

    def refresh(self, phase='turn_boundary'):
        current, version = self.process.read_context_snapshot()
        if version == self.version:
            return
        self.harness, self.version = current, version
        raw = current.get('signals')
        self.signals = normalize_governance_signals(raw) if raw is not None else None
        if self.signals is not None and self.bus is None:
            self.bus = SignalBus.from_contract(self.workspace_path) if self.workspace_path else SignalBus()
        self.emit({'event': 'governance_snapshot', 'governance_version': version,
                   'signal_count': len(self.signals or []), 'phase': phase}, detail=current)

    def authorize(self, tool_name, tool_args):
        self.refresh()
        if self.signals is not None and self.bus is not None:
            return self.bus.check_tool(tool_name, tool_args, self.process, signals=self.signals)
        return _policy_pre_check(tool_name, tool_args, self.process)

    def retention_suggestions(self):
        if self.signals is None or self.bus is None:
            return None
        return self.bus.dispatch(self.signals, self.process, context='retention').suggestions or None

    @property
    def active_bus(self):
        return self.bus if self.signals is not None else None
