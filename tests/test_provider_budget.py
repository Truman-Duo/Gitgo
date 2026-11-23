import copy
from unittest.mock import patch

import pytest

from backend.core.application.services import ApplicationServices, OperationError
from backend.core.llm_config import LLMConfigManager, LLMProvider
from backend.core.provider_budget import default_context_budget, resolve_profile, resolve_provider
from backend.core.provider_metadata import enrich
from backend.core.loop.provider_router import configured_runtime_provider

OFFICIAL = "https://api.openai.com/v1"
MODEL = "gpt-6.1-terra"


def test_invalid_legacy_endpoint_stays_editable_without_reference_capability_leakage():
    original = [{"id": MODEL, "limits": {}, "source": "manual", "state": "unknown"}]
    assert enrich(original, "https://[invalid/v1", {}) == original
    assert original[0]["limits"] == {}


@pytest.mark.parametrize("capacity,expected",[(None,512000),(128000,128000),(256000,256000),(400000,400000),(800000,512000),(1000000,1000000),(1050000,1000000)])
def test_budget_policy_does_not_invent_a_minimum_model_capacity(capacity, expected):
    assert default_context_budget(capacity) == expected


def test_official_reference_is_exact_endpoint_and_id_scoped_and_service_fields_win():
    model = {"id": MODEL, "name": MODEL, "limits": {}, "source": OFFICIAL + "/models"}
    enriched = enrich([model], OFFICIAL, {})[0]
    assert enriched["limits"] == {"context_window": 1050000, "max_output_capacity": 128000}
    assert enriched["metadata"]["reasoning_efforts"] == ["none","low","medium","high","xhigh","max"]
    assert enriched["metadata"]["reasoning_effort_default"] == "medium"
    assert enriched["metadata"]["source_version"] == "2026-11-07.1"
    assert enriched["metadata"]["state"] == "reference"
    assert model["limits"] == {}
    for endpoint in ["https://compat.example/v1", "http://api.openai.com/v1", OFFICIAL + "/other", "https://api.openai.com:8443/v1"]:
        assert enrich([model], endpoint, {}) == [model]
    declared = {**model, "limits": {"context_window":32000}, "metadata":{"reasoning_efforts":["low"],"source":"service","state":"declared"}}
    actual = enrich([declared], OFFICIAL, {})[0]
    assert actual["limits"]["context_window"] == 32000
    assert actual["metadata"]["reasoning_efforts"] == ["low"]
    assert "reasoning_effort_default" not in actual["metadata"]


def test_only_proven_default_values_migrate_and_resolution_does_not_write():
    original = {"model_id":MODEL, "base_url":OFFICIAL, "context_window":128000, "limits_source":"default"}
    before = copy.deepcopy(original)
    resolved = resolve_provider(original)
    assert original == before
    assert resolved["context_window"] == 1000000
    assert resolved["configuration"]["budget"]["capacity"] == 1050000
    assert resolved["configuration"]["sources"]["context_window"] == "default"
    for source in ["user", "configured", None]:
        raw = {**original, "limits_source":source} if source else {key:value for key,value in original.items() if key != "limits_source"}
        assert resolve_provider(raw)["context_window"] == 128000
    profile = {"model_id":MODEL,"context_window":128000,"sources":{"context_window":"user"}}
    assert resolve_profile(profile, OFFICIAL)["context_window"] == 128000
    legacy = resolve_provider({"model_id":MODEL,"base_url":OFFICIAL,"context_window":128000,"limits_source":"configured"})
    assert legacy["configuration"]["sources"]["context_window"] == "legacy"


def test_manual_model_gets_offline_capabilities_budget_and_effort_without_listing_or_probe():
    service = ApplicationServices()
    with patch("backend.core.provider_metadata.catalogue") as network:
        created = service.provider_save(name="Official",base_url=OFFICIAL,api_key="synthetic",model_id=MODEL,protocol="openai_responses")["provider"]
    network.assert_not_called()
    assert created["context_window"] == 1000000
    assert created["capabilities"] == {}  # reference is not a connectivity/tool verification
    options = service.provider_effort_options()
    assert options["levels"] == ["none","low","medium","high","xhigh","max"]
    assert options["default_effort"] == "medium" and options["state"] == "reference"
    before = LLMConfigManager._config_path().read_bytes()
    service.provider_status()
    assert before == LLMConfigManager._config_path().read_bytes()
    service.provider_effort_select(created["id"],MODEL,"max",options["revision"])
    runtime = configured_runtime_provider()
    assert runtime.context_window == 1000000
    assert runtime.request_preferences["reasoning_effort"] == "max"
    with pytest.raises(OperationError):
        service.provider_effort_select(created["id"],MODEL,"low",options["revision"])


