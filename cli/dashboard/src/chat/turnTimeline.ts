import type { ChatMessage, ProviderUsageSummary, RuntimeNotice, StreamActivity, ToolCallCard } from "../types.js";
import type { StreamEvent } from "../daemon/streamEvents.js";
import { runtimeNoticeFromEvent } from "../daemon/runtimeNotices.js";
import {childActorLabel,isTemporarySubagent,promotionSource} from "../daemon/agentLabels.js";
import type { TraceRecord } from "../traceView.js";
import {updatePublicProgress,isRuntimePreparation} from "./publicProgress.js";
import {withRequestContext} from "./requestInputs.js";
import {traceRefs} from "./traceSource.js";
import {progressIdentity} from "./publicProgress.js";

export type TurnTimelineModel = {
  activity: StreamActivity[];
  tools: ToolCallCard[];
  notices: RuntimeNotice[];
  providerUsage?: ProviderUsageSummary;
};

function callKey(event: TraceRecord): string {
  return `${String(event.process_id || "host")}:${String(event.tool_call_id || event.tool_name || "")}`;
}

/**
 * Convert the authoritative SQLite trace into the same ordered model used by
 * the live main/subprocess renderer. Text followed by another provider/tool step
 * is progress; the final response draft is omitted because TaskOutcome is the
 * one public final answer rendered after the completed capsule.
 */
