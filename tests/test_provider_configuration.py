import io
import json
import threading
import urllib.error
from unittest.mock import patch

import pytest

from backend.core.application.services import ApplicationServices, OperationError
from backend.core.llm_config import LLMConfigManager, LLMProvider
from backend.core.provider_catalog import discover, models_urls, parse_models
from backend.core.provider_drafts import ProviderDrafts
from backend.core.loop.provider_probe import ProviderProbe
from backend.core.loop.provider_transport import ProviderTransportCancelled
from backend.core.loop.provider_transport import ProviderHttpError


@pytest.fixture(autouse=True)
def no_reference_catalogue_network(monkeypatch):
    monkeypatch.setattr("backend.core.provider_metadata.catalogue", lambda *a, **k: ({}, "unavailable"))


def connection(**extra):
    return dict(base_url="https://example.test/prefix/v1",api_key="synthetic-private-key",model_id="model",protocol="openai_chat",kind="remote",auth_mode="api_key",**extra)


def test_models_preserve_proxy_prefix_and_never_guess_limits():
    assert models_urls("https://api.openai.com/v1", "openai_responses") == ["https://api.openai.com/v1/models"]
    assert models_urls("https://api.test/prefix/v1/chat/completions", "openai_chat") == ["https://api.test/prefix/v1/models"]
    assert models_urls("https://api.test/prefix", "anthropic_messages") == ["https://api.test/prefix/v1/models","https://api.test/prefix/models"]
    result=parse_models({"data":[{"id":"gpt-any","context_length":32000,"top_provider":{"max_completion_tokens":7000}},{"id":"unknown"},None,{"id":"unknown"}]},"https://api.test/v1/models")
    assert result[0]["limits"] == {"context_window":32000,"max_output_capacity":7000}
    assert result[1]["limits"] == {}
    assert result[0]["state"] == "declared"


def test_discovery_uses_protocol_auth_and_only_404_fallback():
    calls=[]
    def opener(request,**kwargs):
        calls.append((request.full_url,request.headers))
        if len(calls)==1:
            raise urllib.error.HTTPError(request.full_url,404,"missing",{},io.BytesIO(b"private"))
        return io.BytesIO(json.dumps({"models":[{"slug":"m"}]}).encode())
    result=discover(base_url="https://api.test/prefix",api_key="secret",protocol="anthropic_messages",open_url=opener)
    assert result["models"][0]["id"] == "m"
    assert calls[0][1]["X-api-key"] == "secret"
    assert "Authorization" not in calls[0][1]
    calls.clear()
    def unauthorized(request,**kwargs):
        calls.append(True)
        raise urllib.error.HTTPError(request.full_url,401,"bad",{},io.BytesIO(b"secret"))
    with pytest.raises(ValueError,match="MODEL_LIST_HTTP_401") as caught:
        discover(base_url="https://api.test",api_key="secret",open_url=unauthorized)
    assert len(calls)==1 and "secret" not in str(caught.value)


def test_discovery_cancelled_and_oversized():
    event=threading.Event();event.set()
    with pytest.raises(ProviderTransportCancelled):
        discover(base_url="https://api.test",cancel_event=event)
    with pytest.raises(ValueError,match="TOO_LARGE"):
        discover(base_url="https://api.test",open_url=lambda *a,**k:io.BytesIO(b"x"*(4*1024*1024+1)))


def test_config_preserves_unknown_fields_and_rejects_stale_save():
    service=ApplicationServices()
    first=service.provider_save(name="Test",**connection())["provider"]
    assert first["revision"] == service.provider_status()["providers"][0]["revision"]
    raw=LLMConfigManager.load();raw["future_setting"]={"keep":True};raw["providers"][0]["future_field"]={"keep":True};LLMConfigManager.save(raw)
    current=service.provider_status()["providers"][0]
    # Unrelated route changes do not rotate credentials or invalidate the form.
    service.provider_failover_configure(True,[])
    assert service.provider_status()["providers"][0]["revision"] == current["revision"]
    service.provider_save(provider_id=first["id"],name="Edited",expected_revision=current["revision"],**connection())
    saved=LLMConfigManager.load()
    assert saved["future_setting"] == {"keep":True} and saved["providers"][0]["future_field"] == {"keep":True}
    with pytest.raises(OperationError,match="changed"):
        service.provider_save(provider_id=first["id"],name="Stale",expected_revision=current["revision"],**connection())
    assert LLMConfigManager.get_active().name == "Edited"
    assert "synthetic-private-key" not in json.dumps(service.provider_status())


