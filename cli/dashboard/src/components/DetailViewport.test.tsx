import React,{createRef,useEffect} from "react";
import {expect,test} from "bun:test";
import {PassThrough} from "node:stream";
import {Box,Text,ScrollBox,renderSync,getElementRect,type DOMElement,type ScrollBoxHandle} from "@anthropic/ink";
import instances from "../../vendor/ink/src/core/instances.js";
import {cellAt} from "../../vendor/ink/src/core/screen.js";
import {InputProvider} from "../input/runtime.js";
import {ContentViewport,useContentViewport} from "../ui/ContentViewport.js";
import {DetailViewport} from "./DetailViewport.js";
import {TraceSourceText} from "./TraceSourceText.js";
import {ToolCallDisplay} from "./ToolCallDisplay.js";
import {RequestContextRow} from "./RequestContextRow.js";
import {StageReport} from './StageReport.js';
import {TurnTimeline} from './TurnTimeline.js';
import {traceBodyReader} from "../chat/traceSource.js";
import {useScrollInput} from "../input/runtime.js";
const wait=(ms=70)=>new Promise(resolve=>setTimeout(resolve,ms));
const collect=(node:any):any[]=>[node,...(node.childNodes||[]).flatMap(collect)];
const nodeText=(node:any):string=>node.nodeValue??(node.childNodes||[]).map(nodeText).join('');
async function clickAction(io:any,label:string){
 const node=collect(io.ink.rootNode).find(n=>n.attributes?.clickMode==='action'&&nodeText(n).includes(label));
 expect(node).toBeDefined();const r=getElementRect(node)!;
 io.stdin.write(`\x1b[<0;${r.x+1};${r.y+1}M\x1b[<0;${r.x+1};${r.y+1}m`);await wait(180);
}
function terminal(element:React.ReactNode,width=90){
 const stdin:any=new PassThrough();Object.assign(stdin,{isTTY:true,setRawMode(){},ref(){},unref(){}});
 const stdout:any=new PassThrough();Object.assign(stdout,{isTTY:true,columns:width,rows:26});stdout.on("data",()=>{});
 const stderr:any=new PassThrough();stderr.on("data",()=>{});
 const root=renderSync(<InputProvider>{element}</InputProvider>,{stdin,stdout,stderr,patchConsole:false,exitOnCtrlC:false});
 const ink:any=instances.get(stdout);ink.setAltScreenActive(true);ink.onRender();
 return {stdin,ink,rerender(element:React.ReactNode){root.rerender(<InputProvider>{element}</InputProvider>);},frame(){const s=ink.frontFrame.screen;return Array.from({length:s.height},(_,y)=>Array.from({length:s.width},(_,x)=>cellAt(s,x,y)?.char||" ").join(""));},
  close(){root.unmount();root.cleanup();stdin.destroy();stdout.destroy();stderr.destroy();}};
}
test("detail window consumes its wheel, forwards boundary remainder and leaves original end readable",async()=>{
 const outer=createRef<ScrollBoxHandle>(),bounds=createRef<DOMElement>();let inner:ScrollBoxHandle|null=null,closed=0;
 const original=Array.from({length:16000},(_,index)=>"SOURCE "+index+" 原文 👩‍💻 e\u0301").join("\n")+"\nSOURCE ORIGINAL END";
 const points=Array.from(original),requests:any[]=[];
 const client:any={async callTool(_name:string,args:any){requests.push(args);
  const end=Math.min(points.length,args.offset+8192);
  return {detail:{representation:"trace_record_source_page",source_index:0,offset:args.offset,next_offset:end,
   total_chars:points.length,has_more:end<points.length,text:points.slice(args.offset,end).join(""),
   before:points.slice(Math.max(0,args.offset-64),args.offset).join(""),after:points.slice(end,end+64).join("")}};
 }};
 const read=traceBodyReader(client,"fixture","turn");
 function Capture(){const value=useContentViewport();useEffect(()=>{inner=value!.current;});return null;}
 function Fixture(){useScrollInput(outer);return <Box width={90} height={24} flexDirection="column">
  <ContentViewport scrollRef={outer}><ScrollBox ref={outer} flexDirection="column" flexGrow={1}>
   <Text>OUTER PREFIX</Text><Box ref={bounds} flexDirection="column" flexShrink={0}>
    <DetailViewport onClose={()=>closed++}>
     {bodyWidth=><><Capture/><TraceSourceText reference="immutable" index={0} totalChars={points.length} read={read.recordSource!} width={bodyWidth}/></>}
    </DetailViewport>
   </Box><Box height={50} flexShrink={0}><Text>OUTER FOLLOWING</Text></Box>
  </ScrollBox></ContentViewport></Box>;}
 const io=terminal(<Fixture/>);
 try{
  await wait(150);expect(inner).not.toBeNull();expect(inner!.getViewportHeight()).toBeLessThanOrEqual(8);
  expect(requests.length).toBeGreaterThan(0);expect(requests.length).toBeLessThan(8);
  const wheel=async(direction:number)=>{const r=getElementRect(bounds.current!)!;
   io.stdin.write("\x1b[<"+direction+";"+(Math.floor(r.x)+2)+";"+(Math.floor(r.y)+2)+"M");await wait();};
  await wheel(65);expect(inner!.getScrollTop()).toBeGreaterThan(0);expect(outer.current!.getScrollTop()).toBe(0);
  inner!.scrollToBottom();await wait(220);
  expect(io.frame().join("\n")).toContain("SOURCE ORIGINAL END");
  expect(requests.length).toBeLessThan(12);
  await wheel(65);expect(outer.current!.getScrollTop()).toBeGreaterThan(0);
  const r=getElementRect(bounds.current!)!;
  io.stdin.write("\x1b[<35;"+(Math.floor(r.x)+2)+";"+(Math.floor(r.y)+2)+"M");await wait(20);
  io.stdin.write("\x1b");await wait();expect(closed).toBe(1);
  outer.current!.scrollTo(0);inner!.scrollTo(0);await wait();
  const entry=getElementRect(bounds.current!)!;
  io.stdin.write("\x1b[<35;"+(Math.floor(entry.x)+2)+";"+(Math.floor(entry.y)+2)+"M");await wait(20);
  // Typing gives keyboard navigation back to the original conversation owner.
  io.stdin.write("a\x1b[6~");await wait();
  expect(inner!.getScrollTop()).toBe(0);expect(outer.current!.getScrollTop()).toBeGreaterThan(0);
  expect(collect(io.ink.rootNode).length).toBeLessThan(160);
 }finally{io.close();}
});

