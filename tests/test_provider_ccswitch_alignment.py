import io
import json
from unittest.mock import patch
import pytest
from backend.core.provider_catalog import parse_models
from backend.core.provider_metadata import enrich, catalogue, CATALOG_URL
from backend.core.loop.provider_preferences import validate_preferences
from backend.core.loop.llm import LLMProvider
from backend.core.application.services import ApplicationServices, OperationError
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.provider_router import configured_runtime_provider


def reference():
    return {"vendor":{"api":"https://api.test/plan/v1","models":{"model":{"limit":{"context":64000,"output":8000},"reasoning":True,"modalities":{"input":["text","image"]},"reasoning_options":[{"type":"effort","values":["low","high"]}]}}}}


def test_reference_enriches_missing_fields_and_keeps_actual_service_limits():
    models=parse_models({"data":[{"id":"model","context_window":32000},{"id":"other"}]},"https://api.test/plan/v1/models")
    result=enrich(models,"https://api.test/plan/v1",reference())
    assert result[0]["limits"]=={"context_window":32000,"max_output_capacity":8000}
    assert result[0]["field_sources"]["context_window"]==models[0]["source"]
    assert result[0]["field_sources"]["max_output_capacity"]==CATALOG_URL
    assert result[0]["metadata"]["reasoning_efforts"]==["low","high"]
    assert result[1]["limits"]=={}
    assert "metadata" not in models[0]  # caller's service evidence is untouched
    assert enrich(models,"https://api.test/other-plan/v1",reference())==models


def test_catalogue_is_credential_free_cached_and_failure_does_not_break_model_discovery(monkeypatch):
    monkeypatch.setattr("backend.core.provider_metadata._cache",({},0))
    calls=[]
    def opening(request,**kwargs):
        calls.append(request)
        assert request.full_url==CATALOG_URL
        assert not request.has_header("Authorization")
        return io.BytesIO(json.dumps(reference()).encode())
    with patch("backend.core.provider_catalog.safe_opener",return_value=opening):
        assert catalogue()[1]=="fresh"
        assert catalogue()[1]=="cached"
    assert len(calls)==1
    monkeypatch.setattr("backend.core.provider_metadata._cache",({},0))
    with patch("backend.core.provider_catalog.safe_opener",side_effect=OSError("offline")):
        assert catalogue()==({},"unavailable")


def test_metadata_preserves_service_efforts_and_distinguishes_plan_endpoints():
    models=parse_models({"data":[{"id":"model","effort":{"supported_levels":["medium"]}}]},"https://api.test:443/plan/v1/models")
    data=reference()
    data["malformed"]={"api":"https://api.test:bad/plan/v1","models":{}}
    enriched=enrich(models,"https://api.test:443/plan/v1",data)
    assert enriched[0]["metadata"]["reasoning_efforts"]==["medium"]
    assert enriched[0]["limits"]["context_window"]==64000
    assert enrich(enriched,"https://api.test/plan/v1",data)==enriched
    assert enrich(models,"https://api.test:8443/plan/v1",data)==models


def test_catalogue_cancelled_during_lock_wait_does_not_start_network(monkeypatch):
    import threading
    event=threading.Event()
    class Lock:
        def acquire(self,**kwargs):
            event.set()
            return True
        def release(self):pass
    monkeypatch.setattr("backend.core.provider_metadata._lock",Lock())
    with patch("backend.core.provider_catalog.safe_opener") as opening:
        assert catalogue(event)==({},"cancelled")
    opening.assert_not_called()


@pytest.mark.parametrize("value,protocol,error",[
    ({"reasoning_effort":"high"},"unknown_protocol","PROTOCOL_UNSUPPORTED"),
    ({"reasoning_effort":"high","body":{"output_config":{"effort":"low"}}},"anthropic_messages","CONFLICT"),
    ({"reasoning_effort":"high","body":{"reasoning":{"effort":"low"}}},"openai_responses","CONFLICT"),
    ({"body":{"temperature":float("nan")}},"openai_chat","INVALID"),
    ({"body":{"seed":True}},"openai_chat","INVALID_SEED"),
])
def test_invalid_preferences_are_explicit_not_silently_dropped(value,protocol,error):
    with pytest.raises(ValueError,match=error):validate_preferences(value,protocol)


