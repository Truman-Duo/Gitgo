import json
from unittest.mock import patch
import pytest
from backend.core.application.services import ApplicationServices,OperationError
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.provider_router import configured_runtime_provider
from backend.core.provider_profiles import profiles

def model(name,context=32000,output=2048,effort="low"):
    return dict(model_id=name,protocol="openai_chat",context_window=context,max_output_tokens=output,request_preferences={"reasoning_effort":effort},context_source="user",output_source="user")

def create(service,name="API",items=None):
    items=items or [model("a"),model("b",64000,4096,"high")]
    return service.provider_save(name=name,base_url="https://api.test/v1",api_key="synthetic-secret",**items[0],models=items)["provider"]

def test_one_api_models_select_projects_parameters_into_existing_runtime_and_preserves_key():
    service=ApplicationServices();created=create(service)
    other=create(service,"Other API",[model("other-only")])
    options=service.provider_model_options()
    assert options["provider_id"]==created["id"]
    assert [p["model_id"] for p in options["models"]]==["a","b"]
    result=service.provider_model_select(created["id"],"b",options["revision"])
    assert result["model_id"]=="b"
    active=LLMConfigManager.get_active()
    assert (active.id,active.model_id,active.context_window,active.max_output_tokens)==(created["id"],"b",64000,4096)
    assert active.api_key=="synthetic-secret"
    runtime=configured_runtime_provider()
    assert runtime._model=="b" and runtime.request_preferences=={"reasoning_effort":"high"}
    service.provider_switch(other["id"])
    assert [p["model_id"] for p in service.provider_model_options()["models"]]==["other-only"]
    assert "synthetic-secret" not in json.dumps(service.provider_model_options())

def test_model_selection_rejects_foreign_models_stale_revision_and_changed_api():
    service=ApplicationServices();created=create(service);other=create(service,"Other",[model("foreign")])
    options=service.provider_model_options()
    with pytest.raises(OperationError,match="not configured"):
        service.provider_model_select(created["id"],"foreign",options["revision"])
    service.provider_model_select(created["id"],"b",options["revision"])
    with pytest.raises(OperationError,match="configuration changed"):
        service.provider_model_select(created["id"],"a",options["revision"])
    service.provider_switch(other["id"])
    with pytest.raises(OperationError,match="current API changed"):
        service.provider_model_select(created["id"],"a",options["revision"])

@pytest.mark.parametrize("items",[[],[model("a"),model("a")],[model("a"),{**model("b"),"context_window":100}],[model("a"),{**model("b"),"api_key":"not-allowed"}]])
def test_invalid_multi_model_save_is_atomic(items):
    service=ApplicationServices();created=create(service)
    before=LLMConfigManager._config_path().read_bytes()
    with pytest.raises(OperationError):
        service.provider_save(provider_id=created["id"],name="changed",base_url="https://api.test/v1",retain_api_key=True,**model("a"),models=items)
    assert LLMConfigManager._config_path().read_bytes()==before

def test_legacy_configuration_loads_as_one_model_without_rewriting_or_losing_extensions():
    service=ApplicationServices()
    original=service.provider_save(name="Legacy",base_url="https://api.test/v1",api_key="synthetic",model_id="legacy")["provider"]
    config=LLMConfigManager.load();item=config["providers"][0]
    item["configuration"].pop("models");item["configuration"]["future_extension"]={"x":1};LLMConfigManager.save(config)
    before=LLMConfigManager._config_path().read_bytes()
    options=service.provider_model_options()
    assert [p["model_id"] for p in options["models"]]==["legacy"]
    assert LLMConfigManager._config_path().read_bytes()==before
    service.provider_model_select(original["id"],"legacy",options["revision"])
    assert LLMConfigManager.get_active().configuration["future_extension"]=={"x":1}