@pytest.mark.parametrize(("model", "first_level"), [
    ("gpt-6.1-sol", "low"),
    ("gpt-6-sol", "none"),
    ("gpt-6-luna", "none"),
])
def test_saved_gpt_profiles_gain_official_effort_options_without_reconfiguration(model, first_level):
    from backend.core.loop.provider_preferences import apply_preferences

    profile = dict(model_id=model, protocol="openai_responses", context_window=512000, max_output_tokens=4096)
    provider = LLMConfigManager.add(LLMProvider(
        name="Official", base_url=OFFICIAL, api_key="synthetic", model_id=model,
        protocol="openai_responses", configuration={"models": [profile]},
    ))
    before = LLMConfigManager._config_path().read_bytes()
    service = ApplicationServices()
    with patch("backend.core.provider_metadata.catalogue") as network:
        options = service.provider_effort_options()
        service.provider_status()
        network.assert_not_called()
    optional_none = ["none"] if first_level == "none" else []
    assert options["levels"] == [*optional_none, "low", "medium", "high", "xhigh", "max"]
    assert options["default_effort"] == "medium" and options["state"] == "reference"
    assert options["source"] == f"https://developers.openai.com/api/docs/models/{model}"
    assert LLMConfigManager._config_path().read_bytes() == before
    if first_level != "none":
        with pytest.raises(OperationError, match="not declared"):
            service.provider_effort_select(provider.id, model, "none", options["revision"])
        assert LLMConfigManager._config_path().read_bytes() == before
    service.provider_effort_select(provider.id, model, "high", options["revision"])
    assert ApplicationServices().provider_effort_options()["effort"] == "high"
    runtime = configured_runtime_provider()
    assert apply_preferences({}, runtime.request_preferences, "openai_responses") == {"reasoning": {"effort": "high"}}
    proxy = resolve_profile(profile, "https://compat.example/v1")
    assert "deployment" not in proxy


def test_smaller_service_limit_and_user_override_survive_profile_save_and_reopen():
    service = ApplicationServices()
    connection=dict(base_url="https://smaller.example/v1",api_key="synthetic",protocol="openai_chat")
    listed={"models":[{"id":"small","name":"small","limits":{"context_window":32000},"source":"service"}]}
    with patch("backend.core.provider_drafts.discover",return_value=listed),patch("backend.core.provider_metadata.catalogue",return_value=({},"unavailable")):
        receipt=service.provider_models(connection,"small-list")
    models=[dict(model_id="small",protocol="openai_chat",context_window=512000,max_output_tokens=2048,
        context_source="default",model_receipt=receipt["receipt_id"]),
        dict(model_id="manual",protocol="openai_chat",context_window=16000,max_output_tokens=2048,context_source="user")]
    created=service.provider_save(name="Small",**connection,**{k:v for k,v in models[0].items() if k != "protocol"},models=models)["provider"]
    assert created["context_window"] == 32000
    options=service.provider_model_options()
    service.provider_model_select(created["id"],"manual",options["revision"])
    assert configured_runtime_provider().context_window == 16000
    assert service.provider_model_options()["models"][0]["budget"]["choices"] == []


