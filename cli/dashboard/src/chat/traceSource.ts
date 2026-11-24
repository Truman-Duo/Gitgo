import type {BackendClient} from "../backend/client.js";
import {runtimeTrace} from "../backend/tools.js";
import type {TraceRecord} from "../traceView.js";
import {cacheTrace, cachedTrace} from "./traceCache.js";

/** Locations in the original trace, never another stored conversation. */
export type TraceSnapshot = {
  schema: 1; trace_id: string; process_id: string;
  first_seq: number; last_seq: number; event_count: number;
};
export type TraceBodyRef = {
  schema: 1; snapshot: TraceSnapshot; first_seq: number; last_seq: number;
  event: string; process_id: string; event_count: number; text_chars: number;
};
export type TraceSummary = {
  trace_id: string; event_count: number; first_seq: number; last_seq: number;
  first_at: string; last_at: string; event_counts: Record<string, number>; complete: boolean;
};
export type TraceDetailReader = (ref: string, signal: AbortSignal) => Promise<unknown>;
export type InputSourcePage = {source_index:number;offset:number;next_offset:number;total_chars:number;has_more:boolean;text:string;before?:string;after?:string};
export const INPUT_SOURCE_PAGE_CHARS=8192;
export type TraceInputSourceReader=(ref:string,index:number,offset:number,signal:AbortSignal)=>Promise<InputSourcePage>;
export type TraceBodyReader = ((refs: readonly TraceBodyRef[], signal: AbortSignal) => Promise<TraceRecord[]>) &
  {detail?: TraceDetailReader;inputSources?:TraceDetailReader;inputSource?:TraceInputSourceReader;
   recordSources?:TraceDetailReader;recordSource?:TraceInputSourceReader};
type InputRead={signals:Set<AbortSignal>;load:()=>Promise<unknown>;resolve:(value:unknown)=>void;reject:(error:unknown)=>void};
const inputReads=new WeakMap<object,{active:number;queue:InputRead[];pending:Map<string,Promise<unknown>>;requests:Map<string,InputRead>}>();

/** Observation reads share a two-request budget, separate from task requests.
 * Reopening the same immutable page joins its read; abandoned queued reads
 * never enter the Host. Each consumer still owns its cancellation lifetime. */
async function limitedInputRead(client:object,key:string,signal:AbortSignal,load:()=>Promise<unknown>):Promise<any>{
  let state=inputReads.get(client);
  if(!state){state={active:0,queue:[],pending:new Map(),requests:new Map()};inputReads.set(client,state);}
  const shared=state.pending.get(key);
  if(shared){state.requests.get(key)!.signals.add(signal);return shared;}
  let resolve!:(value:unknown)=>void,reject!:(error:unknown)=>void;
  const pending=new Promise((yes,no)=>{resolve=yes;reject=no;});
  const request={signals:new Set([signal]),load,resolve,reject};
  state.pending.set(key,pending);state.requests.set(key,request);state.queue.push(request);
  const owned=state;
  const cleanup=(item:InputRead)=>{
    for(const [identity,value] of owned.requests)if(value===item){owned.requests.delete(identity);owned.pending.delete(identity);break;}
  };
  const drain=()=>{
    while(owned.active<2 && owned.queue.length){
      const item=owned.queue.shift()!;
      if([...item.signals].every(value=>value.aborted)){cleanup(item);item.reject(new DOMException("Trace read cancelled","AbortError"));continue;}
      owned.active++;
      void Promise.resolve().then(item.load).then(item.resolve,item.reject).finally(()=>{owned.active--;cleanup(item);drain();});
    }
  };
  drain();
  return pending;
}
export function traceRefs(event: TraceRecord): TraceBodyRef[] {
  return event.body_ref ? [event.body_ref as TraceBodyRef] : [];
}
function cancelled(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException("Trace read cancelled", "AbortError");
}
function sameSnapshot(left: TraceSnapshot, right: TraceSnapshot) {
  return Boolean(right) && (["schema", "trace_id", "process_id", "first_seq", "last_seq", "event_count"] as const)
    .every(key => left[key] === right[key]);
}

