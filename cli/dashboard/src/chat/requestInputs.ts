import type {RequestInputEvent,StreamActivity} from "../types.js";

export const inputRecord=(value:unknown):Record<string,any> =>
  value && typeof value==="object" && !Array.isArray(value) ? value as Record<string,any> : {};

/** Source metadata and content come from the saved dispatch snapshot only.
 * Missing snapshots remain unknown; current files/config never fill history. */
export function recordedInputSources(detail:unknown) {
  const saved=inputRecord(detail), provenance=inputRecord(saved.input_provenance);
  const exact=["canonical_provider_input","canonical_provider_input_sources"].includes(saved.representation) && provenance.schema_version===1;
  const messages=Array.isArray(saved.messages) ? saved.messages : [];
  const sources=exact && Array.isArray(provenance.sources)
    ? provenance.sources.flatMap((source:unknown,index:number)=>source && typeof source==="object" && !Array.isArray(source) ? [{
        id:`source-${index}`,source:source as Record<string,any>,source_index:index,
        content:Number.isInteger((source as any).message_index) ? inputRecord(messages[(source as any).message_index]).content : undefined,
      }] : []) : [];
  return {exact,provenance,sources};
}

/** Stable request identity is shared by live and original-trace projections. */
export function contextActivity(request:RequestInputEvent,ordinal:number,actor?:string) {
  const process_id=String(request.process_id || "");
  return {kind:"context" as const,request,process_id,actor_label:actor,
    record_id:`context:${request.trace_id || request.task_id || ""}:${process_id}:${request.seq ?? request.detail_ref ?? ordinal}`};
}

/** A prepared request may be followed by its exact dispatch-parameter record.
 * Replace that preparation in place rather than drawing two context cards. */
export function withRequestContext(activity:StreamActivity[],request:RequestInputEvent,actor?:string):StreamActivity[] {
  const item=contextActivity(request,activity.length,actor);
  const duplicate=activity.findIndex(previous=>previous.kind==="context" && previous.record_id===item.record_id);
  if(duplicate>=0)return activity;
  if(request.event==="provider_request_parameters"){
    const index=activity.findLastIndex(previous=>previous.kind==="context" && previous.process_id===item.process_id);
    const previous=activity[index];
    if(previous?.kind==="context" && previous.request.event==="provider_request_started"
        && !previous.request.request_parameters
        && ["trace_id","task_id","model","route_key","provider_id"].every(key=>!previous.request[key] || !request[key] || previous.request[key]===request[key])){
      const result=activity.slice();
      result[index]={...previous,request:{...previous.request,...request}};
      return result;
    }
  }
  return [...activity,item];
}