for(const width of [34,90])test(`framed details fit the physical parent and preserve the first and last line (${width})`,async()=>{
 const outer=createRef<ScrollBoxHandle>();
 const io=terminal(<Box width={width} height={10} flexDirection="column"><ContentViewport scrollRef={outer}>
  <ScrollBox ref={outer} flexGrow={1} flexDirection="column">
   <Text>TOOL HEADER</Text><Box paddingLeft={2} flexDirection="column" flexShrink={0}>
    <DetailViewport onClose={()=>{}} width={90}>{bodyWidth=><Box width={bodyWidth} flexDirection="column" flexShrink={0}>
     {Array.from({length:100},(_,index)=><Text key={index}>{index===99?'ORIGINAL LAST LINE':`ORIGINAL LINE ${index}`}</Text>)}
    </Box>}</DetailViewport>
   </Box><Text>FOLLOWING ANSWER</Text>
  </ScrollBox></ContentViewport></Box>,width);
 try{
  await wait();
  const frame=collect(io.ink.rootNode).find(node=>node.style?.borderStyle==='single');
  const rect=getElementRect(frame)!;
  expect(rect.x+rect.width).toBeLessThanOrEqual(width);
  expect(io.frame().join('\n')).toContain('ORIGINAL LINE 0');
  const corners=()=>io.frame().filter(line=>line.includes('┐')||line.includes('┘'));
  expect(corners()).toHaveLength(2);
  expect(corners().every(line=>line.trimEnd().endsWith('┐')||line.trimEnd().endsWith('┘'))).toBe(true);
  io.stdin.write(`\x1b[<35;${Math.floor(rect.x)+2};${Math.floor(rect.y)+2}M`);await wait(20);
  io.stdin.write('\x1b[F');await wait();
  expect(io.frame().join('\n')).toContain('ORIGINAL LAST LINE');
  expect(io.frame().join('\n')).toContain('FOLLOWING ANSWER');
  expect(outer.current!.getScrollTop()).toBe(0);
 }finally{io.close();}
});