/** Bounded transport pages; the existing expand/scroll operation reads all of
 * its requested body automatically. Leaving it prevents subsequent reads. */
export function traceBodyReader(client: BackendClient, project: string, traceId: string): TraceBodyReader {
  const read: TraceBodyReader = async (references, signal) => {
    const result: TraceRecord[] = [];
    for (const reference of references) {
      cancelled(signal);
      if (reference.snapshot.trace_id !== traceId) throw new Error("TRACE_INDEX_SCOPE_MISMATCH");
      const key = JSON.stringify(["body", project, reference]);
      const saved = cachedTrace(client, key);
      if (saved) { for (const event of saved.events) result.push(event); continue; }
      const events: TraceRecord[] = [];
      let cursor = reference.first_seq - 1;
      for (;;) {
        cancelled(signal);
        const page: any = await runtimeTrace(client, project, {
          action: "block", trace_id: traceId, body_ref: reference, after_seq: cursor, limit: 500,
        });
        cancelled(signal);
        if (!sameSnapshot(reference.snapshot, page.snapshot)) throw new Error("TRACE_INDEX_SNAPSHOT_MISMATCH");
        const next = Number(page.next_seq);
        const batch = page.events as TraceRecord[];
        if (!Array.isArray(batch) || !Number.isInteger(next) || next < cursor
            || page.has_more && next === cursor) throw new Error("TRACE_BLOCK_CURSOR_INVALID");
        for (const event of batch) {
          if (Number(event.seq) <= cursor || Number(event.seq) > reference.last_seq
              || event.event !== reference.event || String(event.process_id || "") !== reference.process_id) {
            throw new Error("TRACE_BLOCK_SCOPE_MISMATCH");
          }
          cursor = Number(event.seq);
          events.push(event);
        }
        if (next !== cursor) throw new Error("TRACE_BLOCK_CURSOR_INVALID");
        if (!page.has_more) break;
      }
      if (cursor !== reference.last_seq || events.length !== reference.event_count) {
        throw new Error("TRACE_BLOCK_INCOMPLETE");
      }
      cacheTrace(client, key, {events, truncated: false});
      for (const event of events) result.push(event);
    }
    return result;
  };
  read.detail = async (reference,signal) => {
    cancelled(signal);
    const key=JSON.stringify(["detail",project,traceId,reference]);
    const saved=cachedTrace(client,key);
    if(saved && "detail" in saved)return saved.detail;
    const result:any=await limitedInputRead(client,key,signal,()=>runtimeTrace(client,project,{action:"detail",ref:reference}));
    cancelled(signal);
    if(!result || typeof result !== "object" || !("detail" in result))throw new Error("TRACE_DETAIL_MISSING");
    cacheTrace(client,key,{events:[],truncated:false,detail:result.detail});
    return result.detail;
  };
  const sources=(view:"input_sources"|"record_sources",representation:string):TraceDetailReader=>async(reference,signal)=>{
    cancelled(signal);
    const key=JSON.stringify([view,project,traceId,reference]);
    const saved=cachedTrace(client,key);
    if(saved && "detail" in saved)return saved.detail;
    const result:any=await limitedInputRead(client,key,signal,()=>runtimeTrace(client,project,{action:"detail",ref:reference,view}));
    cancelled(signal);
    if(result?.detail?.representation!==representation)throw new Error("TRACE_INPUT_VIEW_UNAVAILABLE");
    cacheTrace(client,key,{events:[],truncated:false,detail:result.detail});
    return result.detail;
  };
  const pageReader=(view:"input_source"|"record_source",representation:string):TraceInputSourceReader=>async(reference,index,offset,signal)=>{
    cancelled(signal);
    const key=JSON.stringify([view,project,traceId,reference,index,offset]);
    const saved=cachedTrace(client,key);
    if(saved && "detail" in saved)return saved.detail as InputSourcePage;
    const result:any=await limitedInputRead(client,key,signal,()=>runtimeTrace(client,project,{action:"detail",ref:reference,view,source_index:index,offset}));
    cancelled(signal);
    const page=result?.detail;
    if(page?.representation!==representation || page.source_index!==index || page.offset!==offset
        || !Number.isSafeInteger(page.next_offset) || page.next_offset<offset || page.next_offset>offset+INPUT_SOURCE_PAGE_CHARS
        || !Number.isSafeInteger(page.total_chars) || page.total_chars<page.next_offset || typeof page.text!=="string"
        || [...page.text].length!==page.next_offset-offset || page.has_more!==(page.next_offset<page.total_chars)
        || page.before!==undefined && (typeof page.before!=="string" || [...page.before].length>64)
        || page.after!==undefined && (typeof page.after!=="string" || [...page.after].length>64)
        || page.has_more && page.next_offset!==offset+INPUT_SOURCE_PAGE_CHARS)throw new Error("TRACE_INPUT_PAGE_INVALID");
    cacheTrace(client,key,{events:[],truncated:false,detail:page});
    return page;
  };
  read.inputSources=sources("input_sources","canonical_provider_input_sources");
  read.inputSource=pageReader("input_source","canonical_provider_input_source_page");
  read.recordSources=sources("record_sources","trace_record_sources");
  read.recordSource=pageReader("record_source","trace_record_source_page");
  return read;
}