@pytest.mark.parametrize("routed",[False,True])
def test_live_budget_save_preserves_inflight_request_and_updates_next_request_and_restart(routed):
    import io
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from backend.core.native_host import NativeHost
    from backend.core.loop.provider_protocol import ProviderEventType
    from backend.core.loop.provider_router import runtime_client

    entered, release = threading.Event(), threading.Event()
    bodies = []
    class Transport:
        def stream_sse(self, url, body, headers, **kwargs):
            bodies.append(copy.deepcopy(body))
            if len(bodies) == 1:
                entered.set()
                assert release.wait(5)
            yield "message", {"choices":[{"index":0,"delta":{"content":"ok"}}]}
            yield "message", {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
    service = ApplicationServices()
    created = service.provider_save(name="Live",base_url="https://live.example/v1",api_key="synthetic",model_id="one",
        context_window=256000,max_output_tokens=2048,request_preferences={"reasoning_effort":"low"})["provider"]
    transport = Transport()
    if routed:
        from backend.core.loop.provider_router import RoutedProvider
        client = RoutedProvider(LLMConfigManager.load(),client_factory=lambda configured:runtime_client(configured,transport=transport,live_preferences=True))
    else:
        client = runtime_client(LLMConfigManager.get_active(),transport=transport,live_preferences=True)
    host = NativeHost(stdin=io.StringIO(),stdout=io.StringIO())
    host._active_processes["live-request"] = {}
    class LiveDaemon:
        def stop(self):
            raise AssertionError("A budget-only save must not retire a daemon")
    host._daemons["project"] = LiveDaemon()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future=executor.submit(lambda:list(client.stream_events([{"role":"user","content":"hello"}])))
        assert entered.wait(3)
        updated=host._dispatch({"operation":"provider.save","arguments":dict(provider_id=created["id"],name="Live",base_url="https://live.example/v1",
            retain_api_key=True,model_id="one",context_window=512000,max_output_tokens=4096,
            request_preferences={"reasoning_effort":"high"},expected_revision=created["revision"])})
        assert not updated["runtime_reload"] and updated["effective_from"] == "next_model_request"
        assert not future.done() and "project" in host._daemons
        release.set()
        first=future.result(timeout=3)
        second=list(client.stream_events([{"role":"user","content":"hello"}]))
        list(runtime_client(LLMConfigManager.get_active(),transport=transport).stream_events([{"role":"user","content":"hello"}]))
        assert [body["reasoning_effort"] for body in bodies] == ["low","high","high"]
        assert [body["max_tokens"] for body in bodies] == [2048,4096,4096]
        observations=lambda events:[event.artifact["request_parameters"] for event in events
            if event.type == ProviderEventType.ROUTING_NOTICE and "request_parameters" in (event.artifact or {})]
        assert observations(first)[0]["context_window"] == 256000
        assert observations(second)[0]["context_window"] == 512000
        assert observations(second)[0]["effort"] == "high"
        before=LLMConfigManager._config_path().read_bytes()
        with pytest.raises(OperationError):
            host._dispatch({"operation":"provider.save","arguments":dict(provider_id=created["id"],name="Live",
                base_url="https://different.example/v1",api_key="synthetic",model_id="one",expected_revision=updated["provider"]["revision"])})
        assert before == LLMConfigManager._config_path().read_bytes()
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_sync_wire_retries_reuse_one_settings_snapshot():
    from backend.core.loop.llm import LLMProvider
    from backend.core.loop.provider_transport import ProviderHttpError
    state={"context_window":256000,"max_output_tokens":2048,"request_preferences":{"reasoning_effort":"low"}}
    calls=[]
    class Transport:
        def post_json(self,url,body,headers,**kwargs):
            calls.append(copy.deepcopy(body))
            if len(calls)==1:
                state.update(context_window=512000,max_output_tokens=4096,request_preferences={"reasoning_effort":"high"})
                raise ProviderHttpError(500,"synthetic server error",{})
            return {"choices":[{"message":{"content":"ok"}}]}
    client=LLMProvider("https://test.example","synthetic","model",transport=Transport(),request_configuration_source=lambda:copy.deepcopy(state))
    client.chat([{"role":"user","content":"hello"}],max_retries=1,base_delay=0)
    client.chat([{"role":"user","content":"hello"}],max_retries=0)
    assert [body["reasoning_effort"] for body in calls]==["low","low","high"]
    assert [body["max_tokens"] for body in calls]==[2048,2048,4096]


def test_saving_a_profile_preserves_unrecognized_fields_for_previous_version_roundtrips():
    service=ApplicationServices()
    connection=dict(name="Future",base_url="https://future.example/v1",api_key="synthetic",model_id="one",context_window=256000,max_output_tokens=2048)
    service.provider_save(**connection,models=[dict(model_id=model,protocol="openai_chat",context_window=256000,max_output_tokens=2048) for model in ["one","two"]])
    config=LLMConfigManager.load()
    for item in config["providers"][0]["configuration"]["models"]:
        item["future_extension"]={"preserve":item["model_id"]}
    LLMConfigManager.save(config)
    original=service.provider_status()["providers"][0]
    saved=service.provider_save(**connection,provider_id=original["id"],expected_revision=original["revision"],
        models=[dict(model_id=model,protocol="openai_chat",context_window=256000,max_output_tokens=2048) for model in ["one","two"]])["provider"]
    assert [p["future_extension"] for p in saved["configuration"]["models"]]==[{"preserve":"one"},{"preserve":"two"}]