test('a large unframed context source list stays virtual without eager source reads',async()=>{
 const outer=createRef<ScrollBoxHandle>(),calls:any[]=[];
 const client:any={async callTool(_name:string,args:any){calls.push(args);
  expect(args.view).toBe('input_sources');
  return {detail:{representation:'canonical_provider_input_sources',input_provenance:{schema_version:1,
   sources:Array.from({length:200},(_,index)=>({message_index:index,sections:[{name:'source-'+index,version:'saved'}]}))}}};
 }};
 const read=traceBodyReader(client,'fixture','turn');
 const io=terminal(<Box width={90} height={24}><ContentViewport scrollRef={outer}><ScrollBox ref={outer} flexGrow={1} flexDirection='column'>
  <RequestContextRow request={{event:'provider_request_started',detail_ref:'saved'}} readBody={read} width={86}/>
 </ScrollBox></ContentViewport></Box>);
 try{
  await wait();expect(calls).toHaveLength(0);
  const action=collect(io.ink.rootNode).find(node=>node.attributes?.clickMode==='action'&&node._eventHandlers?.onClick);
  const rect=getElementRect(action)!;
  io.stdin.write(`\x1b[<0;${rect.x+1};${rect.y+1}M\x1b[<0;${rect.x+1};${rect.y+1}m`);await wait(300);
  expect(calls).toHaveLength(1);
  const windows=collect(io.ink.rootNode).filter(node=>node.style?.borderStyle==='single');
  expect(windows).toHaveLength(0);
  outer.current!.scrollToBottom();await wait(180);
  expect(io.frame().join('\n')).toContain('source-199');expect(calls).toHaveLength(1);
  expect(outer.current!.getScrollTop()).toBeGreaterThan(0);expect(collect(io.ink.rootNode).length).toBeLessThan(500);
 }finally{io.close();}
});
test("read_file keeps parameters on its header and opens directly to bounded original file pages",async()=>{
 const outer=createRef<ScrollBoxHandle>(),requests:any[]=[];
 const content="const exact_original = '文件正文';\n".repeat(5000)+"EXACT FILE END";
 const points=Array.from(content);
 const client:any={async callTool(_name:string,args:any){requests.push(args);
  if(args.view==="record_sources")return {detail:{representation:"trace_record_sources",sources:[{source_index:0,kind:"content",total_chars:points.length}]}};
  expect(args.view).toBe("record_source");
  const end=Math.min(points.length,args.offset+8192);
  return {detail:{representation:"trace_record_source_page",source_index:0,offset:args.offset,next_offset:end,total_chars:points.length,
   has_more:end<points.length,text:points.slice(args.offset,end).join(""),before:points.slice(Math.max(0,args.offset-64),args.offset).join(""),after:points.slice(end,end+64).join("")}};
 }};
 const read=traceBodyReader(client,"fixture","turn");
 const io=terminal(<Box width={90} height={24} flexDirection="column"><ContentViewport scrollRef={outer}>
  <ScrollBox ref={outer} flexDirection="column" flexGrow={1}>
   <ToolCallDisplay tool={{tool_name:"read_file",tool_call_id:"read",target:"",arguments:{path:"src/file.ts",offset:10,limit:20},
    detail_ref:"saved",allowed:true,status_label:"OK",duration_ms:11,timestamp:""}} width={86} readBody={read}/>
  </ScrollBox></ContentViewport></Box>);
 try{
  await wait();expect(requests).toHaveLength(0);
  const row=io.frame().findIndex(line=>line.includes("read_file")),col=io.frame()[row]!.indexOf("▸");
  expect(io.frame()[row]).toContain("src/file.ts:11–30");
  io.stdin.write("\x1b[<0;"+(col+1)+";"+(row+1)+"M\x1b[<0;"+(col+1)+";"+(row+1)+"m");await wait(180);
  const screen=io.frame().join("\n");expect(screen).toContain("exact_original");
  expect(screen).not.toContain("Arguments");expect(screen).not.toContain('"content"');
  expect(requests.every(request=>request.view==="record_sources"||request.view==="record_source")).toBe(true);
  expect(requests.length).toBeLessThan(8);
 }finally{io.close();}
});

