import {expect,test} from "bun:test";
import {initStreamState,reduceStreamEvent} from "../daemon/streamReducer.js";
import {timelineFromTrace} from "./turnTimeline.js";
import {NativeHostClient} from "../backend/client.js";
import {sendChat} from "./sendChat.js";

test("legacy runtime preparation is omitted by protocol identity while real Agent prose is preserved",()=>{
  const preparation="Understanding the request and preparing the next action.";
  const events:any[]=[
    {event:"progress_summary",phase:"provider",process_id:"a",message:preparation},
    {event:"progress_summary",phase:"commentary",process_id:"a",update_id:"agent",message:"我先看一下目录结构和顶层文件。"},
    {event:"progress_summary",phase:"commentary",process_id:"a",update_id:"literal",message:preparation},
  ];
  const original=JSON.stringify(events);
  let live=initStreamState();for(const event of events)live=reduceStreamEvent(live,event);
  const history=timelineFromTrace(events.map((event,seq)=>({...event,seq,time:""})),"a");
  for(const model of [live,history])expect(model.activity.filter(item=>item.kind==="progress").map(item=>item.text))
    .toEqual(["我先看一下目录结构和顶层文件。",preparation]);
  expect(JSON.stringify(events)).toBe(original);
});

test("native admission acknowledgements do not manufacture public explanations",async()=>{
  const rows:any[]=[],acks:any[]=[];
  class Client extends NativeHostClient{
    get ready(){return true;}
    async sendTaskStreaming(_args:any,cb:any){
      cb.onAcknowledged({stage:"accepted",request_id:"r"});
      cb.onAcknowledged({stage:"admitted",request_id:"r",task_id:"t",process_id:"a"});
      cb.onChunk({event:"progress_summary",process_id:"a",update_id:"u",message:"ACTUAL AGENT EXPLANATION"});
      cb.onComplete({outcome:{task_id:"t",process_id:"a",status:"completed",response:"FINAL"}});
    }
  }
  await sendChat(new Client("unused","unused"),"fixture","Inspect",new Date().toISOString(),null,{
    onStream:row=>rows.push(row),onAcknowledged:ack=>acks.push(ack),onDone(){},onError:error=>{throw new Error(String(error));},
  });
  expect(acks.map(item=>item.stage)).toEqual(["accepted","admitted"]);
  expect(rows.slice(0,2).every(row=>row.activity.length===0)).toBe(true);
  expect(rows.at(-1).activity.filter((item:any)=>item.kind==="progress").map((item:any)=>item.text)).toEqual(["ACTUAL AGENT EXPLANATION"]);
});
