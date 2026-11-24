import {expect,test} from "bun:test";
import {projectConversationRounds} from "./conversationRounds.js";
import {timelineFromTrace,withProcessMessages} from "./turnTimeline.js";
import type {ChatMessage} from "../types.js";
import {runtimeNoticeFromEvent} from "../daemon/runtimeNotices.js";

test("unscoped historical answers remain independent records rather than becoming one invented turn",()=>{
  const original:ChatMessage[]=Array.from({length:100},(_,index)=>({role:"assistant",content:`answer-${index}`,message_id:`m:${index}`,timestamp:"now"}));
  expect(projectConversationRounds(original,null)).toEqual(original);
});
test("same-named records from different task or process identities never collapse together",()=>{
  const messages:ChatMessage[]=[
    {role:"user",content:"first",timestamp:"now",turn_id:"t1"},
    {role:"assistant",kind:"progress",content:"Same text",timestamp:"now",turn_id:"t1",process_id:"A",update_id:"u"},
    {role:"assistant",content:"first answer",timestamp:"now",turn_id:"t1",status:"completed"},
    {role:"user",content:"second",timestamp:"now",turn_id:"t2"},
    {role:"assistant",kind:"progress",content:"Same text",timestamp:"now",turn_id:"t2",process_id:"A",update_id:"u"},
    {role:"assistant",kind:"progress",content:"Same text",timestamp:"now",turn_id:"t2",process_id:"B",update_id:"u"},
    {role:"assistant",content:"second answer",timestamp:"now",turn_id:"t2",status:"completed"},
  ];
  const rows=projectConversationRounds(messages,null);
  expect(rows).toHaveLength(4);expect(rows[1]?.process_messages).toHaveLength(1);expect(rows[3]?.process_messages).toHaveLength(2);
});
test("a saved question replaces its matching trace row in place and owns its recorded answer",()=>{
  const question:any={decision_id:"q",task_id:"t",process_id:"A",question:"Original question",why_user_must_decide:"Required",options:[],allow_free_form:true};
  const model=timelineFromTrace([
    {seq:1,time:"",event:"toolcall_start",process_id:"A",tool_name:"before",tool_call_id:"before"},
    {seq:2,time:"",event:"decision_required",process_id:"A",decision_id:"q",decision:question},
    {seq:3,time:"",event:"user_decision_received",process_id:"A",decision_id:"q",answer:"Original answer"},
    {seq:4,time:"",event:"decision_required",process_id:"A",decision_id:"q",decision:question},
    {seq:5,time:"",event:"toolcall_start",process_id:"A",tool_name:"after",tool_call_id:"after"},
  ]);
  const original=JSON.stringify(model);
  const result=withProcessMessages(model,[{role:"assistant",content:"Original question",timestamp:"now",turn_id:"t",decision:question,decision_answer:"Original answer",message_id:"decision:q"}],"t");
  expect(result.activity.map(item=>item.kind)).toEqual(["tool","notice","tool"]);
  expect(result.notices.find(item=>item.decision)?.decision_answer).toBe("Original answer");
  expect(JSON.stringify(model)).toBe(original);
});

test("expanded notice detail preserves the original full human-readable message",()=>{
  const message="Original warning\n"+"中文".repeat(200)+" EXACT END";
  const event:any={event:"architecture_warning",process_id:"A",message};
  expect(runtimeNoticeFromEvent(event)?.detail).not.toContain("EXACT END");
  expect(runtimeNoticeFromEvent(event,true)?.detail).toBe(message);
});

test("answered input gets one external summary and one original full card without another task or message write",()=>{
  const decision:any={task_id:"t",process_id:"a",decision_id:"q",question:"What should I inspect?",options:[],allow_free_form:true};
  const original:ChatMessage[]=[
    {role:"user",turn_id:"t",message_id:"user:t",content:"Inspect",timestamp:"now"},
    {role:"assistant",decision,decision_answer:"THE USER'S ADDITIONAL PROMPT",message_id:"decision:q",content:decision.question,timestamp:"now"},
    {role:"assistant",decision,decision_answer:"THE USER'S ADDITIONAL PROMPT",message_id:"decision:q:refresh",content:decision.question,timestamp:"now"},
    {role:"assistant",turn_id:"t",message_id:"outcome:t",kind:"outcome",status:"completed",content:"FINAL",timestamp:"now"},
  ];
  const before=JSON.stringify(original),result=projectConversationRounds(original,null);
  expect(result.map(item=>item.kind || item.role)).toEqual(["user","decision_summary","outcome"]);
  expect(result[1]).toMatchObject({decision,decision_answer:"THE USER'S ADDITIONAL PROMPT"});
  expect(result[2]!.process_messages).toHaveLength(1);
  expect(result[2]!.process_messages![0]!.decision_answer).toBe(result[1]!.decision_answer);
  expect(JSON.stringify(original)).toBe(before);
});

test("a later recorded answer replaces the unanswered card for both compact and full views",()=>{
  const decision:any={task_id:"t",process_id:"a",decision_id:"q",question:"A question",options:[],allow_free_form:true};
  const original:ChatMessage[]=[
    {role:"assistant",decision,content:"A question",timestamp:"now",message_id:"decision:q"},
    {role:"assistant",decision,decision_answer:"RECORDED REPLY",content:"A question",timestamp:"now",message_id:"decision:q"},
    {role:"assistant",turn_id:"t",content:"FINAL",status:"completed",timestamp:"now"},
  ];
  const result=projectConversationRounds(original,null);
  expect(result.filter(item=>item.kind==="decision_summary")).toHaveLength(1);
  expect(result.at(-1)!.process_messages![0]!.decision_answer).toBe("RECORDED REPLY");
});