def test_auto_protocol_receipts_compile_each_model_before_the_single_save():
    from types import SimpleNamespace
    service=ApplicationServices()
    connection=dict(base_url="https://api.test/v1",api_key="synthetic",protocol="auto",kind="remote",auth_mode="api_key")
    items=[]
    result={"protocol":"openai_responses","capabilities":{"protocol":"openai_responses","probed_at":"synthetic-probe"},"checks":{"tools":"verified"},"observations":["synthetic"]}
    with patch("backend.core.provider_drafts.ProviderProbe.probe",return_value=SimpleNamespace(to_dict=lambda:result)):
        for name in ("a","b"):
            profile={**model(name),"protocol":"auto"}
            probe=service.provider_probe({**connection,"model_id":name,"request_preferences":profile["request_preferences"]},"probe-"+name)
            items.append({**profile,"probe_receipt":probe["receipt_id"]})
    saved=service.provider_save(name="Auto models",**{**connection,**items[0]},models=items)["provider"]
    assert saved["protocol"]=="openai_responses"
    assert all(p["protocol"]=="openai_responses" for p in saved["configuration"]["models"])

def test_entering_new_connection_reads_public_models_without_a_key_then_checks_account_list():
    service=ApplicationServices()
    draft=dict(base_url="https://api.test/plan/v1",api_key="",protocol="openai_chat",kind="remote",auth_mode="api_key")
    catalogue={"vendor":{"api":draft["base_url"],"models":{"public-model":{"name":"Public model","limit":{"context":32000,"output":8192}}}}}
    with patch("backend.core.provider_metadata.catalogue",return_value=(catalogue,"cached")),patch("backend.core.provider_drafts.discover") as discovery:
        listed=service.provider_models(draft,"entry-public")
    discovery.assert_not_called()
    assert listed["reference_only"] and listed["models"][0]["state"]=="reference"
    saved=service.provider_save(name="Public candidates",**{**draft,"api_key":"synthetic"},model_id="public-model",context_window=32000,max_output_tokens=2048,context_source="reference",model_receipt=listed["receipt_id"])["provider"]
    assert saved["capabilities"]=={} and saved["configuration"]["sources"]["context_window"]=="reference"
    with patch("backend.core.provider_metadata.catalogue",return_value=(catalogue,"cached")),patch("backend.core.provider_drafts.discover",return_value={"models":[{"id":"account-only","name":"Account only","limits":{},"source":"https://api.test/plan/v1/models"}]}):
        actual=service.provider_models({**draft,"api_key":"synthetic"},"account-list")
    assert not actual.get("reference_only")
    assert [p["id"] for p in actual["models"]]==["account-only"]

def test_new_keyless_authenticated_draft_does_not_probe_account_endpoint_when_reference_is_unavailable():
    service=ApplicationServices()
    draft=dict(base_url="https://api.openai.com/v1",api_key="",protocol="openai_responses",kind="remote",auth_mode="api_key")
    with patch("backend.core.provider_metadata.catalogue",return_value=({},"unavailable")),patch("backend.core.provider_drafts.discover",side_effect=ValueError("MODEL_LIST_HTTP_401")) as discovery:
        listed=service.provider_models(draft,"empty-key-no-auth-request")
    discovery.assert_not_called()
    assert listed["models"]==[] and listed["credentials_required"] and listed["manual_allowed"]
    assert LLMConfigManager.load()["providers"]==[]

def test_entry_reference_and_authenticated_model_discovery_use_real_http_without_false_form_writes():
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            requests.append(self.path)
            authorized=self.headers.get("Authorization")=="Bearer synthetic-valid"
            self.send_response(200 if authorized else 401)
            self.send_header("Content-Type","application/json");self.end_headers()
            self.wfile.write(json.dumps({"data":[{"id":"synthetic-gpt"}]} if authorized else {"error":"synthetic-private-response-body"}).encode())
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    service=ApplicationServices()
    draft=dict(base_url=f"http://127.0.0.1:{server.server_port}/v1",api_key="",protocol="openai_responses",kind="local",auth_mode="api_key")
    try:
        with patch("backend.core.provider_metadata.catalogue",return_value=({},"unavailable")):
            pending=service.provider_models(draft,"http-no-key")
            assert pending["credentials_required"] and requests==[]
            with pytest.raises(OperationError) as caught:
                service.provider_models({**draft,"api_key":"synthetic-invalid"},"http-401")
            assert caught.value.code=="MODEL_LIST_HTTP_401"
            assert "synthetic-private-response-body" not in str(caught.value)
            actual=service.provider_models({**draft,"api_key":"synthetic-valid"},"http-valid")
        assert requests==["/v1/models","/v1/models"]
        assert actual["models"][0]["id"]=="synthetic-gpt" and not actual.get("reference_only")
        assert LLMConfigManager.load()["providers"]==[]
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)

