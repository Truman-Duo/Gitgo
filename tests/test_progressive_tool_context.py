"""Selected tool information is delivered before effects, not all at start."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.core.loop.agent_tool import AgentTool, ToolEffect, ApprovalMode
from backend.core.loop.execution_contract import data_broker
from backend.core.loop.models import AgentProcess, RingLevel
from backend.core.loop.session import AgentSession
from backend.core.loop.tools import ToolRegistry
from backend.core.loop.provider_route_runtime import ProviderRouteRuntime
from backend.core.loop.provider_protocol import deterministic_hash
from backend.core.loop.provider_tool_transport import reference_catalog
from backend.core.loop.provider_context import ContextSections, decode_records
from backend.core.loop.tool_context import contract_overview, required_tool_context, provide_tool_context, tool_definition, visible_tool_contracts
from backend.core.loop.tool_proposal_runtime import ToolProposalRuntime
from tests.test_mechanism_interactions import runtime


def fixture_process(workspace):
    process=AgentProcess("progressive-context","worker",RingLevel.RING_3,
        session=AgentSession(),active_task_id="task",worktree_path=str(workspace))
    process.tool_registry=ToolRegistry(["selected","unrelated","small"])
    def big(name):
        return AgentTool(name,"Purpose "+name+"\n"+("EXACT DETAILS "+name+" ")*800,
            {"type":"object","properties":{"path":{"type":"string"},"optional":{"type":"object",
                "properties":{"depth":{"type":"integer"}}}},"required":["path"]},
            lambda args: {"value":args},execution_contract=data_broker("test.context"),
            effect=ToolEffect.READ,approval=ApprovalMode.DENY)
    tools={name:big(name) for name in ("selected","unrelated")}
    tools["small"]=AgentTool("small","Read a value",{"type":"object","properties":{}},
        lambda args:{},execution_contract=data_broker("test.context"))
    route=ProviderRouteRuntime(process,process.session,None,"route",None,lambda:None)
    route.prepare_envelope([tool.to_openai_function() for tool in tools.values()],
        {"tool_behaviors":{name:{"effect":tool.effect.value,"cancellation":tool.cancellation.value}
                          for name,tool in tools.items()}})
    wire=process.session.to_provider_messages(dynamic_envelope=route.envelope,provider_route="route")
    return process,tools,route,wire


def test_initial_request_has_basics_then_selected_exact_details_without_execution(tmp_path_factory):
    process,tools,route,wire=fixture_process(tmp_path_factory)
    original=deepcopy(wire)
    initial="\n".join(message["content"] for message in wire)
    assert "EXACT DETAILS" not in initial
    current=decode_records(wire)
    assert current["runtime.tool_contracts"]["selected"]["parameters"]["required"]==["path"]
    assert current["runtime.tool_contracts"]["selected"]["detail_state"]=="on_demand"
    assert "detail_state" not in current["runtime.tool_contracts"]["small"]
    calls=[{"name":"selected","id":"native-intent","args":{"path":"one.txt"}}]
    reviews=[];events=[]
    proposal=ToolProposalRuntime(process,process.session,lambda:tools,
        lambda **kw:reviews.append(kw),lambda event:events.append(event))
    result=proposal.review(calls,"")
    assert result.retry and not reviews and not process.tool_receipts
    supplied=process.session.messages[-1]
    assert supplied["data"]["definition"]==tool_definition(tools["selected"])
    assert supplied["data"]["execution_state"]=="not_started"
    assert supplied["host_authority"] and supplied["context_purpose"]=="tool_contract"
    assert "EXACT DETAILS selected" in supplied["content"]
    assert "EXACT DETAILS unrelated" not in supplied["content"]
    assert events[0]["event"]=="tool_context_prepared" and events[0]["executed"] is False
    assert wire==original
    assert not required_tool_context(process,calls,tools)
    assert proposal.review(calls,"").result is None and len(reviews)==1
    assert not process.approval_grants


def test_loaded_details_do_not_grant_original_pipeline_permissions(tmp_path_factory):
    from backend.core.loop.execution_context import ExecutionContext
    from backend.core.loop.event_bus import EventBus
    from backend.core.loop.tool_execution import ToolExecution
    process,tools,_,_=fixture_process(tmp_path_factory)
    call={"name":"selected","id":"native","args":{"path":"one"}}
    provide_tool_context(process,[call],required_tool_context(process,[call],tools))
    ctx=ExecutionContext(process=process,session=process.session,workspace_path=str(tmp_path_factory),event_bus=EventBus())
    result=ToolExecution("original-pipeline",ctx,[call]).execute_batch(tools)[0]
    assert result.is_error and not process.approval_grants


def test_changed_definition_retires_prior_view_and_quoted_text_never_counts(tmp_path_factory):
    process,tools,_,_=fixture_process(tmp_path_factory)
    call={"name":"selected","id":"one","args":{}}
    process.session.append_user("EXACT DETAILS selected "+deterministic_hash(tool_definition(tools["selected"])))
    required=required_tool_context(process,[call],tools)
    assert required
    provide_tool_context(process,[call],required)
    saved=deepcopy(process.session.messages[-1])
    tools["selected"].description+="\nNEW REGISTERED DETAIL"
    assert required_tool_context(process,[call],tools)
    assert process.session.messages[-1]==saved
    process.session.messages[-1]["provider_visible"]=False
    assert required_tool_context(process,[call],tools)


def test_retired_tools_leave_the_current_interface_and_old_full_seed_is_rebased(tmp_path_factory):
    process,tools,route,_=fixture_process(tmp_path_factory)
    process.session.provider_context_seed={"scope":"task","route":"route",
        "values":{"runtime.tool_contracts":{"unrelated":tool_definition(tools["unrelated"])}}}
    route.prepare_envelope([tools["small"].to_openai_function()])
    wire=process.session.to_provider_messages(dynamic_envelope=route.envelope,provider_route="route")
    assert set(decode_records(wire)["runtime.tool_contracts"])=={"small"}
    assert "EXACT DETAILS" not in "\n".join(message["content"] for message in wire)
    from backend.core.loop.provider_context import SEED_POLICY
    assert process.session.provider_context_seed["policy"]==SEED_POLICY


def test_compact_interfaces_need_no_extra_preparation(tmp_path_factory):
    process,tools,_,_=fixture_process(tmp_path_factory)
    assert not required_tool_context(process,[{"name":"small","id":"small","args":{}}],tools)
    assert reference_catalog(process.session,[tools["small"].to_openai_function()],scope="task")=={
        "small":tools["small"].to_openai_function()["function"]}


def test_contract_help_follows_each_operation_and_discovery_only_covers_its_view(tmp_path_factory):
    from backend.core.loop.executor import _build_internal_tools
    from backend.core.loop.tool_discovery import discover, mark_discovery_result

    process, _, route, _ = fixture_process(tmp_path_factory)
    tool = _build_internal_tools(process, {}, str(tmp_path_factory))["declare_task_contract"]
    tools = {tool.name: tool}
    route.prepare_envelope([tool.to_openai_function()])
    process.session.to_provider_messages(dynamic_envelope=route.envelope, provider_route="route")
    early = discover(process, tools, {"operation": "describe", "name": tool.name})
    assert "## First declaration" in early["definition"]["description"]
    assert "## Revise" not in early["definition"]["description"]
    process.session.append_tool_result("shown", tool_name="capability_status", data=early)
    mark_discovery_result(process, process.session.messages[-1], tools)
    process.session.request_tool_contracts = visible_tool_contracts(process.session.messages, process.session.session_id)
    declare = {"name": tool.name, "id": "declare", "args": {}}
    assert not required_tool_context(process, [declare], tools)

    process.replace_context_snapshot({"task_contract": {"revision": 1}})
    later = discover(process, tools, {"operation": "describe", "name": tool.name})
    assert "## Revise" in later["definition"]["description"]
    assert "## First declaration" not in later["definition"]["description"]
    assert "## Withdraw" not in later["definition"]["description"]
    assert later["definition"]["parameters"] == early["definition"]["parameters"] == tool.advertised_parameters
    revise = {"name": tool.name, "id": "revise", "args": {"operation": "revise", "goal": "Refine scope"}}
    withdraw = {"name": tool.name, "id": "withdraw", "args": {"operation": "revise",
        "maintenance": {"resolve_changes": [{"id": "change", "assignment_scope": {}}]}}}
    handoff = {"name": tool.name, "id": "handoff", "args": {"operation": "revise",
        "routing_transition": "handoff_to_worker"}}
    calls = [declare, revise, withdraw, handoff]
    needs = required_tool_context(process, calls, tools)
    assert set(needs) == {1, 2, 3}
    provide_tool_context(process, calls, needs)
    receipts = process.session.messages[-len(calls):]
    assert [row["tool_call_id"] for row in receipts] == ["declare", "revise", "withdraw", "handoff"]
    assert all(not row["data"]["executed"] for row in receipts)
    assert "## Withdraw" not in receipts[1]["content"]
    assert "## Withdraw" in receipts[2]["content"] and "## Handoff" not in receipts[2]["content"]
    assert "## Handoff" in receipts[3]["content"] and "## Withdraw" not in receipts[3]["content"]
    assert not process.tool_receipts


def test_new_task_projects_old_manual_while_preserving_native_batch_and_receipt(tmp_path_factory):
    process, tools, route, _ = fixture_process(tmp_path_factory)
    session = process.session
    session.append_user("Keep UTF-8.", turn_id=process.active_task_id)
    calls = [{"name": "selected", "id": "old-manual", "args": {}},
             {"name": "small", "id": "old-observation", "args": {}}]
    session.append_assistant("", tool_calls=deepcopy(calls))
    provide_tool_context(process, calls[:1], required_tool_context(process, calls[:1], tools))
    session.append_tool_result("Independent verification passed", tool_name="small", tool_call_id="old-observation")
    wire = session.to_provider_messages(dynamic_envelope=route.envelope, provider_route="route")
    assert "EXACT DETAILS selected" in "\n".join(row["content"] for row in wire)
    current = {"name": "selected", "id": "new-manual", "args": {}}
    assert not required_tool_context(process, [current], tools)

    original = deepcopy(session.messages)
    process.active_task_id = "successor"
    session.append_user("Continue the work in a new task.", turn_id=process.active_task_id)
    route.prepare_envelope([tool.to_openai_function() for tool in tools.values()])
    wire = session.to_provider_messages(dynamic_envelope=route.envelope, provider_route="route")
    content = "\n".join(row["content"] for row in wire)
    assert "EXACT DETAILS selected" not in content
    assert "Keep UTF-8." in content and "Independent verification passed" in content
    assert [row["tool_call_id"] for row in wire if row["role"] == "tool"] == ["old-manual", "old-observation"]
    assert session.messages[:-1] == original
    session.request_tool_contracts = visible_tool_contracts(wire, session.session_id)
    needed = required_tool_context(process, [current], tools)
    assert needed[0][0]["name"] == "selected"
    session.append_assistant("", tool_calls=[deepcopy(current)])
    provide_tool_context(process, [current], needed)
    assert required_tool_context(process, [current], tools)  # Not sent yet.
    wire = session.to_provider_messages(dynamic_envelope=route.envelope, provider_route="route")
    session.request_tool_contracts = visible_tool_contracts(wire, session.session_id)
    assert not required_tool_context(process, [current], tools)


@pytest.mark.parametrize(('platform', 'shell_detail'), [
    ('win32', 'use Windows PowerShell 5.1-compatible syntax'),
    ('linux', 'requires a verified Bash'),
    ('darwin', 'macOS helper accepts exec_command producers'),
])
def test_native_tool_overviews_and_platform_details(tmp_path_factory, monkeypatch, platform, shell_detail):
    from backend.core.tools import catalog
    from backend.core.prompt_templates import load_prompt

    monkeypatch.setattr(catalog, 'sys', SimpleNamespace(platform=platform))
    tools = catalog.build_workspace_tools(tmp_path_factory)
    command_definition = tool_definition(tools['exec_command'])
    script_definition = tool_definition(tools['shell_script'])
    command = contract_overview(command_definition)
    assert 'kind=test' in command['description']
    assert 'run_test' in command['description'] and 'test_id' in command['description']
    script = contract_overview(script_definition)
    assert 'pipeline' in script['description'] and 'exec_command' in script['description']
    shared = load_prompt('tools/native_workspace_publication').strip()
    for overview, definition in ((command, command_definition), (script, script_definition)):
        description = definition['description']
        first_line = description.splitlines()[0]
        assert overview['description'].splitlines()[0] == first_line and len(first_line) <= 240
        if overview.get('detail_state') == 'on_demand':
            assert overview['description'] == first_line
        assert description.count(shared) == 1
    script_details = script_definition['description']
    assert shell_detail in script_details
    assert ('use Windows PowerShell 5.1-compatible syntax' in script_details) == (platform == 'win32')
    assert ('macOS helper accepts exec_command producers' in script_details) == (platform == 'darwin')


def test_one_exact_definition_pairs_repeated_native_intents_without_duplicate_payload(tmp_path_factory):
    process,tools,route,_=fixture_process(tmp_path_factory)
    calls=[{'id':str(index),'name':'selected','args':{}} for index in range(3)]
    process.session.append_assistant('',tool_calls=deepcopy(calls))
    provide_tool_context(process,calls,required_tool_context(process,calls,tools))
    results=[message for message in process.session.messages if message.get('role')=='tool']
    assert [item['tool_call_id'] for item in results]==['0','1','2']
    assert sum('definition' in item['data'] for item in results)==1
    wire=process.session.to_provider_messages(dynamic_envelope=route.envelope,provider_route='route')
    supplied=[m for m in wire if m.get('context_purpose')=='tool_contract' and m.get('data',{}).get('definition')]
    assert len(supplied)==1 and supplied[0]['data']['definition']==tool_definition(tools['selected'])


def test_changed_details_pause_parallel_suffix_and_never_replay_committed_prefix(runtime):
    _,process,effects,_,_,batch,_=runtime
    reads=[]
    selected=AgentTool('selected','Purpose\n'+'Exact registered detail '*300,
        {'type':'object','properties':{}},lambda args:reads.append('selected') or {'ok':True},
        execution_contract=data_broker('test.context'))
    sibling=AgentTool('sibling','Read another value',{'type':'object','properties':{}},
        lambda args:reads.append('sibling') or {'ok':True},execution_contract=data_broker('test.context'))
    def update(args):
        effects.append('prefix once')
        selected.description+='\nNEW CURRENT TOOL CONTRACT'
        return {'ok':True}
    prefix=AgentTool('prefix','Change the registered fixture definition',{'type':'object','properties':{}},
        update,effect=ToolEffect.PROCESS,execution_contract=data_broker('test.context'))
    tools={tool.name:tool for tool in (prefix,sibling,selected)}
    for name in tools:process.tool_registry.add_scoped(name)
    batch.get_tools=lambda:tools
    route=ProviderRouteRuntime(process,process.session,None,'route',None,lambda:None)
    route.prepare_envelope([tool.to_openai_function() for tool in tools.values()])
    process.session.to_provider_messages(dynamic_envelope=route.envelope,provider_route='route')
    old=[{'name':'selected','id':'old-view','args':{}}]
    process.session.append_assistant('',tool_calls=old)
    provide_tool_context(process,old,required_tool_context(process,old,tools))
    values=[{'name':name,'id':name,'args':{}} for name in ('prefix','sibling','selected')]
    process.session.append_assistant('',tool_calls=deepcopy(values))
    assert not required_tool_context(process,values,tools)
    result=batch.execute_live_batch(values)
    assert result.retry and effects==['prefix once'] and reads==[]
    outputs=[m for m in process.session.messages if m.get('role')=='tool']
    assert [m['tool_call_id'] for m in outputs][-3:]==['prefix','sibling','selected']
    assert outputs[-1]['data']['definition']==tool_definition(selected)
    assert not process.pending_tool_batch and not process.approval_grants
    refined=[{**call,'id':call['id']+'-refined'} for call in values[1:]]
    process.session.append_assistant('',tool_calls=deepcopy(refined))
    result=batch.execute_live_batch(refined)
    assert not result.retry and not result.result
    assert effects==['prefix once'] and sorted(reads)==['selected','sibling']
