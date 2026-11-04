import copy
import io
from unittest.mock import patch
import pytest
from backend.core.application.services import ApplicationServices,OperationError
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.provider_router import configured_runtime_provider
from backend.core.native_host import NativeHost


def configured(service,protocol="openai_chat",connection=None):
    connection=connection or dict(base_url="https://api.test/v1",api_key="synthetic",protocol=protocol,kind="remote",auth_mode="api_key")
    listing={"models":[{"id":"one","name":"one","limits":{},"source":"https://api.test/v1/models","metadata":{"reasoning_efforts":["low","medium","high"],"state":"declared","source":"https://api.test/v1/models"}}]}
    with patch("backend.core.provider_drafts.discover",return_value=listing),patch("backend.core.provider_metadata.catalogue",return_value=({},"unavailable")):
        receipt=service.provider_models(connection,"effort-list")
    items=[dict(model_id=model,protocol=protocol,context_window=32000,max_output_tokens=2048,request_preferences={},context_source="user",model_receipt=receipt["receipt_id"] if model=="one" else "") for model in ("one","two")]
    return service.provider_save(name="API",**{**connection,**items[0]},models=items)["provider"]


@pytest.mark.parametrize("protocol",["openai_chat","openai_responses","anthropic_messages"])
def test_effort_updates_current_profile_and_runtime_projection_together(protocol):
    service=ApplicationServices();created=configured(service,protocol)
    options=service.provider_effort_options()
    assert options["levels"]==["low","medium","high"] and options["effort"]==""
    before=copy.deepcopy(service.provider_model_options()["models"][1])
    service.provider_effort_select(created["id"],"one","high",options["revision"])
    saved=LLMConfigManager.get_active()
    assert saved.configuration["request_preferences"]=={"reasoning_effort":"high"}
    assert saved.configuration["models"][0]["request_preferences"]=={"reasoning_effort":"high"}
    assert saved.configuration["models"][1]==before
    assert configured_runtime_provider().request_preferences=={"reasoning_effort":"high"}
    reopened=ApplicationServices().provider_effort_options()
    assert reopened["effort"]=="high"
    with pytest.raises(OperationError):service.provider_effort_select(created["id"],"one","low",options["revision"])
    service.provider_effort_select(created["id"],"one","",reopened["revision"])
    assert LLMConfigManager.get_active().configuration["request_preferences"]=={}


def test_effort_rejects_foreign_model_unknown_level_missing_revision_without_a_write():
    service=ApplicationServices();created=configured(service);options=service.provider_effort_options()
    before=LLMConfigManager._config_path().read_bytes()
    for model,level,revision in [("two","low",options["revision"]),("one","max",options["revision"]),("one","low","")]:
        with pytest.raises(OperationError):service.provider_effort_select(created["id"],model,level,revision)
        assert LLMConfigManager._config_path().read_bytes()==before
    service.provider_model_select(created["id"],"two",options["revision"])
    assert service.provider_effort_options()["levels"]==[]
    assert service.provider_effort_options()["state"]=="unknown"


def test_native_effort_switch_commits_while_busy_and_preserves_daemons_on_success_or_failure():
    service=ApplicationServices();created=configured(service);options=service.provider_effort_options()
    host=NativeHost(stdin=io.StringIO(),stdout=io.StringIO())
    args=dict(provider_id=created["id"],model_id="one",effort="low",expected_revision=options["revision"])
    host._active_processes["busy"]={}
    result=host._dispatch({"operation":"provider.effort.select","arguments":args})
    assert result["effective_from"]=="next_model_request" and not result["runtime_reload"]
    assert service.provider_effort_options()["effort"]=="low"
    host._active_processes.clear()
    stopped=[]
    class Idle:
        def stop(self):stopped.append(True)
    host._daemons["project"]=Idle()
    args["expected_revision"]=service.provider_effort_options()["revision"]
    with pytest.raises(OperationError):host._dispatch({"operation":"provider.effort.select","arguments":{**args,"effort":"max"}})
    assert "project" in host._daemons and not stopped
    host._dispatch({"operation":"provider.effort.select","arguments":args})
    assert stopped==[] and "project" in host._daemons

