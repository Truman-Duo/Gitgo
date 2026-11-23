import {expect, test} from "bun:test";
import type {ChatMessage, StreamingRow} from "../types.js";
import {initStreamState, reduceStreamEvent} from "../daemon/streamReducer.js";
import {timelineFromTrace,withProcessMessages} from "./turnTimeline.js";
import {projectConversationRounds} from "./conversationRounds.js";

const updates: any[] = [
  {event:"progress_delta", process_id:"a", task_id:"turn", update_id:"u", delta:"Confirmed; "},
  {event:"reasoning_delta", process_id:"a", delta:"private reasoning"},
  {event:"toolcall_start", process_id:"b", tool_name:"read_file", tool_call_id:"read"},
  {event:"progress_delta", process_id:"a", task_id:"turn", update_id:"u", delta:"checking callers."},
  {event:"progress_summary", process_id:"a", task_id:"turn", update_id:"u", message:"Confirmed; checking callers."},
  {event:"progress_summary", process_id:"a", task_id:"turn", update_id:"u", message:"Confirmed; checking callers."},
];

test("live and history preserve explicit progress identity through interleaved tools and terminal summaries", () => {
  let live = initStreamState();
  for (const event of updates) live = reduceStreamEvent(live, event);
  const history = timelineFromTrace(updates.map((event, index) => ({...event, seq:index+1, time:""})), "a");
  for (const model of [live, history]) {
    expect(model.activity.map(item => item.kind)).toEqual(["progress", "reasoning", "tool"]);
    expect(model.activity[0]).toMatchObject({text:"Confirmed; checking callers.", visibility:"public", update_id:"u"});
  }
  expect(live.text).toBe("");
});

const outcome: ChatMessage = {role:"assistant", content:"Final answer", timestamp:"now", turn_id:"turn",
  process_id:"a", message_id:"outcome:turn", status:"completed", trace_id:"turn",
  activity:[{kind:"progress", visibility:"public", text:"Confirmed; checking callers.", update_id:"u", process_id:"a"},
    {kind:"progress", visibility:"verbose", text:"private draft", process_id:"a"}]};
const saved: ChatMessage = {role:"assistant", content:"Confirmed; checking callers.", timestamp:"now",
  kind:"progress", turn_id:"turn", update_id:"u", process_id:"a", message_id:"progress:turn:a:u"};

test("completion and re-entry place saved progress inside one answer's process", () => {
  const before=JSON.stringify([saved,outcome]);
  const rows=projectConversationRounds([saved,outcome],null);
  expect(rows.map(row=>row.content)).toEqual(["Final answer"]);
  expect(rows[0]?.process_messages).toEqual([saved]);
  const model=withProcessMessages({activity:outcome.activity!,tools:[],notices:[]},rows[0]!.process_messages,"turn");
  expect(model.activity.filter(item=>item.kind==="progress" && item.visibility==="public")).toHaveLength(1);
  expect(model.activity.some(item=>item.kind==="progress" && item.visibility==="verbose")).toBe(true);
  const reopened=projectConversationRounds([saved,{...outcome,activity:undefined,public_progress_keys:undefined}],null);
  expect(reopened[0]?.process_messages).toEqual([saved]);
  expect(JSON.stringify([saved,outcome])).toBe(before);
});

test("an active stream owns its updates when checkpoint polling catches up", () => {
  let live=initStreamState();for(const event of updates)live=reduceStreamEvent(live,event);
  expect(projectConversationRounds([saved],{...live,timestamp:"now"})).toEqual([]);
});

test("another task never absorbs a progress record with a reused update id", () => {
  const rows=projectConversationRounds([saved,{...outcome,turn_id:"next"}],null);
  expect(rows[1]?.process_messages).toBeUndefined();
  expect(rows[0]).toBe(saved);
  let live=initStreamState();live=reduceStreamEvent(live,updates[0]);live=reduceStreamEvent(live,{...updates[0],process_id:"b"});
  expect(live.activity).toHaveLength(2);
});

test("a canonical outcome replaces its optimistic copy and preserves unrelated error facts inside the process", () => {
  const user:ChatMessage={role:"user",content:"question",timestamp:"now",turn_id:"turn"};
  const optimistic={...outcome,message_id:"optimistic:answer",awaiting_persistence:true};
  const diagnostic:ChatMessage={role:"system",content:"UI transport observation",timestamp:"now",turn_id:"turn",message_id:"transport-observation"};
  const rows=projectConversationRounds([user,optimistic,outcome,{...outcome},diagnostic],null);
  expect(rows).toHaveLength(2);expect(rows[1]?.content).toBe("Final answer");
  expect(rows[1]?.process_messages).toEqual([diagnostic]);
});

test("progress indices are scoped to tasks and immutable live snapshots stay unchanged", () => {
  const first = reduceStreamEvent(initStreamState(), updates[0]);
  const before = JSON.stringify(first.activity);
  const second = reduceStreamEvent(first, {...updates[0], task_id:"another-task", delta:"Different update"});
  const third = reduceStreamEvent(second, updates[3]);
  expect(first.publicProgressIndex.size).toBe(1);
  expect(second.publicProgressIndex.size).toBe(2);
  expect(JSON.stringify(first.activity)).toBe(before);
  expect(third.activity).toHaveLength(2);
  expect(third.activity[0]).toMatchObject({text:"Confirmed; checking callers."});
  expect(third.activity[1]).toMatchObject({text:"Different update"});
});