export function timelineFromTrace(
  events: TraceRecord[],
  rootProcessId = "",
  terminal?: {status?: string; timestamp?: string},
): TurnTimelineModel {
  let activity: StreamActivity[] = [];
  const tools: ToolCallCard[] = [];
  const notices: RuntimeNotice[] = [];
  const toolIndex = new Map<string, number>();
  const publicProgressIndex = new Map<string, number>();
  const pendingText = new Map<string, number[]>();
  const visibilityByProcess = new Map<string, "public" | "verbose">();
  const knownSubagents = new Set<string>();
  let providerUsage: ProviderUsageSummary | undefined;
  const flushProgress = (processId: string) => {
    for(const index of pendingText.get(processId) || []) {
      const item=activity[index];
      if(item?.kind === "draft") activity[index]={...item,kind:"progress"};
    }
    pendingText.delete(processId);
  };

  for (const event of events) {
    if(isRuntimePreparation(event))continue;
    const trace_refs = traceRefs(event);
    const processId = String(event.process_id || "host");
    if (event.process_id && isTemporarySubagent(event)) knownSubagents.add(event.process_id);
    const actor_label = childActorLabel(event, rootProcessId);
    if (event.event === "provider_usage") {
      const usage = event.usage && typeof event.usage === "object"
        ? event.usage as Record<string, unknown> : {};
      providerUsage = {
        input_tokens: (providerUsage?.input_tokens || 0) + Number(usage.input_tokens || 0),
        output_tokens: (providerUsage?.output_tokens || 0) + Number(usage.output_tokens || 0),
      };
      continue;
    }
    if (event.event === "text_delta") {
      const visibility = event.visibility === "public" ? "public" : event.visibility === "verbose" ? "verbose" : visibilityByProcess.get(processId) || "verbose";
      const previous = activity[pendingText.get(processId)?.at(-1) ?? -1];
      if(previous?.kind === "draft" && previous.visibility !== visibility) flushProgress(processId);
      const indices=pendingText.get(processId) || [];
      const lastIndex=activity.findLastIndex(item=>item.kind!=="context"),last=activity[lastIndex],delta=String(event.delta || "");
      if(last?.kind === "draft" && last.process_id === processId
          && last.visibility === visibility) activity[lastIndex]={...last,text:last.text+delta,
            trace_refs:[...(last.trace_refs || []),...trace_refs]};
      else if(delta || trace_refs.length) {indices.push(activity.length);activity.push({kind:"draft",text:delta,visibility,process_id:processId,actor_label,trace_refs});}
      visibilityByProcess.set(processId,visibility);
      pendingText.set(processId,indices);continue;
    }
    if (event.event === "reasoning_delta") {
      const delta = String(event.delta || "");
      const last = activity[activity.length - 1];
      if (last?.kind === "reasoning" && last.process_id === processId) {
        activity[activity.length - 1] = {...last, text: last.text + delta, trace_refs:[...(last.trace_refs || []),...trace_refs]};
      } else if (delta || trace_refs.length) {
        activity.push({kind: "reasoning", text: delta, process_id: processId, actor_label, trace_refs});
      }
      continue;
    }
    if (event.event === "progress_summary" || event.event === "progress_delta") {
      activity = updatePublicProgress(activity, {...event, process_id: processId, actor_label, trace_refs,
        update_id: typeof event.update_id === "string" ? event.update_id : undefined}, publicProgressIndex, true);
      continue;
    }
    if ((event.event === "provider_request_started" && event.phase !== "alignment" && event.answer_continuation !== true)
      || event.event === "toolcall_start" || event.event === "toolcall_done") {
      flushProgress(processId);
    }
    if(event.event === "provider_request_started" && event.phase !== "alignment") {
      visibilityByProcess.set(processId,event.task_kind === "answer" ? "public":"verbose");
      activity=withRequestContext(activity,event,actor_label);
    }
    if(event.event==="provider_request_parameters" && event.purpose!=="alignment"){
      activity=withRequestContext(activity,event,actor_label);
    }
    if (event.event === "agent_terminal" || event.event === "agent_complete") {
      // The terminal provider prose is the TaskOutcome rendered below the
      // capsule. Keeping it here would duplicate the final answer.
      pendingText.delete(processId);
    }

    if (event.event === "toolcall_start" || event.event === "toolcall_done") {
      const existing=toolIndex.get(callKey(event));
      if(existing!==undefined){
        tools[existing]={...tools[existing],argument_preview:event.argument_preview as Record<string,unknown>|undefined,
          detail_ref:typeof event.detail_ref==="string"?event.detail_ref:tools[existing].detail_ref};
        continue;
      }
      const index = tools.length;
      const key = callKey(event);
      toolIndex.set(key, index);
      tools.push({
        tool_name: String(event.tool_name || "tool"),
        tool_call_id: String(event.tool_call_id || ""),
        target: String(event.target || ""), allowed: true,
        status_label: "running", duration_ms: 0,
        timestamp: String(event.time || ""), is_running: true, state: "running",
        actor_label, process_id: String(event.process_id || ""),
        detail_ref: String(event.detail_ref || "") || undefined,
        trace_refs,
        arguments:event.arguments,
        argument_preview:event.argument_preview as Record<string,unknown>|undefined,
      });
      activity.push({kind: "tool", toolIndex: index, detail_ref: String(event.detail_ref || "") || undefined});
      continue;
    }
    if (event.event === "tool_context_prepared") {
      const index=toolIndex.get(callKey(event));
      if(index!==undefined)tools[index]={...tools[index],context_only:true,is_running:false,state:"pending"};
      continue;
    }
    if (event.event === "tool_result") {
      const key = callKey(event);
      let index = toolIndex.get(key);
      if (index === undefined) {
        index = tools.length;
        toolIndex.set(key, index);
        tools.push({
          tool_name: String(event.tool_name || "tool"),
          tool_call_id: String(event.tool_call_id || ""), target: "",
          allowed: true, status_label: "", duration_ms: 0,
          timestamp: String(event.time || ""), actor_label,
          process_id: String(event.process_id || ""),
        });
        activity.push({kind: "tool", toolIndex: index, detail_ref: String(event.detail_ref || "") || undefined});
      }
      const failed = Boolean(event.is_error);
      tools[index] = {
        ...tools[index], allowed: !failed, is_running: false,
        state: failed ? "error" : "completed",
        status_label: failed ? "ERROR" : "OK",
        duration_ms: Number(event.duration_ms || 0),
        result_text: String(event.result_preview || event.error || ""),
        compact_summary: String(event.compact_summary || "") || undefined,
        blocked_reason: failed ? String(event.error || "tool failed") : undefined,
        diff: String(event.diff || "") || undefined,
        detail_ref: String(event.detail_ref || tools[index].detail_ref || "") || undefined,
        trace_refs: [...(tools[index].trace_refs || []), ...trace_refs],
      };
      continue;
    }
    if (event.event === "composite_step_result") {
      const failed = Boolean(event.is_error);
      const index = tools.length;
      tools.push({
        tool_name: String(event.tool_name || "tool"),
        tool_call_id: `${String(event.execution_id || "")}:${String(event.step_id || "")}`,
        composite_tool: String(event.composite_tool || ""), execution_id: String(event.execution_id || ""), step_id: String(event.step_id || ""),
        target: `${String(event.composite_tool || "composite")} / ${String(event.step_id || "step")}`,
        allowed: !failed, status_label: failed ? "ERROR" : "OK",
        duration_ms: Number(event.duration_ms || 0), timestamp: String(event.time || ""),
        result_text: String(event.error || "receipt recorded"),
        blocked_reason: failed ? String(event.error || "composite step failed") : undefined,
        is_running: false, state: failed ? "error" : "completed",
        actor_label, process_id: String(event.process_id || ""),
        detail_ref: String(event.detail_ref || "") || undefined,
        trace_refs,
      });
      activity.push({kind: "tool", toolIndex: index, detail_ref: String(event.detail_ref || "") || undefined});
      continue;
    }

    const projected = runtimeNoticeFromEvent({
      ...event, actor_label,
      ...(event.event === "worktree_promoted" ? {promotion_source:promotionSource(event,knownSubagents)} : {}),
    } as unknown as StreamEvent);
    if (projected) {
      const index = notices.length;
      notices.push({...projected, trace_refs});
      activity.push({kind: "notice", noticeIndex: index, detail_ref: projected.detail_ref, trace_refs});
    }
  }
  if (terminal?.status && !["running", "waiting", "awaiting_user"].includes(terminal.status)) {
    const endedAt = Date.parse(terminal.timestamp || "");
    for (let index = 0; index < tools.length; index += 1) {
      const tool = tools[index]!;
      if (!tool.is_running && tool.state !== "running" && tool.state !== "pending") continue;
      const startedAt = Date.parse(tool.timestamp || "");
      tools[index] = {
        ...tool,
        is_running: false,
        state: "unresolved",
        allowed: false,
        duration_ms: Number.isFinite(endedAt) && Number.isFinite(startedAt)
          ? Math.max(0, endedAt - startedAt) : 0,
        blocked_reason: "No durable tool result was recorded before the turn ended; execution state is unknown.",
      };
    }
  }
  return {activity: activity.filter(item => item.kind !== "draft"), tools, notices, providerUsage};
}