/** Summary and index go through the existing canonical read boundary.
 * Each published prefix is usable; body requests remain independently lazy. */
export async function readTraceIndex(client: BackendClient, project: string, traceId: string,
  processId: string, signal: AbortSignal,
  onSummary: (summary: TraceSummary) => void,
  onPage: (events: TraceRecord[], complete: boolean) => void) {
  const summary = await runtimeTrace(client, project, {
    action: "summary", trace_id: traceId, process_id: processId, include_recent: false,
  }) as TraceSummary;
  cancelled(signal); onSummary(summary);
  let cursor = -1, snapshot: TraceSnapshot | undefined;
  const events: TraceRecord[] = [];
  for (;;) {
    cancelled(signal);
    const page: any = await runtimeTrace(client, project, {
      action: "index", trace_id: traceId, process_id: processId, after_seq: cursor, limit: 200,
      ...(snapshot ? {snapshot} : {}),
    });
    cancelled(signal);
    if (snapshot && !sameSnapshot(snapshot, page.snapshot)) throw new Error("TRACE_INDEX_SNAPSHOT_MISMATCH");
    snapshot = page.snapshot;
    if (!snapshot || snapshot.trace_id !== traceId || snapshot.process_id !== processId) {
      throw new Error("TRACE_INDEX_SCOPE_MISMATCH");
    }
    const next = Number(page.next_seq);
    if (!Array.isArray(page.events) || !Number.isInteger(next) || next < cursor
        || page.has_more && next === cursor) throw new Error("TRACE_INDEX_CURSOR_INVALID");
    for (const event of page.events as TraceRecord[]) {
      const reference = traceRefs(event)[0];
      if (!reference || reference.schema !== 1 || reference.first_seq !== Number(event.seq)
          || !sameSnapshot(reference.snapshot, snapshot)
          || reference.first_seq <= cursor || reference.last_seq < reference.first_seq) {
        throw new Error("TRACE_INDEX_CURSOR_INVALID");
      }
      cursor = reference.last_seq;
      events.push(event);
    }
    if (next !== cursor) throw new Error("TRACE_INDEX_CURSOR_INVALID");
    if (!page.has_more && snapshot.event_count > 0 && (cursor !== snapshot.last_seq
        || events.reduce((count, event) => count + traceRefs(event)[0]!.event_count, 0) !== snapshot.event_count)) {
      throw new Error("TRACE_INDEX_INCOMPLETE");
    }
    onPage(events.slice(), !page.has_more);
    if (!page.has_more) return;
  }
}
