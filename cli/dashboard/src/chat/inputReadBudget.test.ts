import {expect,test} from "bun:test";
import {traceBodyReader} from "./traceSource.js";
const tick=()=>new Promise(resolve=>setTimeout(resolve,10));

test("source reads coalesce, allow at most two observations, and drop cancelled queued reads",async()=>{
  const calls:any[]=[],replies:Array<(value:any)=>void>=[];
  const client:any={callTool:async(name:string,args:any)=>{
    calls.push({name,args});
    return new Promise(resolve=>replies.push(resolve));
  }};
  const read=traceBodyReader(client,"project","turn"),a=new AbortController(),b=new AbortController(),c=new AbortController(),again=new AbortController();
  const first=read.inputSources!("a",a.signal),duplicate=read.inputSources!("a",again.signal);
  const second=read.inputSources!("b",b.signal);
  const abandoned=read.inputSources!("c",c.signal).catch(error=>error.name);
  c.abort();
  await tick();expect(calls).toHaveLength(2);
  for(const reply of replies)reply({detail:{representation:"canonical_provider_input_sources",input_provenance:{schema_version:1,sources:[]}}});
  await Promise.all([first,duplicate,second]);expect(await abandoned).toBe("AbortError");
  expect(calls).toHaveLength(2);
  expect(calls.every(call=>call.args.view==="input_sources")).toBe(true);
});

test("page ownership and bounded cursor metadata are checked before cache admission",async()=>{
  const client:any={callTool:async()=>({detail:{representation:"canonical_provider_input_source_page",
    source_index:0,offset:0,next_offset:1,total_chars:2,has_more:true,text:"x"}})};
  const read=traceBodyReader(client,"p","t");
  await expect(read.inputSource!("saved",0,0,new AbortController().signal)).rejects.toThrow("TRACE_INPUT_PAGE_INVALID");
});
