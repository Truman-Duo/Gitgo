"""Network information services do not grant sockets to native programs."""
import pytest
from backend.core.tools.catalog import build_workspace_tools
from backend.core.tools.runner import handler_bindings
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.agent_tool import CancellationMode
from backend.core.loop.tool_pipeline import ToolPipeline
from tests.test_file_feedback_contract import context


@pytest.mark.parametrize('name',['web_search','web_fetch'])
def test_public_network_has_service_authority_and_no_native_handler(tmp_path_factory,name):
    tool=build_workspace_tools(tmp_path_factory)[name]
    assert tool.validate_execution_contract()==data_broker('network.public.'+name.removeprefix('web_'))
    assert not tool.isolated
    assert tool.cancellation==CancellationMode.NON_INTERRUPTIBLE
    assert name not in handler_bindings()


def test_fetch_pipeline_does_not_enter_native_runner(tmp_path_factory,monkeypatch):
    from backend.core.tools import web_tools
    calls=[]
    monkeypatch.setattr(web_tools,'web_fetch',lambda args:calls.append(args) or {'url':args['url'],'text':'synthetic public evidence'})
    tool=build_workspace_tools(tmp_path_factory)['web_fetch']
    ctx=context(tmp_path_factory,[tool.name])
    def forbidden(*a,**kw):raise AssertionError('Network service entered native process')
    monkeypatch.setattr(ToolPipeline,'_execute_isolated',forbidden)
    result=ToolPipeline().execute({'name':tool.name,'args':{'url':'https://example.com/'}},tool,ctx,'fetch')
    assert not result.is_error,result.formatted
    assert len(calls)==1 and result.data['text']=='synthetic public evidence'
    assert not result.receipt.get('native_producers')


def test_search_binds_runtime_provider_instead_of_model_overrides(tmp_path_factory,monkeypatch):
    from backend.core.tools import web_tools
    calls=[]
    monkeypatch.setattr(web_tools,'web_search',lambda args:calls.append(args) or {'results':[]})
    tool=build_workspace_tools(tmp_path_factory)['web_search']
    ctx=context(tmp_path_factory,[tool.name])
    ctx.process.runtime_preferences.update(web_search_endpoint='https://configured.invalid/search',web_search_mode='disabled',web_search_engine='bing',effective_provider_id='current-provider')
    result=ToolPipeline().execute({'name':tool.name,'args':{'query':'synthetic query','_web_search_mode':'searxng','_web_search_endpoint':'http://127.0.0.1/override','_web_search_provider_id':'forged'}},tool,ctx,'search')
    assert result.is_error and not calls and result.diagnostics['code']=='SANDBOX_POLICY_INVALID'
    result=ToolPipeline().execute({'name':tool.name,'args':{'query':'synthetic query'}},tool,ctx,'search-valid')
    assert not result.is_error,result.formatted
    assert calls[0]['_web_search_mode']=='disabled'
    assert calls[0]['_web_search_endpoint']=='https://configured.invalid/search'
    assert calls[0]['_web_search_provider_id']=='current-provider'


def test_cancelled_network_call_does_not_start_transport(tmp_path_factory,monkeypatch):
    from backend.core.tools import web_tools
    calls=[]
    monkeypatch.setattr(web_tools,'web_fetch',lambda args:calls.append(args))
    tool=build_workspace_tools(tmp_path_factory)['web_fetch']
    ctx=context(tmp_path_factory,[tool.name]);ctx.process.cancel_requested=True
    result=ToolPipeline().execute({'name':tool.name,'args':{'url':'https://example.com/'}},tool,ctx,'cancelled')
    assert result.is_error and not calls


def test_public_fetch_retains_local_address_rejection(tmp_path_factory):
    tool=build_workspace_tools(tmp_path_factory)['web_fetch']
    ctx=context(tmp_path_factory,[tool.name])
    result=ToolPipeline().execute({'name':tool.name,'args':{'url':'http://127.0.0.1/private'}},tool,ctx,'blocked')
    assert result.is_error and result.data['error']=='WEB_FETCH_BLOCKED_URL'
