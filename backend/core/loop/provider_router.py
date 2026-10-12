"""Ordered failover with lease-based circuit recovery and route-safe history.

Adapts cc-switch's queue and circuit design to Gitgo's session/event protocol.
Configuration is authority; request routing never changes the active setting.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import copy
import hashlib
import logging
import threading
import time

from .llm import LLMProvider, StreamCancelledError, StreamInterruptedError
from .provider_policy import failover_settings, provider_endpoint
from .provider_protocol import ProviderEvent, ProviderEventType, deterministic_hash
from .provider_transport import LocalProviderTransport, ProviderHttpError


class ProviderRoutesUnavailable(RuntimeError):
    def __init__(self, attempts):
        self.attempts = list(attempts)
        super().__init__("No configured provider route is available: " + ", ".join(
            f"{a['provider_id']} ({a['reason']})" for a in self.attempts))


@dataclass(frozen=True)
class Permit:
    generation: int
    probe: bool


class CircuitBreaker:
    def __init__(self, settings=None, *, clock=time.monotonic):
        self.settings = failover_settings({"circuit_breaker": settings or {}})
        self.clock = clock
        self.lock = threading.RLock()
        self.state = "closed"
        self.generation = 0
        self.failures = self.successes = 0
        self.opened_at = 0.0
        self.probe_inflight = False
        self.samples = deque(maxlen=self.settings["window_size"])

    def acquire(self):
        with self.lock:
            if self.state == "open":
                if self.clock() - self.opened_at < self.settings["cooldown_seconds"]:
                    return None
                self.state = "half_open"
                self.generation += 1
                self.successes = 0
            probe = self.state == "half_open"
            if probe and self.probe_inflight:
                return None
            if probe:
                self.probe_inflight = True
            return Permit(self.generation, probe)

    def finish(self, permit, success: bool | None):
        with self.lock:
            if permit.generation != self.generation:
                return  # a stale completion cannot heal a newer breaker
            if permit.probe:
                self.probe_inflight = False
            if success is None:
                return
            self.samples.append(success)
            if success:
                self.failures = 0
                if self.state == "half_open":
                    self.successes += 1
                    if self.successes >= self.settings["success_threshold"]:
                        self.state = "closed"
                        self.generation += 1
                        self.samples.clear()
            else:
                self.failures += 1
                failed_rate = 1 - sum(self.samples) / len(self.samples)
                if (self.state == "half_open" or self.failures >= self.settings["failure_threshold"]
                        or (len(self.samples) >= self.settings["min_requests"]
                            and failed_rate >= self.settings["error_rate_threshold"])):
                    self.state = "open"
                    self.opened_at = self.clock()
                    self.generation += 1
                    self.probe_inflight = False
                    self.successes = 0

    def status(self):
        with self.lock:
            return {"breaker_state": self.state, "failures": self.failures,
                    "samples": len(self.samples), "failed_requests": len(self.samples) - sum(self.samples),
                    "probe_inflight": self.probe_inflight,
                    "retry_in_seconds": max(0, round(self.settings["cooldown_seconds"] - (self.clock() - self.opened_at), 2)) if self.state == "open" else 0,
                    "health_supported": True}


class BreakerRegistry:
    """One owning Daemon shares health across foreground, BTW and children."""
    def __init__(self):
        self.lock = threading.RLock()
        self.entries = {}

    def get(self, key, settings):
        with self.lock:
            if key not in self.entries:
                # Configuration changes produce another key; an in-flight old
                # result retains its own object rather than affecting new state.
                if len(self.entries) >= 128:
                    self.entries.pop(next(iter(self.entries)))
                self.entries[key] = CircuitBreaker(settings)
            return self.entries[key]


_REGISTRY = BreakerRegistry()


def failure_kind(error):
    if isinstance(error, StreamCancelledError):
        return False, "cancelled"
    if isinstance(error, StreamInterruptedError) and error.is_context_overflow:
        return False, "context_overflow"
    cause = error
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        status = getattr(cause, "status_code", None)
        if status is not None:
            return status in {401, 402, 403, 404, 408, 429} or status >= 500, f"HTTP_{status}"
        cause = cause.__cause__
    if isinstance(error, StreamInterruptedError):
        return bool(error.retryable), "transport_failure"
    if isinstance(error, RuntimeError) and ("connection failed" in str(error).lower() or "stream failed" in str(error).lower()):
        return True, "transport_failure"
    return False, "request_error"


def runtime_client(configured, *, transport=None):
    endpoint = provider_endpoint(configured.base_url, kind=configured.kind, auth_mode=configured.auth_mode)
    key = configured.api_key if configured.auth_mode == "api_key" else ""
    if configured.auth_mode == "api_key" and not key:
        raise ValueError("Provider API key is unavailable")
    client = LLMProvider(endpoint, key, configured.model_id,
                       protocol=configured.protocol, capabilities=configured.runtime_capabilities(),
                       transport=transport or (LocalProviderTransport() if configured.kind == "local" else None))
    from backend.core.tools.web_tools import provider_search_transport
    client.provider_id = configured.id
    client.hosted_search_transport = provider_search_transport(configured)
    return client


class RoutedProvider:
    supports_generation_purpose = True
    def __init__(self, config, *, registry=None, client_factory=runtime_client):
        from backend.core.llm_config import LLMProvider as ConfiguredProvider
        self.config = copy.deepcopy(config)
        self.settings = failover_settings(config)
        self.registry = registry or _REGISTRY
        self.local = threading.local()
        self.notices = deque(maxlen=32)
        self.notice_lock = threading.RLock()
        configured = {p['id']: ConfiguredProvider.from_dict(p) for p in config.get('providers', [])}
        primary = str(config.get('active_provider') or '')
        if primary not in configured:
            raise ValueError("The active provider is missing")
        order = [primary, *config.get('failover_order', [])]
        self.order = list(dict.fromkeys(order))
        if len(self.order) > 9 or any(p not in configured for p in self.order):
            raise ValueError("Invalid backup provider queue")
        self.providers = configured
        self.clients = {}
        self.configuration_errors = {}
        self.breakers = {}
        for provider_id in self.order:
            provider = configured[provider_id]
            try:
                client = client_factory(provider)
                self.clients[provider_id] = client
                # Secret rotation resets health without disclosing its identity.
                fingerprint = deterministic_hash({'id': provider_id, 'route': client.route_key,
                    'credential_version': hashlib.sha256(provider.api_key.encode()).hexdigest(),
                    'settings': self.settings, 'capabilities': provider.runtime_capabilities()})
                self.breakers[provider_id] = self.registry.get(fingerprint, self.settings)
            except ValueError:
                self.configuration_errors[provider_id] = 'configuration_unavailable'
        if primary not in self.clients:
            # A missing primary credential is a visible failed candidate, not a
            # reason to silently disable the explicitly configured backups.
            if not self.clients:
                raise ValueError("No usable provider configuration")

    @property
    def current(self):
        return self.clients[getattr(self.local, 'provider_id', next(iter(self.clients)))]

    def bind_session(self, session):
        """Restore this worker's projection from durable, session-scoped state.

        Worker thread identity is not conversation identity. Do not reset a
        resumed conversation to the configured primary before routing a call.
        """
        effective_route = session.active_provider_route
        for item in reversed(session.host_ledger):
            if item.get('event') == 'provider_route_activated':
                effective_route = item.get('route_key', effective_route)
                break
        for provider_id, client in self.clients.items():
            if client.route_key == effective_route:
                self.local.provider_id = provider_id
                return client
        self.local.provider_id = next(iter(self.clients))
        return self.current

    def activate(self, session, provider_id, *, initial_route='', attempt=1):
        client = self.clients[provider_id]
        previous = initial_route
        committed = False
        if session is not None:
            for item in reversed(session.host_ledger):
                if item.get('event') == 'provider_route_activated':
                    previous = item.get('route_key', '')
                    committed = True
                    break
        record = {'provider_id': provider_id, 'route_key': client.route_key,
                  'model': client._model, 'attempt': attempt,
                  'route_changed': previous != client.route_key if committed else
                      (attempt > 1 or bool(previous and previous != client.route_key)),
                  'message': f"Using {self.providers[provider_id].name}"}
        if session is not None and (not committed or previous != client.route_key):
            session.host_ledger.append({'event': 'provider_route_activated', **record})
        return self.notice('provider_route_activated', **record)

    @property
    def route_key(self):
        return self.current.route_key

    @property
    def protocol(self):
        return self.current.protocol

    @property
    def capabilities(self):
        return self.current.capabilities

    @property
    def _model(self):
        return self.current._model

    @property
    def context_window(self):
        return self.current.context_window

    @property
    def max_output_tokens(self):
        return self.current.max_output_tokens

    def status(self):
        return [{"id": p, **(self.breakers[p].status() if p in self.breakers else
                              {"breaker_state": "unavailable", "health_supported": True}),
                 "configuration_error": self.configuration_errors.get(p)} for p in self.order]

    def notice(self, event, **artifact):
        record = {'event': event, **artifact}
        with self.notice_lock:
            self.notices.append(record)
        if event in {'provider_route_failed', 'provider_route_skipped'}:
            logging.getLogger(__name__).warning("Provider routing: %s", record)
        return ProviderEvent(ProviderEventType.ROUTING_NOTICE, artifact=record)

    def stream_session_events(self, session, *, current_turn_envelope='', tools=None,
                              cancel_event=None, cache_intent=None, metadata=None, timeout=120,
                              max_tokens=4096, before_attempt=None):
        attempts = []
        initial_route = session.active_provider_route
        for provider_id in self.order:
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelledError()
            client = self.clients.get(provider_id)
            reason = self.configuration_errors.get(provider_id)
            if client is not None and tools and not client.capabilities.tools:
                reason = 'tools_unsupported'
            if client is not None and session.estimate_tokens() + min(max_tokens, client.max_output_tokens) > client.context_window:
                reason = 'context_capacity_insufficient'
            breaker = self.breakers.get(provider_id)
            permit = None if reason else breaker.acquire()
            if permit is None:
                reason = reason or 'circuit_unavailable'
                attempts.append({'provider_id': provider_id, 'reason': reason})
                yield self.notice('provider_route_skipped', provider_id=provider_id, reason=reason)
                continue
            outcome = None
            iterator = None
            observed = False
            try:
                self.local.provider_id = provider_id
                previous_route = session.active_provider_route
                session.bind_provider_route(client.route_key)
                yield self.notice('provider_route_selected', provider_id=provider_id,
                                  model=client._model, protocol=client.protocol.value,
                                  route_key=client.route_key, previous_route=previous_route,
                                  context_window=client.context_window, max_output_tokens=client.max_output_tokens,
                                  capabilities=client.capabilities.to_dict(), attempt=len(attempts) + 1,
                                  hosted_search_transport=getattr(client, 'hosted_search_transport', 'unavailable'),
                                  message=f"Using {self.providers[provider_id].name}")
                route_metadata = dict(metadata or {})
                route_metadata['hosted_web_search'] = bool(
                    route_metadata.get('web_search_mode') in {'auto', 'provider'}
                    and client.capabilities.hosted_web_search)
                iterator = client.stream_events(
                    session.to_provider_messages(dynamic_envelope=(current_turn_envelope() if callable(current_turn_envelope)
                        else current_turn_envelope), provider_route=client.route_key),
                    tools=tools, cancel_event=cancel_event,
                    cache_intent=session.cache_intent(tools, mode=client.capabilities.prompt_cache),
                    metadata=route_metadata, timeout=timeout, max_tokens=max_tokens, max_attempts=1,
                    before_request=lambda: before_attempt(len(attempts), client) if before_attempt else timeout)
                terminal = False
                for event in iterator:
                    if event.type == ProviderEventType.ROUTING_NOTICE:
                        event.artifact = {**dict(event.artifact or {}), 'provider_id': provider_id}
                    # Even response_started may represent an upstream action.
                    # Once any native event is observed, switching is explicit
                    # Host recovery, never transparent request replay.
                    if event.type not in {ProviderEventType.PROVIDER_CAPABILITY_FALLBACK, ProviderEventType.ROUTING_NOTICE}:
                        if not observed:
                            yield self.activate(session, provider_id, initial_route=initial_route, attempt=len(attempts) + 1)
                        observed = True
                    if event.type in {ProviderEventType.RESPONSE_COMPLETED, ProviderEventType.RESPONSE_INCOMPLETE}:
                        terminal = True
                    yield event
                if not terminal:
                    raise StreamInterruptedError()
                outcome = True
                return
            except StreamCancelledError:
                raise
            except Exception as exc:
                provider_failure, reason = failure_kind(exc)
                outcome = False if provider_failure else None
                attempts.append({'provider_id': provider_id, 'reason': reason})
                yield self.notice('provider_route_failed', provider_id=provider_id, reason=reason,
                                  partial_output=observed, message=f"{self.providers[provider_id].name}: {reason}")
                if observed or not provider_failure:
                    raise
            finally:
                try:
                    if iterator is not None:
                        iterator.close()
                finally:
                    breaker.finish(permit, outcome)
        raise ProviderRoutesUnavailable(attempts)

    def stream_events(self, messages, **kwargs):
        """Bridge canonical non-session callers without borrowing native state."""
        from .session import AgentSession
        session = AgentSession()
        session.messages = copy.deepcopy(messages)
        session.active_provider_route = self.route_key
        for index, message in enumerate(session.messages):
            state = message.pop('provider_state', None)
            if state:
                state_id = f'routing-bridge-{index}'
                message['provider_state_id'] = state_id
                state.setdefault('provider_route', self.route_key)
                session.provider_state[state_id] = state
        yield from self.stream_session_events(session, **kwargs)

    def assess_alignment(self, payload, **kwargs):
        from .alignment_runtime import assess_with_provider
        return assess_with_provider(self, payload, **kwargs)

    def chat(self, messages, *, max_tokens=4096, tools=None, cancel_event=None,
             timeout=120, preserve_provider_state=False, session=None,
             current_turn_envelope='', before_attempt=None, on_notice=None, **kwargs):
        # Non-session callers may already contain native state. Do not move
        # such a payload to another provider; stream_session_events reconstructs
        # route-safe history from the authoritative session instead.
        scoped = session is None and any(m.get('provider_state') or m.get('tool_calls') or m.get('reasoning_details') for m in messages)
        attempts = []
        initial_route = session.active_provider_route if session is not None else ''
        def notify(event, **artifact):
            notice = self.notice(event, **artifact)
            if on_notice is not None:
                on_notice(dict(notice.artifact))
        order = [getattr(self.local, 'provider_id', self.order[0])] if scoped else self.order
        for provider_id in order:
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelledError()
            if provider_id not in self.clients:
                attempts.append({'provider_id': provider_id, 'reason': 'configuration_unavailable'})
                notify('provider_route_skipped', provider_id=provider_id, reason='configuration_unavailable')
                continue
            client = self.clients[provider_id]
            if tools and not client.capabilities.tools:
                attempts.append({'provider_id': provider_id, 'reason': 'tools_unsupported'})
                notify('provider_route_skipped', provider_id=provider_id, reason='tools_unsupported')
                continue
            breaker = self.breakers[provider_id]
            permit = breaker.acquire()
            if permit is None:
                attempts.append({'provider_id': provider_id, 'reason': 'circuit_unavailable'})
                notify('provider_route_skipped', provider_id=provider_id, reason='circuit_unavailable')
                continue
            outcome = None
            try:
                self.local.provider_id = provider_id
                previous_route = session.active_provider_route if session is not None else ''
                if session is not None:
                    session.bind_provider_route(client.route_key)
                notify('provider_route_selected', provider_id=provider_id,
                       model=client._model, protocol=client.protocol.value,
                       route_key=client.route_key, previous_route=previous_route,
                       context_window=client.context_window, max_output_tokens=client.max_output_tokens,
                       capabilities=client.capabilities.to_dict(), attempt=len(attempts) + 1,
                       hosted_search_transport=getattr(client, 'hosted_search_transport', 'unavailable'),
                       message=f"Using {self.providers[provider_id].name}")
                request_timeout = before_attempt(len(attempts), client) if before_attempt else timeout
                notify('provider_request_started', provider_id=provider_id, model=client._model,
                       protocol=client.protocol.value, route_key=client.route_key,
                       idle_timeout_seconds=request_timeout)
                route_messages = session.to_provider_messages(dynamic_envelope=(current_turn_envelope() if callable(current_turn_envelope)
                    else current_turn_envelope),
                    provider_route=client.route_key) if session is not None else messages
                result = client.chat(route_messages, max_tokens=max_tokens, tools=tools, cancel_event=cancel_event,
                                     timeout=request_timeout, preserve_provider_state=preserve_provider_state, max_retries=0,
                                     purpose=kwargs.get('purpose', ''), on_notice=on_notice,
                                     before_generation_fallback=(lambda: before_attempt(len(attempts), client)) if before_attempt else None)
                outcome = True
                activated = self.activate(session, provider_id, initial_route=initial_route, attempt=len(attempts) + 1)
                if on_notice is not None:
                    on_notice(dict(activated.artifact))
                return result
            except StreamCancelledError:
                raise
            except Exception as exc:
                provider_failure, reason = failure_kind(exc)
                outcome = False if provider_failure else None
                attempts.append({'provider_id': provider_id, 'reason': reason})
                notify('provider_route_failed', provider_id=provider_id, reason=reason,
                       message=f"{self.providers[provider_id].name}: {reason}")
                if scoped or not provider_failure:
                    raise
            finally:
                breaker.finish(permit, outcome)
        raise ProviderRoutesUnavailable(attempts)


def configured_runtime_provider(config=None):
    from backend.core.llm_config import LLMConfigManager, LLMProvider as ConfiguredProvider
    config = config if config is not None else LLMConfigManager.load()
    active = next((p for p in config.get('providers', []) if p['id'] == config.get('active_provider')), None)
    if active is None:
        return None
    if config.get('failover_enabled'):
        return RoutedProvider(config)
    return runtime_client(ConfiguredProvider.from_dict(active))
