import type {BackendClient} from "../backend/client.js";
import type {FrameEvent} from "../../vendor/ink/src/core/frame.js";

// Observational only: no raw input text, React state or renderer scheduling.
export type InputObservation = {atMs:number; dispatchMs:number; kind:number; length:number; handled:number};
type Stats = {count:number; total:number; max:number; buckets:number[]};
const stats = ():Stats => ({count:0,total:0,max:0,buckets:[0,0,0,0,0,0]});
function add(target:Stats,value:number) {
  if (!Number.isFinite(value) || value < 0) return;
  target.count++; target.total+=value; target.max=Math.max(target.max,value);
  const index=[8,16,33,100,250].findIndex(n=>value<=n);
  target.buckets[index<0?5:index]++;
}
function batch() {
  return {frames:{duration:stats(),renderer:stats(),diff:stats(),optimize:stats(),write:stats(),yoga:stats(),commit:stats(),
    patches:0,flickers:0,yogaVisited:0,yogaMeasured:0,yogaCacheHits:0,yogaLiveMax:0},
    inputs:{count:0,characters:0,handled:0,kinds:[0,0,0,0,0,0],dispatch:stats()},
    input_to_frame:stats(),event_loop:stats(),dropped:{pending_inputs:0,batches:0}};
}

export function createFrontendObserver(client:BackendClient, options:{enabled?:boolean; now?:()=>number; intervalMs?:number;
  viewport?:()=>{columns:number;rows:number}}={}) {
  if (!options.enabled) return undefined;
  const now=options.now??(()=>performance.now()), intervalMs=options.intervalMs??1000;
  let current=batch(), pending:number[]=[], started=now(), sequence=0, closed=false;
  let inflight:Promise<void>|undefined;
  const onInput=(event:InputObservation) => {
    current.inputs.count++; current.inputs.characters+=event.length; current.inputs.handled+=event.handled;
    current.inputs.kinds[Math.max(0,Math.min(5,event.kind))]++; add(current.inputs.dispatch,event.dispatchMs);
    if(pending.length>=64){pending.shift();current.dropped.pending_inputs++;}
    pending.push(event.atMs);
  };
  const onFrame=(event:FrameEvent) => {
    add(current.frames.duration,event.durationMs); current.frames.flickers+=event.flickers.length;
    for(const key of ["renderer","diff","optimize","write","yoga","commit"] as const) {
      if(event.phases) add(current.frames[key],event.phases[key]);
    }
    current.frames.patches+=event.phases?.patches??0;
    current.frames.yogaVisited+=event.phases?.yogaVisited??0;
    current.frames.yogaMeasured+=event.phases?.yogaMeasured??0;
    current.frames.yogaCacheHits+=event.phases?.yogaCacheHits??0;
    current.frames.yogaLiveMax=Math.max(current.frames.yogaLiveMax,event.phases?.yogaLive??0);
    const at=now(); for(const input of pending) add(current.input_to_frame,Math.max(0,at-input)); pending=[];
  };
  const flush=async()=>{
    if(inflight) return inflight;
    const at=now(), payload={...current,sequence:++sequence,window_ms:Math.max(0,at-started),
      viewport:options.viewport?.()??{columns:0,rows:0}};
    current=batch(); started=at;
    inflight=client.callTool("observer.frontend",payload,5).then(result=>{
      if(result?.captured!==true)current.dropped.batches++;
    },()=>{current.dropped.batches++;}).finally(()=>{inflight=undefined;});
    return inflight;
  };
  let expected=now()+100;
  const loopTimer=setInterval(()=>{const at=now();add(current.event_loop,Math.max(0,at-expected));expected=at+100;},100);
  const timer=setInterval(()=>{void flush();},intervalMs);
  loopTimer.unref?.();timer.unref?.();
  return {onInput,onFrame,flush,async close(){
    if(closed)return; closed=true;clearInterval(timer);clearInterval(loopTimer);
    if(pending.length){current.dropped.pending_inputs+=pending.length;pending=[];}
    if(inflight)await inflight;
    await flush();
  }};
}