@pytest.mark.parametrize("body",[{"stream":False},{"model":"other"},{"tools":[]},{"max_tokens":999999},{"messages":[]},{"store":True}])
def test_request_overrides_cannot_replace_task_or_transport_authority(body):
    with pytest.raises(ValueError,match="PROTECTED"):
        validate_preferences({"body":body},"openai_chat")


@pytest.mark.parametrize("protocol",["openai_chat","openai_responses","anthropic_messages"])
def test_saved_parameters_reach_sync_and_streaming_wire_requests(protocol):
    calls=[]
    class Transport:
        def post_json(self,url,body,headers,**kwargs):
            calls.append(body)
            return {"choices":[{"message":{"content":"pong"}}],"output":[{"type":"message","content":[{"type":"output_text","text":"pong"}]}],"content":[{"type":"text","text":"pong"}]}
        def stream_sse(self,url,body,headers,**kwargs):
            calls.append(body)
            yield from ()
    preferences={"body":{"temperature":0.2,"top_p":0.8,"seed":23}}
    preferences["reasoning_effort"]="high"
    client=LLMProvider("https://api.test/v1","synthetic","model",protocol=protocol,transport=Transport(),request_preferences=preferences)
    client.chat([{"role":"user","content":"hello"}],max_tokens=128,max_retries=0)
    list(client.stream_events([{"role":"user","content":"hello"}],max_tokens=128))
    assert len(calls)==2
    for body in calls:
        assert body["temperature"]==0.2 and body["top_p"]==0.8 and body["seed"]==23
        if protocol=="openai_responses":assert body["reasoning"]["effort"]=="high"
        if protocol=="openai_chat":assert body["reasoning_effort"]=="high"
        if protocol=="anthropic_messages":assert body["output_config"]["effort"]=="high" and "reasoning_effort" not in body
        assert body.get("max_tokens",body.get("max_output_tokens"))==128
    assert calls[0]["stream"] is False and calls[1]["stream"] is True


def test_save_preferences_preserves_overrides_and_canonical_runtime_uses_them(monkeypatch):
    service=ApplicationServices()
    created=service.provider_save(name="Test",base_url="https://api.test/plan/v1",api_key="synthetic",model_id="model",context_window=16000,max_output_tokens=2048,
                                  request_preferences={"reasoning_effort":"high","body":{"temperature":0.2}})["provider"]
    configured=LLMConfigManager.get_active()
    assert configured.configuration["request_preferences"]["reasoning_effort"]=="high"
    client=configured_runtime_provider()
    assert client.request_preferences["body"]=={"temperature":0.2}
    # Legacy edit calls that omit the new fields retain their values.
    saved=service.provider_save(provider_id=created["id"],name="Edited",base_url=configured.base_url,retain_api_key=True,model_id="model",context_window=16000,max_output_tokens=2048)
    assert saved["provider"]["configuration"]["request_preferences"]==configured.configuration["request_preferences"]
    with pytest.raises(OperationError,match="protected"):
        service.provider_save(name="Bad",base_url="https://api.test",api_key="synthetic",model_id="m",request_preferences={"body":{"tools":[]}})


def test_reference_receipt_saves_reference_provenance_without_claiming_service_validation(monkeypatch):
    service=ApplicationServices()
    draft={"base_url":"https://api.test/plan/v1","api_key":"synthetic","model_id":"model","protocol":"openai_chat"}
    monkeypatch.setattr("backend.core.provider_metadata.catalogue",lambda *a,**k:(reference(),"cached"))
    with patch("backend.core.provider_drafts.discover",return_value={"models":parse_models({"data":[{"id":"model"}]},draft["base_url"]+"/models")}):
        listed=service.provider_models(draft,"catalogue")
    selected=listed["models"][0]
    assert selected["limits"]["context_window"]==64000
    saved=service.provider_save(name="Reference",**draft,model_receipt=listed["receipt_id"],context_window=64000,context_source="reference")
    assert saved["provider"]["configuration"]["sources"]["context_window"]=="reference"
    assert saved["provider"]["capabilities"]=={}