test('context sources have independent framed windows and closing one keeps the list and sibling open',async()=>{
 const outer=createRef<ScrollBoxHandle>(),calls:any[]=[];
 const content=Array.from({length:1000},(_,i)=>`original line ${i}`).join('\n')+'\nSOURCE END';
 const points=Array.from(content);
 const client:any={async callTool(_name:string,args:any){calls.push(args);
  if(args.view==='input_sources')return {detail:{representation:'canonical_provider_input_sources',input_provenance:{schema_version:1,
   sources:[0,1].map(i=>({message_index:i,sections:[{name:'part-'+i,version:'v1'}]}))}}};
  const end=Math.min(points.length,args.offset+8192);return {detail:{representation:'canonical_provider_input_source_page',
   source_index:args.source_index,offset:args.offset,next_offset:end,total_chars:points.length,has_more:end<points.length,text:points.slice(args.offset,end).join('')}};
 }};
 const read=traceBodyReader(client,'fixture','turn');
 const io=terminal(<Box width={90} height={50}><ContentViewport scrollRef={outer}><ScrollBox ref={outer} flexDirection='column' flexGrow={1}>
  <RequestContextRow request={{event:'provider_request_started',detail_ref:'saved'}} readBody={read} width={86}/>
 </ScrollBox></ContentViewport></Box>);
 try{
  await wait();await clickAction(io,'Injected context');expect(calls).toHaveLength(1);
  await clickAction(io,'part-0');await clickAction(io,'part-1');
  const frames=collect(io.ink.rootNode).filter(n=>n.style?.borderStyle==='single');expect(frames).toHaveLength(2);
  const windows=frames.map(frame=>collect(frame).find(n=>n.style?.overflowY==='scroll'));
  for(const window of windows){expect(window.yogaNode.getComputedHeight()).toBeLessThanOrEqual(16);expect(window.yogaNode.getComputedHeight()).toBeGreaterThan(8);}
  const area=getElementRect(frames[0])!;
  io.stdin.write(`\x1b[<35;${area.x+2};${area.y+2}M`);await wait(20);io.stdin.write('\x1b[F');await wait(200);
  expect(io.frame().join('\n')).toContain('SOURCE END');expect(outer.current!.getScrollTop()).toBe(0);
  io.stdin.write('\x1b');await wait(200);
  expect(collect(io.ink.rootNode).filter(n=>n.style?.borderStyle==='single')).toHaveLength(1);
  expect(io.frame().join('\n')).toContain('part-0');expect(io.frame().join('\n')).toContain('part-1');
 }finally{io.close();}
});

