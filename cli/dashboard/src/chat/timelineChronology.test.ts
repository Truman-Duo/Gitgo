import {test,expect} from 'bun:test';
import {initStreamState,reduceStreamEvent} from '../daemon/streamReducer.js';
import {timelineFromTrace} from './turnTimeline.js';
const events:any[]=[
 {event:'text_delta',process_id:'a',delta:'I will check the boundary.'},
 {event:'reasoning_delta',process_id:'a',delta:'first private reasoning'},
 {event:'toolcall_start',process_id:'a',tool_name:'read_file',tool_call_id:'read'},
 {event:'tool_result',process_id:'a',tool_name:'read_file',tool_call_id:'read',is_error:false,result_preview:'file read'},
 {event:'reasoning_delta',process_id:'a',delta:'second private reasoning'},
 {event:'progress_summary',process_id:'a',message:'The boundary is clear; I will verify callers.'},
 {event:'toolcall_start',process_id:'a',tool_name:'search_text',tool_call_id:'search'},
 {event:'tool_result',process_id:'a',tool_name:'search_text',tool_call_id:'search',is_error:false},
 {event:'text_delta',process_id:'a',delta:'Final answer'},
];
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
