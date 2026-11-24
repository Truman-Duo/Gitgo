import {test,expect} from 'bun:test';
import {initStreamState,reduceStreamEvent} from '../daemon/streamReducer.js';
import {timelineFromTrace} from './turnTimeline.js';
const events:any[]=[
 {event:'text_delta',process_id:'a',delta:'I will check the boundary.',visibility:'public'},
 {event:'reasoning_delta',process_id:'a',delta:'first private reasoning'},
 {event:'toolcall_start',process_id:'a',tool_name:'read_file',tool_call_id:'read'},
 {event:'tool_result',process_id:'a',tool_name:'read_file',tool_call_id:'read',is_error:false,result_preview:'file read'},
 {event:'reasoning_delta',process_id:'a',delta:'second private reasoning'},
 {event:'progress_summary',process_id:'a',message:'The boundary is clear; I will verify callers.'},
 {event:'toolcall_start',process_id:'a',tool_name:'search_text',tool_call_id:'search'},
 {event:'tool_result',process_id:'a',tool_name:'search_text',tool_call_id:'search',is_error:false},
 {event:'text_delta',process_id:'a',delta:'Final answer'},
];
test('a public text channel never relabels an earlier private response buffer',()=>{
 let state=reduceStreamEvent(initStreamState(),{event:'text_delta',process_id:'a',delta:'Internal draft',visibility:'verbose'});
 state=reduceStreamEvent(state,{event:'text_delta',process_id:'a',delta:'Public response',visibility:'public'});
 expect(state.visibility).toBe('public');expect(state.text).toBe('Public response');
 expect(state.activity[0]).toMatchObject({kind:'progress',visibility:'verbose',text:'Internal draft'});
});

test('history inherits only an explicit request visibility and keeps unclassified legacy text private',()=>{
 for(const task_kind of ['answer','supervisor',undefined]){
  const events:any[]=[{event:'provider_request_started',process_id:'a',task_kind},
   {event:'text_delta',process_id:'a',delta:'prose'},
   {event:'toolcall_start',process_id:'a',tool_name:'read_file'}];
  let live=initStreamState();for(const event of events)live=reduceStreamEvent(live,event);
  const history=timelineFromTrace(events.map((event,seq)=>({...event,seq,time:''})),'a');
  const visibility=task_kind==='answer'?'public':'verbose';
  expect(live.activity.find(item=>item.kind==='progress')).toMatchObject({kind:'progress',visibility});
  expect(history.activity.find(item=>item.kind==='progress')).toMatchObject({kind:'progress',visibility});
 }
});
test('live and durable chronology keeps commentary in its original position across thinking and tools',()=>{
 let state=initStreamState();for(const event of events)state=reduceStreamEvent(state,event);
 const history=timelineFromTrace(events.map((event,i)=>({...event,seq:i+1,time:''})), 'a');
 const settled=state.activity.filter(item=>item.kind!=='draft');
 expect(settled.map(item=>item.kind)).toEqual(['progress','reasoning','tool','reasoning','progress','tool']);
 expect(history.activity.map(item=>item.kind)).toEqual(settled.map(item=>item.kind));
 expect(settled[0]).toMatchObject({kind:'progress',text:'I will check the boundary.',visibility:'public'});
 expect(history.activity.some(item=>'text' in item&&item.text==='Final answer')).toBe(false);
 expect(state.text).toBe('Final answer');
});

for(const boundary of ['toolcall_start','provider_request_started'])
test(`live and history preserve mixed visibility at ${boundary} instead of promoting private drafts`,()=>{
 const events:any[]=[
  {event:'text_delta',process_id:'a',delta:'Internal draft',visibility:'verbose'},
  {event:'text_delta',process_id:'a',delta:'Public update',visibility:'public'},
  {event:boundary,process_id:'a',tool_name:'read_file',tool_call_id:'read',task_kind:'answer'},
  {event:'text_delta',process_id:'a',delta:'Final answer',visibility:'public'},
 ];
 let live=initStreamState();for(const event of events)live=reduceStreamEvent(live,event);
 const history=timelineFromTrace(events.map((event,i)=>({...event,seq:i+1,time:''})), 'a');
 const progress=(items:typeof live.activity)=>items.filter(item=>item.kind==='progress').map(item=>({text:item.text,visibility:item.visibility}));
 expect(progress(live.activity)).toEqual([{text:'Internal draft',visibility:'verbose'},{text:'Public update',visibility:'public'}]);
 expect(progress(history.activity)).toEqual(progress(live.activity));
 expect(history.activity.some(item=>'text' in item&&item.text==='Final answer')).toBe(false);
});