test('stage report keeps prose visible with a leading arrow, frameless 16-row detail and bottom L',async()=>{
 const outer=createRef<ScrollBoxHandle>();
 const original=Array.from({length:3000},(_,i)=>`DETAIL ${i} 原文`).join('\n')+'\nTHOUGHT END';
 const draw=(verbose=true,detail:string|undefined=original)=><Box width={90} height={26}><ContentViewport scrollRef={outer}>
  <ScrollBox ref={outer} flexGrow={1} flexDirection='column'>
   <StageReport verbose={verbose} identity='stage1' detail={detail} width={86}><Text>One public report.</Text></StageReport>
  </ScrollBox></ContentViewport></Box>;
 const io=terminal(draw());
 try{
  await wait(180);const screen=io.frame().join('\n');
  expect(screen).toContain('▾ One public report.');expect(screen).toContain('└─');expect(screen).not.toContain('├');
  expect(screen).not.toContain('┌');expect(screen).not.toContain('Thinking');
  const windows=collect(io.ink.rootNode).filter(n=>n.style?.overflowY==='scroll');
  const inner=windows.find(n=>n!==windows[0])!;expect(inner.yogaNode.getComputedHeight()).toBe(16);
  const area=getElementRect(inner)!;io.stdin.write(`\x1b[<35;${area.x+1};${area.y+1}M`);await wait(20);
  io.stdin.write('\x1b[F');await wait(200);expect(io.frame().join('\n')).toContain('THOUGHT END');
  await clickAction(io,'▾');expect(io.frame().join('\n')).toContain('One public report.');expect(io.frame().join('\n')).not.toContain('DETAIL');
  await clickAction(io,'▸');expect(io.frame().join('\n')).toContain('DETAIL');
  io.rerender(draw(false));await wait();expect(io.frame().join('\n')).not.toContain('DETAIL');expect(io.frame().join('\n')).not.toContain('▾');
  io.rerender(draw());await wait();expect(io.frame().join('\n')).toContain('DETAIL');
  io.rerender(draw(true,''));await wait();expect(io.frame().join('\n')).toContain('One public report.');expect(io.frame().join('\n')).not.toContain('DETAIL');
 }finally{io.close();}
});

test('live phase detail attaches only to the matching public report, without a second Thinking row',async()=>{
 const outer=createRef<ScrollBoxHandle>();
 const stage={id:'p:1',process_id:'p',reasoning:'CURRENT DETAIL'};
 const activity:any[]=[{kind:'progress',text:'Other process report',visibility:'public',process_id:'q',stage_id:'p:1'},
  {kind:'progress',text:'Matching public report',visibility:'public',process_id:'p',stage_id:'p:1'}];
 const draw=(active=true,verbose=true)=><Box width={90} height={26}><ContentViewport scrollRef={outer}><ScrollBox ref={outer} flexDirection='column' flexGrow={1}>
  <TurnTimeline activity={activity} tools={[]} notices={[]} verbose={verbose} width={86} active={active} stage={stage} thinking/>
 </ScrollBox></ContentViewport></Box>;
 const io=terminal(draw());
 try{await wait();const screen=io.frame().join('\n');expect(screen).toContain('▾ Matching public report');expect(screen).not.toContain('▾ Other process report');
  expect(screen).toContain('CURRENT DETAIL');expect(screen).not.toContain('Thinking');
  io.rerender(draw(true,false));await wait();expect(io.frame().join('\n')).not.toContain('CURRENT DETAIL');expect(io.frame().join('\n')).toContain('Matching public report');
  io.rerender(draw(false));await wait();expect(io.frame().join('\n')).not.toContain('CURRENT DETAIL');
 }finally{io.close();}
});

test('a long public report is not height capped and a short thought does not fill sixteen rows',async()=>{
 const outer=createRef<ScrollBoxHandle>();
 const report=Array.from({length:45},(_,i)=>`PUBLIC ${i}`).join('\n')+'\nPUBLIC REPORT END';
 const io=terminal(<Box width={34} height={14}><ContentViewport scrollRef={outer}><ScrollBox ref={outer} flexDirection='column' flexGrow={1}>
  <StageReport identity='long-report' verbose detail='SHORT DETAIL' width={32}><Text>{report}</Text></StageReport>
 </ScrollBox></ContentViewport></Box>,34);
 try{await wait();const textNode=collect(io.ink.rootNode).find(node=>node.nodeName==='ink-text'&&nodeText(node)===report);
  expect(textNode).toBeDefined();expect(textNode.yogaNode.getComputedHeight()).toBeGreaterThan(16);
  outer.current!.scrollToBottom();await wait(180);const screen=io.frame().join('\n');
  expect(screen).toContain('PUBLIC REPORT END');expect(screen).toContain('SHORT DETAIL');expect(screen).toContain('└─');
  const windows=collect(io.ink.rootNode).filter(node=>node.style?.overflowY==='scroll');
  expect(windows.at(-1).yogaNode.getComputedHeight()).toBeLessThan(16);
 }finally{io.close();}
});
