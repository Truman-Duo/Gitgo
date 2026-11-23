import {expect,test} from "bun:test";
import {recordedInputSources} from "./requestInputs.js";
import {initStreamState,reduceStreamEvent} from "../daemon/streamReducer.js";
import {timelineFromTrace} from "./turnTimeline.js";

test("source content and missing versions come only from the saved canonical request",()=>{
  const detail={representation:"canonical_provider_input",input_provenance:{schema_version:1,sources:[
    {message_index:0,message_type:"project_rules",sections:[{name:"AGENTS.md",provenance:"project",version:"saved-v1"}]},
    {message_index:2,message_type:"lesson",sections:[{name:"Lesson",provenance:"memory"}]},
  ]},messages:[{content:"SAVED RULES"},{content:"USER QUESTION"},{content:"SAVED LESSON"}]};
  const before=JSON.stringify(detail),result=recordedInputSources(detail);
  expect(result.exact).toBe(true);expect(result.sources.map(item=>item.content)).toEqual(["SAVED RULES","SAVED LESSON"]);
  expect(result.sources[1]!.source.sections[0].version).toBeUndefined();
  expect(JSON.stringify(detail)).toBe(before);
  expect(recordedInputSources({...detail,representation:"prepared_runtime_projection"}).sources).toEqual([]);
  expect(recordedInputSources(undefined).exact).toBe(false);
});

test("the exact parameters refine the same prepared request without a duplicate context card",()=>{
  const events:any[]=[
    {event:"provider_request_started",seq:1,time:"",process_id:"a",model:"m",detail_ref:"prepared"},
    {event:"provider_request_parameters",seq:2,time:"",process_id:"a",model:"m",detail_ref:"exact",request_parameters:{model:"m"}},
    {event:"reasoning_delta",seq:3,time:"",process_id:"a",delta:"private"},
  ];
  let live=initStreamState();for(const event of events)live=reduceStreamEvent(live,event);
  const history=timelineFromTrace(events,"a");
  for(const model of [live,history]){
    const contexts=model.activity.filter(item=>item.kind==="context");
    expect(contexts).toHaveLength(1);
    expect(contexts[0]).toMatchObject({request:{detail_ref:"exact",event:"provider_request_parameters"}});
  }
});

test("request retries remain distinct and duplicate observations do not add cards",()=>{
  let state=initStreamState();
  const first:any={event:"provider_request_started",seq:1,process_id:"a",model:"m",detail_ref:"first"};
  state=reduceStreamEvent(state,first);state=reduceStreamEvent(state,first);
  state=reduceStreamEvent(state,{...first,seq:2,detail_ref:"retry"});
  expect(state.activity.filter(item=>item.kind==="context")).toHaveLength(2);
  state=reduceStreamEvent(state,{...first,trace_id:"next-turn",detail_ref:"next-request"});
  expect(state.activity.filter(item=>item.kind==="context")).toHaveLength(3);
});

test("context telemetry cannot split one public answer continuation into repeated progress",()=>{
  const events:any[]=[
    {event:"text_delta",process_id:"a",visibility:"public",delta:"First fragment"},
    {event:"provider_request_started",process_id:"a",answer_continuation:true,task_kind:"answer",detail_ref:"context"},
    {event:"text_delta",process_id:"a",visibility:"public",delta:" and continuation"},
    {event:"toolcall_start",process_id:"a",tool_name:"read_file"},
  ];
  let live=initStreamState();for(const event of events)live=reduceStreamEvent(live,event);
  const history=timelineFromTrace(events.map((event,seq)=>({...event,seq,time:""})),"a");
  for(const model of [live,history]){
    expect(model.activity.filter(item=>item.kind==="progress")).toMatchObject([{text:"First fragment and continuation"}]);
    expect(model.activity.filter(item=>item.kind==="context")).toHaveLength(1);
  }
});

test("injection metadata does not start thinking; only the provider phase owns its thinking clock",()=>{
  let state=reduceStreamEvent(initStreamState(),{event:"provider_request_parameters",process_id:"a",time:"2026-11-09T00:00:00Z",detail_ref:"context"});
  expect(state.thinking).toBe(false);expect(state.thinkingStartedAt).toBeUndefined();
  state=reduceStreamEvent(state,{event:"provider_request_started",process_id:"a",time:"2026-11-09T00:00:20Z"});
  expect(state.thinkingStartedAt).toBe("2026-11-09T00:00:20Z");
  state=reduceStreamEvent(state,{event:"reasoning_delta",process_id:"a",delta:"current"});
  expect(state.thinkingStartedAt).toBe("2026-11-09T00:00:20Z");
  state=reduceStreamEvent(state,{event:"progress_summary",process_id:"a",update_id:"u",message:"Public stage"});
  expect(state.thinking).toBe(false);expect(state.thinkingStartedAt).toBeUndefined();
});