def test_selecting_the_current_api_is_a_read_only_noop_even_while_busy():
    service=ApplicationServices();created=configured(service)
    before=LLMConfigManager._config_path().read_bytes()
    host=NativeHost(stdin=io.StringIO(),stdout=io.StringIO())
    host._active_processes["busy"]={}
    class LiveDaemon:
        def stop(self):raise AssertionError("Current API selection must not retire the daemon")
    daemon=LiveDaemon();host._daemons["project"]=daemon
    host._sessions["project"]="existing-session"
    result=host._provider_switch(created["id"])
    assert result["active_provider"]==created["id"] and not result["runtime_reload"]
    assert result["retired_projects"]==[] and host._daemons["project"] is daemon
    assert host._sessions["project"]=="existing-session"
    assert LLMConfigManager._config_path().read_bytes()==before


def test_effort_uses_only_a_declared_default_and_explicit_preferences_take_precedence():
    from backend.core.provider_effort import effort_options
    from backend.core.provider_catalog import parse_models
    models=parse_models({"data":[{"id":"one","effort":{"supported_levels":["low","high"],"default":"high"}}]},"synthetic")
    profile={"protocol":"openai_chat","deployment":models[0],"request_preferences":{}}
    assert effort_options(profile)["effective_effort"]=="high"
    profile["request_preferences"]={"reasoning_effort":"low"}
    assert effort_options(profile)["effective_effort"]=="low"
    profile["request_preferences"]={};profile["deployment"]["metadata"].pop("reasoning_effort_default")
    assert effort_options(profile)["effective_effort"]==""


@pytest.mark.parametrize("routed",[False,True])
def test_live_effort_switch_keeps_inflight_http_body_and_updates_next_request_and_restart(routed,monkeypatch):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from backend.core.loop.provider_protocol import ProviderEventType
    from backend.core.daemon.policy_helpers import _runtime_llm_from_config,_resolve_llm_config
    for name in ("GITGO_LLM_BASE_URL","GITGO_LLM_API_KEY","GITGO_LLM_MODEL"):
        monkeypatch.delenv(name,raising=False)
    def daemon_client():
        return _runtime_llm_from_config(_resolve_llm_config("synthetic-workspace"))
    entered=threading.Event();release=threading.Event();cancel=threading.Event();calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers["Content-Length"])));calls.append(body)
            if len(calls)==1:entered.set();release.wait(5)
            self.send_response(200);self.send_header("Content-Type","text/event-stream");self.end_headers()
            for payload in [{"choices":[{"index":0,"delta":{"content":"ok"}}]}, {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}]:
                self.wfile.write(("data: "+json.dumps(payload)+"\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n");self.wfile.flush()
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    executor=ThreadPoolExecutor(max_workers=1)
    try:
        service=ApplicationServices();created=configured(service,connection=dict(base_url=f"http://127.0.0.1:{server.server_port}/v1",api_key="",protocol="openai_chat",kind="local",auth_mode="none"))
        options=service.provider_effort_options();service.provider_effort_select(created["id"],"one","low",options["revision"])
        if routed:LLMConfigManager.configure_failover(True,[],provider_id=created["id"])
        client=daemon_client();messages=[{"role":"user","content":"synthetic request"}]
        future=executor.submit(lambda:list(client.stream_events(messages,cancel_event=cancel)))
        assert entered.wait(3) and calls[0]["reasoning_effort"]=="low"
        host=NativeHost(stdin=io.StringIO(),stdout=io.StringIO());host._active_processes["synthetic-busy"]={}
        class LiveDaemon:
            def stop(self):raise AssertionError("Effort switching must not retire a live daemon")
        daemon=LiveDaemon();host._daemons["synthetic-project"]=daemon
        options=service.provider_effort_options()
        result=host._dispatch({"operation":"provider.effort.select","arguments":dict(provider_id=created["id"],model_id="one",effort="high",expected_revision=options["revision"])})
        assert result["effective_from"]=="next_model_request" and not cancel.is_set() and not future.done()
        assert host._daemons["synthetic-project"] is daemon
        release.set();events=future.result(timeout=3)
        assert any(e.type==ProviderEventType.RESPONSE_COMPLETED for e in events)
        list(client.stream_events(messages,cancel_event=cancel))
        list(daemon_client().stream_events(messages,cancel_event=cancel))
        assert [body["reasoning_effort"] for body in calls]==["low","high","high"]
        assert ApplicationServices().provider_effort_options()["effort"]=="high"
    finally:
        release.set();executor.shutdown(wait=True);server.shutdown();server.server_close();thread.join(timeout=2)