/** Ledger progress and historical question cards are alternate projections of
 * the same records, not extra chat answers. Keep their trace position when known. */
export function withProcessMessages(model: TurnTimelineModel, messages: ChatMessage[] = [], turnId = ""): TurnTimelineModel {
  const activity = model.activity.slice(), notices = model.notices.slice();
  const progress = new Map(activity.flatMap((item,index) => item.kind === "progress" && item.update_id
    ? [[progressIdentity({...item,turn_id:item.turn_id || turnId}),index] as const] : []));
  const decisions = new Map<string,number>();
  notices.forEach((item,index)=>{if(item.kind==="decision_required" && item.decision_id && !decisions.has(item.decision_id))decisions.set(item.decision_id,index);});
  const records = new Set(activity.flatMap(item=>item.record_id ? [item.record_id] : []));
  for (const message of messages) {
    const record_id = message.message_id || message.id;
    if (record_id && records.has(record_id)) continue;
    if (record_id) records.add(record_id);
    if (message.kind === "progress") {
      const key = message.update_id ? progressIdentity({...message,turn_id:message.turn_id || turnId}) : "";
      const previous = key ? progress.get(key) : undefined;
      const item: StreamActivity = {kind:"progress",text:message.content,visibility:"public",record_id,
        process_id:message.process_id,update_id:message.update_id,turn_id:message.turn_id,progress_status:message.progress_status};
      if (previous !== undefined) activity[previous] = {...activity[previous],...item,trace_refs:undefined} as StreamActivity;
      else {if (key) progress.set(key,activity.length); activity.push(item);}
    } else if (message.decision) {
      const previous = decisions.get(message.decision.decision_id);
      const notice: RuntimeNotice = {key:record_id || `decision:${message.decision.decision_id}`,kind:"decision_required",
        label:message.decision.question,severity:"info",decision:message.decision,decision_answer:message.decision_answer,
        decision_id:message.decision.decision_id,process_id:message.process_id};
      if (previous !== undefined) notices[previous] = {...notices[previous],...notice};
      else {decisions.set(message.decision.decision_id,notices.length); activity.push({kind:"notice",noticeIndex:notices.length,record_id}); notices.push(notice);}
    } else {
      const [label,...rest] = message.content.split("\n");
      activity.push({kind:"notice",noticeIndex:notices.length,record_id});
      notices.push({key:record_id || `saved:${notices.length}`,kind:message.kind || "saved_message",label:label || "",
        detail:rest.join("\n") || undefined,severity:"info",process_id:message.process_id});
    }
  }
  const answered=new Set(messages.flatMap(message=>message.decision && message.decision_answer != null ? [message.decision.decision_id] : []));
  const savedQuestions=new Set(messages.flatMap(message=>message.decision ? [message.decision.decision_id] : []));
  return {...model,activity:activity.filter(item=>{
    if(item.kind!=="notice")return true;
    const notice=notices[item.noticeIndex], id=notice?.decision_id || "";
    if(notice?.kind==="user_decision_received" && answered.has(id))return false;
    return notice?.kind!=="decision_required" || !savedQuestions.has(id) || decisions.get(id)===item.noticeIndex;
  }),notices};
}
