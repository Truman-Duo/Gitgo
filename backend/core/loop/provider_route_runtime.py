"""One live model route, context window and current request envelope."""
from .capability_negotiation import apply_provider_runtime, record_provider_changed
from .context_window import ContextWindow
from .turn_guidance import _build_current_turn_envelope


class ProviderRouteRuntime:
    def __init__(self, process, session, provider, route, window, refresh_prompt):
        self.process, self.session, self.provider = process, session, provider
        self.route, self.window, self.refresh_prompt = route, window, refresh_prompt
        self.tools_param = None
        self.discovery = None
        self.envelope = ''

    def prepare_envelope(self, tools_param, discovery=None):
        self.tools_param = tools_param
        if discovery is not None:
            self.discovery = discovery
        self.envelope = _build_current_turn_envelope(self.process, tools_param, self.discovery)

    def selected(self, artifact):
        if artifact.get('event') != 'provider_route_selected':
            return
        self.route = str(artifact['route_key'])
        changed = apply_provider_runtime(self.process, provider_id=artifact['provider_id'], route_key=self.route,
            model_id=artifact['model'], protocol=artifact['protocol'], capabilities=artifact['capabilities'],
            hosted_search_transport=artifact.get('hosted_search_transport', 'unavailable'))
        self.session.bind_provider_route(self.route)
        self.session.model_context_limit = int(artifact['context_window'])
        self.window = ContextWindow(self.session.model_context_limit)
        if changed:
            self.refresh_prompt()
            record_provider_changed(self.session, model_id=self.process.model_id,
                protocol=str(artifact['protocol']), provider_route=self.route)
        self.prepare_envelope(self.tools_param)

    def alignment_notice(self, artifact, initial_route, emit, notice):
        if artifact.get('event') == 'generation_profile_fallback':
            notice('ALIGNMENT_GENERATION_FALLBACK', '模型接口不支持辅助检查的低推理配置；已明确回退到接口默认值。')
        self.selected(artifact)
        if artifact.get('event') == 'provider_route_activated' and hasattr(self.provider, 'activate'):
            artifact = dict(self.provider.activate(self.session, artifact['provider_id'],
                initial_route=initial_route, attempt=artifact.get('attempt', 1)).artifact)
        emit({**artifact, 'phase': 'alignment'})