def test_large_model_catalogue_is_saved_without_the_old_256_model_limit():
    service=ApplicationServices()
    items=[model("model-"+str(i)) for i in range(300)]
    saved=create(service,items=items)
    assert len(saved["configuration"]["models"])==300
    assert len(service.provider_model_options()["models"])==300

def test_three_models_survive_save_reopen_and_independent_runtime_selection():
    service=ApplicationServices()
    entries=[model("v3",16000,1024),model("v4flash",64000,2048),model("v4pro",128000,4096,"high")]
    saved=create(service,items=entries)
    reopened=ApplicationServices()
    assert [p["model_id"] for p in reopened.provider_model_options()["models"]]==["v3","v4flash","v4pro"]
    for entry in entries:
        current=reopened.provider_model_options()
        reopened.provider_model_select(saved["id"],entry["model_id"],current["revision"])
        runtime=LLMConfigManager.get_active()
        assert (runtime.model_id,runtime.context_window,runtime.max_output_tokens)==(entry["model_id"],entry["context_window"],entry["max_output_tokens"])

def test_light_connection_check_is_one_request_and_not_a_full_capability_receipt():
    from backend.core.loop.provider_probe import ProviderProbe
    from backend.core.loop.provider_protocol import ProviderCapabilities
    class Transport:
        calls=0
        def post_json(self,url,body,headers,**kwargs):
            self.calls+=1
            assert body["model"]=="v3" and not body.get("tools")
            return {"choices":[{"message":{"content":"pong"}}]}
        def stream_sse(self,*a,**k):raise AssertionError("no paid stream/tool roundtrip during a connection check")
    transport=Transport();probe=ProviderProbe(transport)
    result=probe.probe(base_url="https://api.test/v1",api_key="synthetic",model_id="v3",protocol="openai_chat",connection_only=True).to_dict()
    assert transport.calls==1 and result["checks"]["model_response"]=="verified"
    assert result["checks"]["tools"]=="unknown" and result["checks"]["streaming"]=="unknown"
    service=ApplicationServices()
    from types import SimpleNamespace
    with patch("backend.core.provider_drafts.ProviderProbe.probe",return_value=SimpleNamespace(to_dict=lambda:result)):
        receipt=service.provider_connection(dict(base_url="https://api.test/v1",api_key="synthetic",model_id="v3",protocol="openai_chat"),"light-check")
    with pytest.raises(OperationError,match="expired"):
        service.provider_save(name="Not a capability probe",base_url="https://api.test/v1",api_key="synthetic",model_id="v3",probe_receipt=receipt["receipt_id"])

def test_native_model_switch_uses_existing_busy_boundary_and_retires_idle_daemon():
    from backend.core.native_host import NativeHost
    service=ApplicationServices();created=create(service)
    host=NativeHost();options=service.provider_model_options()
    host._active_processes["synthetic"]={}
    with pytest.raises(OperationError,match="safe boundary"):
        host._provider_change("provider.model.select",dict(provider_id=created["id"],model_id="b",expected_revision=options["revision"]))
    assert LLMConfigManager.get_active().model_id=="a"
    host._active_processes.clear()
    class IdleDaemon:
        stopped=False
        def stop(self):self.stopped=True
    daemon=IdleDaemon();host._daemons["synthetic-project"]=daemon
    with patch.object(host,"_emit"),patch.object(host.services,"provider_model_select",wraps=host.services.provider_model_select):
        result=host._provider_change("provider.model.select",dict(provider_id=created["id"],model_id="b",expected_revision=options["revision"]))
    assert result["runtime_reload"] and result["model_id"]=="b"
    assert daemon.stopped and not host._daemons
    host.close()