def test_key_change_invalidates_evidence_and_endpoint_change_cannot_borrow_key():
    service=ApplicationServices()
    created=service.provider_save(name="Test",**connection())["provider"]
    item=LLMConfigManager.get_active();item.capabilities={"tools":True};item.configuration={"verification":{"checks":{"tools":"verified"}}};LLMConfigManager.update(item)
    changed={**connection(),"api_key":"replacement"}
    service.provider_save(provider_id=created["id"],name="Test",**changed)
    assert not LLMConfigManager.get_active().capabilities
    assert "verification" not in LLMConfigManager.get_active().configuration
    with pytest.raises(OperationError):
        service.provider_models({"provider_id":created["id"],"retain_api_key":True,"base_url":"https://other.test"},"request")


def test_probe_receipt_is_identity_bound_and_preview_never_writes():
    service=ApplicationServices();draft=connection()
    with patch("backend.core.provider_drafts.discover",return_value={"models":[{"id":"model","limits":{"context_window":32000,"max_output_capacity":2048}}]}):
        models=service.provider_models(draft,"one")
    assert LLMConfigManager.load()["providers"] == []
    with pytest.raises(OperationError):
        service.provider_save(name="Test",**draft,model_receipt=models["receipt_id"],context_window=64000,context_source="discovered")
    saved=service.provider_save(name="Test",**draft,model_receipt=models["receipt_id"],context_window=32000,max_output_tokens=1024,context_source="discovered")
    assert saved["provider"]["configuration"]["deployment"]["limits"]["max_output_capacity"]==2048
    with pytest.raises(OperationError):
        service.provider_save(name="Other",**{**draft,"api_key":"another"},model_receipt=models["receipt_id"])


def test_cancel_before_start_has_no_remote_effect():
    registry=ProviderDrafts();registry.cancel("cancelled")
    with patch("backend.core.provider_drafts.discover") as remote, pytest.raises(ValueError):
        registry.run("models",connection(),"cancelled",15)
    remote.assert_not_called()


def test_repeated_new_save_after_unknown_ack_cannot_duplicate_or_overwrite():
    service=ApplicationServices()
    a=service.provider_save(name="New",creation_id="a"*24,**connection())
    b=service.provider_save(name="New",creation_id="a"*24,**connection())
    assert a["provider"]["id"]==b["provider"]["id"]
    assert len(service.provider_status()["providers"])==1
    with pytest.raises(OperationError):
        service.provider_save(name="Changed",creation_id="a"*24,**connection())
    assert LLMConfigManager.get_active().name=="New"


class RoundtripTransport:
    def __init__(self,fail_tool=False):self.requests=[];self.fail_tool=fail_tool
    def post_json(self,url,body,headers,**kwargs):
        self.requests.append(body)
        tool=next((m for m in body["messages"] if m["role"]=="tool"),None)
        text=tool["content"] if tool else "pong"
        return {"choices":[{"message":{"content":text}}]}
    def stream_sse(self,url,body,headers,**kwargs):
        self.requests.append(body)
        if self.fail_tool:raise RuntimeError("temporary failure")
        yield "message",{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","type":"function","function":{"name":"gitgo_probe_echo","arguments":'{"value":"probe"}'}}]},"finish_reason":None}]}
        yield "message",{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}


def test_tools_require_actual_result_continuation_and_transient_failure_is_unknown():
    transport=RoundtripTransport()
    result=ProviderProbe(transport).probe(**connection(),timeout=5)
    assert result.checks["tools"]=="verified"
    assert len(transport.requests)==3
    assert transport.requests[-1]["messages"][-1]["content"].startswith("echo-")
    unavailable=ProviderProbe(RoundtripTransport(True)).probe(**connection(),timeout=5)
    assert unavailable.checks["tools"]=="unknown" and not unavailable.capabilities.tools
    assert unavailable.checks["model_response"]=="verified"


@pytest.mark.parametrize("error,expected",[
    ({"message":"Unsupported model model"},"PROVIDER_MODEL_UNAVAILABLE"),
    ({"code":"model_not_found","message":"private diagnostics"},"PROVIDER_MODEL_UNAVAILABLE"),
    ({"message":"Invalid max_tokens"},"PROVIDER_PROBE_HTTP_400"),
])
def test_model_rejection_is_explicit_and_never_inferred_from_http_status(error,expected):
    class Rejected:
        def post_json(self,*args,**kwargs):
            raise ProviderHttpError(400,json.dumps({"error":error}),{})
    with pytest.raises(ValueError,match=expected) as caught:
        ProviderProbe(Rejected()).probe(**connection(),timeout=2)
    assert "private" not in str(caught.value)
